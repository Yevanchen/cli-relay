# Design decisions (v0.1, experimental)

Status: accepted for v0.1. Each decision lists the evidence it rests on. Anything we
could not verify on the target machine is stated as such and re-checked in
[`acceptance.md`](acceptance.md).

## D1. Scope: three responsibilities, nothing else

cli-relay is a thin ledger and hand-off layer that runs on the machine where the
official coding CLIs (Claude Code, Codex) run. It only adds:

1. **No blind re-run for a repeated request key.** One `(scope, key)` maps to one
   logical task. The same key with a different payload is an explicit conflict.
2. **Reconciliation after disconnect.** The ledger durably records each attempt's host,
   cwd, keeper/CLI process identity, native session id, exit code and native result.
   Anything it cannot prove is reported as `unknown`.
3. **Terminal results kept until the consumer ACKs.** The terminal state and a stable
   completion event id are committed in one transaction. Events are re-delivered
   until a named consumer ACKs them. ACK is separate from "the CLI exited".

Non-goals: P2P messaging, a networked relay, PTY/terminal runtimes, an agent
harness, workflow orchestration, UIs, accounts, model routing, automatic retry of
unknown side effects, plugins, production deployment.

## D2. Baseline: what the native tools and existing projects already cover

The evidence below comes from source reading at fixed commits and from
`--help` of the CLI versions installed on the target machine (Claude Code
2.1.301, Codex 0.163.0). The Reason Network baseline is `b4ffb158`; a GitHub compare
shows that only two Cargo version strings differ from the reviewed `17aeddb1`, so
the review was not repeated. Agenvo is `9ec56436` and AGPair is `755d4b2e`.

| Capability | Claude Code `-p` | Codex `exec` | Grok command channel | Reason Network | Agenvo | AGPair |
|---|---|---|---|---|---|---|
| Caller-chosen native session id | yes: `--session-id <uuid>` | no (thread id assigned at start) | – | – | – | – |
| Exact resume | `--resume <uuid>` | `exec resume <uuid>` | – | – | Codex `thread/resume` | executors run with `--no-session-persistence` / `--ephemeral` |
| Machine-readable result | `--output-format stream-json` (final `result` event) | `--json` JSONL events, `-o` last message | process exit + output | – | native data, no classification | receipts + exit code |
| Request key → one task | – | – | – | per-message ULID; retries mint new ids | relay mints a new requestId per call | `--idempotency-key` scoped to repo; **same key + different body silently returns the old task** |
| Survives caller disconnect | only if detached | only if detached | waiter process | daemon | connector reconnects | `start_new_session` executor |
| Durable attempt / identity record | native transcript only | native rollout only | – | sessions table, no tasks | in-memory pending, no task store (by design) | tasks/attempts tables, PID + start-time check |
| Crash-window reconciliation | – | – | – | – | `unknown`, no write replay | `new` task can be orphaned; no `unknown` state |
| Result kept until consumer ACK | – | – | not durable | outbox dropped after 7 days | webhook ≤5 attempts (~30 s) then dropped | 30-day GC independent of acceptance |
| Permission posture | caller-chosen | caller-chosen | – | – | Codex forced `danger-full-access` + `never` | defaults bypass approvals/sandbox; auth may fall back to a non-subscription provider |

A real incident on the target machine shows the Grok-side gap. A waiter process
ended normally, but its completion event never reached the bot, and a later query
returned "No shell found". A waiter is not a durable record.

**Conclusion.** The official CLIs already provide execution, native session
identity, exact resume and machine-readable results, so cli-relay must not re-implement them. They do not provide request
keys, payload-conflict detection, a durable per-attempt ledger, or
retain-until-ACK delivery. Those three gaps are the only things cli-relay adds.

## D3. Reuse AGPair? → No dependency; borrow proven patterns (credited)

AGPair is the closest existing implementation: Python + SQLite tasks/attempts, a
repo-scoped idempotency key, receipt de-duplication and a state-change watch. It
is not a blank space, and this repository does not claim to be the first.

We evaluated three options:

1. **Use AGPair as-is.** It fails the invariants without code changes:
   - same key + different body returns the old task (`agpair/cli/task.py:1830-1858`), and no payload hash is stored;
   - a task can be orphaned between `create_task` → `Popen` → `mark_acked` (`cli/task.py:1874-2017`; the daemon only polls `acked` tasks, `daemon/loop.py:440`);
   - per-kind default execution budgets (900–5400 s) SIGTERM/SIGKILL the executor's process group (`wait_policy.py:37-45`, `daemon/loop.py:621-667`);
   - terminal tasks, receipts and journal are deleted after 30 days regardless of acceptance (`daemon/loop.py:770-783`);
   - the Claude executor defaults to `--permission-mode bypassPermissions` and `--no-session-persistence` (`executors/claude_code.py:19-23,90`); the Codex executor to `--dangerously-bypass-approvals-and-sandbox --ephemeral` (`executors/codex.py:9-15,32`);
   - Claude auth `auto` falls back to a CC Switch provider and maps its token to `ANTHROPIC_API_KEY` (`executors/claude_auth.py:155-164,555-565`).
2. **Patch / fork AGPair.** Every fix lands in its core paths (the 3.6k-line
   `cli/task.py`, the daemon loop, the executors and auth). The fork would also carry
   features we must not ship (executor routing, fan-out workflows, the Antigravity
   bridge, global hook installers, a VS Code companion). Upstream development
   stopped at the end of June 2026 per the author, though the repository is not
   archived, so an upstream patch is unlikely to land soon. The "thin" adapter would
   in practice be a large fork.
3. **Small independent core.** Implement only D1, keep execution in the official
   CLIs, and port AGPair's proven patterns with attribution (MIT):
   - a partial unique index plus an `IntegrityError` fallback for concurrent submits;
   - a runner that records the real exit code;
   - atomic state files;
   - `start_new_session` detachment;
   - process identity as pid + OS start time;
   - only an explicit operator action creates a new attempt.

**Decision: option 3.** We revisit it if AGPair (or another maintained project)
gains payload-conflict detection, crash-window `unknown` handling and
retain-until-ACK delivery. At that point cli-relay should shrink to an adapter.

## D4. Agenvo and Reason Network are not used in v0.1

- **Agenvo.** Its Codex connector overwrites caller options with
  `approvalPolicy: "never"` + `danger-full-access` on every work entry point
  (`apps/codex-app-server/src/codex-execution.ts:4-25`) and auto-approves requests
  (`:31-59`). At start-up it resumes every loaded thread of the attached app-server
  with those settings (`packages/connector/src/connection.ts:295-316`,
  `apps/codex-app-server/src/codex.ts:165-207`). This cannot be enabled on a real
  machine. Its `unknown` execution vocabulary and the "no automatic write replay"
  rule are adopted as design ideas.
- **Reason Network.** It is a session-to-session message bus. Its ACKs mean "stored"
  or "injected into a session" (`src/net.rs:648-664`, `src/proto.rs:139-143`), not
  "task finished". It needs a resident daemon, agent hooks and P2P networking. A
  single-machine ledger does not need it. It could later serve as an optional wake
  channel, while delivery would still be reported as pending until ACK.

## D5. Language and storage: Python standard library + SQLite (chosen, not assumed)

Requirements:
- several records (terminal state, event, attempt facts) committed atomically;
- a unique constraint that makes concurrent same-key submits converge;
- crash consistency on a local disk;
- explicit errors on disk-full or corruption;
- no third-party dependencies;
- runs on macOS and the free `ubuntu-latest` CI runner.

| Option | Verdict |
|---|---|
| POSIX shell + files | no multi-record atomicity; locking differs across macOS/Linux; quoting risk |
| JSON file per task + `flock` + atomic rename | viable, but uniqueness, multi-file ACK atomicity and corruption detection would be hand-built |
| **SQLite via Python `sqlite3`** | transactions, unique constraints, `BEGIN IMMEDIATE`, integrity check; ships with Python (target machine: Python 3.14.3 / SQLite 3.52.0) |
| Go / Rust binary | toolchain and release pipeline for ~1–2k lines of logic; no benefit for v0.1 |

Constraints we accept: the ledger must live on a local filesystem (WAL is not safe
on network filesystems), and Windows is unsupported in v0.1.

## D6. One execution backend per engine

- **Claude Code:** `claude -p --output-format stream-json --verbose --session-id <uuid> --permission-prompts none`, with the prompt on stdin. Resume uses `--resume <exact uuid>`; never `--continue`.
  - *Not* `--bg`: background sessions do not hand back the CLI exit code or the final `result` event to the caller.
  - The native supervisor may restart sessions on its own, so relay-level submit counts would not bound native executions.
  - `claude agents --help` needed interactive approval on the target machine, so that interface could not be verified there.
- **Codex:** `codex exec --json -o <file> [--sandbox read-only|workspace-write] -`, with the prompt on stdin. Resume uses `codex exec resume <exact uuid> -`; never `--last`.
  - *Not* the app-server: it would need a resident server, which is the Agenvo route.
- **Fixture:** a deterministic local program speaking the same event format, used by tests, CI and the soak run.

## D7. A minimal per-attempt keeper, no daemon

The CLI exit code is only observable by its parent. A tiny keeper process owns one
attempt:
1. Claim the attempt with compare-and-set.
2. Run preflight checks.
3. Record "spawning".
4. Spawn the CLI with stdout/stderr redirected to **files** (not pipes, so the CLI survives keeper death).
5. Record the CLI pid + start time.
6. Wait for exit.
7. Write `exit.json` atomically.
8. Commit the terminal state + completion event in one transaction.

The keeper is double-forked into its own session, so it survives the caller's
SSH/terminal disconnect. The CLI stays in the keeper's process group, so the group
reveals an orphaned CLI even if its pid was never recorded. There is no resident
service. Reconciliation runs inside the short `reconcile`/`status`/`wait`/`events`
commands.

## D8. Permissions and billing

cli-relay never adds a permission-relaxing flag:
- no `--dangerously-*`;
- no `bypassPermissions`;
- no `--approve-for-me`;
- `--sandbox danger-full-access` is rejected.

Permission prompts that no host can answer are denied (`--permission-prompts none`).
A run with denials ends as `needs_input`, never auto-approved.

Before spawning, the keeper refuses (`blocked`) when it sees:
- API-key or gateway environment variables (names are reported, values never);
- a non-subscription login (`claude auth status` must report `claude.ai` /
  `firstParty`; `codex login status` must report ChatGPT).

The keeper also stops the CLI if the Claude init event reports an API-key source.
`--bare` is never used because it bypasses the subscription login.

## D9. Delivery and retention semantics

- Event ids are stored and stable across re-reads.
- ACK binds `(task, attempt, event, consumer)`. It is idempotent and rejects mismatches.
- Nothing is deleted automatically. There is no TTL and no GC.
- "Delivered", "the CLI exited", "the consumer ACKed" and "the user saw it" are different
  levels. The relay only promises the first three, in that order.

## D10. What v0.1 does not promise

- Exactly-once external side effects.
  - A re-run is never automatic, but an explicit resume can repeat a last step.
- Delivery of Grok, iOS or any other notification.
- Execution across sleep, power loss or reboot.
  - After the machine comes back, the ledger is readable and sessions can be resumed explicitly.
- Cross-machine or cross-engine migration.
- Protection against a malicious local user with the same UID.

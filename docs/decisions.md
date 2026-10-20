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

Constraints we accept:
- The ledger must live on a local filesystem. WAL is not safe on network filesystems.
- Windows is unsupported in v0.1.
- Python ≥ 3.11 is required, because Codex configuration is TOML and `tomllib` ships with
  3.11. Python 3.10 reaches end of life in October 2026.

## D6. One execution backend per engine

- **Claude Code:** `claude -p --output-format stream-json --verbose --session-id <uuid> --permission-prompts none`, with the prompt on stdin, plus `--allowedTools` only for the tools a task explicitly opts in to (D8). Resume uses `--resume <exact uuid>`; never `--continue`.
  - *Not* `--bg`: background sessions do not hand back the CLI exit code or the final `result` event to the caller.
  - The native supervisor may restart sessions on its own, so relay-level submit counts would not bound native executions.
  - `claude agents --help` needed interactive approval on the target machine, so that interface could not be verified there.
- **Codex:** `codex exec --json -C <cwd> -o <file> --sandbox <s> -`, with the prompt on stdin. `<s>` defaults to `read-only`; `workspace-write` or explicitly opted-in `danger-full-access` may be requested (D8).
  - Resume uses `codex exec --sandbox <s> resume -c sandbox_mode="<s>" --json -o <file> <exact uuid> -`; never `--last`.
  - `codex exec resume` has no sandbox option of its own. Without the pin it takes the sandbox from the configuration of the moment (Codex 0.163.0; audit #2). The exec-level `--sandbox` before `resume` is accepted by the installed CLI.
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
commands. Each of them, including plain polling, starts a keeper for an attempt that
provably never started, once that attempt has been pending for 5 s.

## D8. Permissions and billing

cli-relay keeps permission bypass flags forbidden:
- no `--dangerously-*`;
- no `bypassPermissions`;
- no `--approve-for-me`;
- no `--yolo` or `--full-auto`.

Codex full access is off by default. `sandbox="danger-full-access"` is rejected with a usage
error unless the same task also supplies the typed boolean `allow_full_access=true`:
`--option 'sandbox="danger-full-access"' --option allow_full_access=true`. Strings such as
`"true"` and numbers such as `1` are not accepted as booleans. The boolean alone is harmless:
it neither selects a sandbox nor changes the `read-only` default.

This is an explicit acceptance of risk: `danger-full-access` disables the Codex sandbox, so
commands can access files outside the task directory and use the network, subject to host
permissions. The adapter passes the real `--sandbox danger-full-access`, including on exact
resume, whose `sandbox_mode` override is pinned too. Argv checks require the stored pair of
options; keeper and crash recovery accept full-access runtime evidence only for opted-in
tasks. Runtime checks still require permission evidence and reject named permission profiles.
Codex config-layer permission checks and all subscription/billing checks stay in force.

Both options remain in the original task payload, including its hash, so duplicate submits
recover the same choice and changed options conflict under the same key. Exact resumes and
fresh reruns reuse that payload. Receipts, `status`/`wait`, terminal/unknown events and
notifications expose `sandbox` and `full_access` as requested permissions, including for
attempts blocked before spawn. `doctor` explains the opt-in, default, and risk. No schema
migration, dedupe, ACK, or notifier transport change is needed.

Claude adds no tool by default. `WebSearch` and `WebFetch` need permission, so under `dontAsk`
a research task without an allow rule for them ends `needs_input` with a structured denial
(issue #6). A task may opt in with the typed list `allowed_tools`, for example
`--option 'allowed_tools=["WebSearch","WebFetch"]'`. Only exact names from a fixed allowlist,
`WebSearch` and `WebFetch`, are accepted. A string, another tool, a rule pattern such as
`WebFetch(domain:…)` or `Bash(…)`, a wildcard or a repeated name is a usage error. Widening the
allowlist is a code change, not an option. The list is stored sorted, so its order is not part
of the request, and like every option it cannot be changed by a resume.

The adapter passes the tools as one `--allowedTools` value, including on exact resume; argv
checks accept that flag only with the stored list, and only in that form, since a further value
of the variadic flag would be one more allow rule. It never passes them through `--settings`,
never changes the permission mode and never writes a settings file. Claude's own precedence
therefore still holds: deny and ask rules from every settings layer are evaluated before allow
rules, a managed deny cannot be overridden by `--allowedTools`, and managed
`allowManagedPermissionRulesOnly` ignores it. A tool that stays denied is still a structured
denial, so the task ends `needs_input` rather than being retried or widened. The preflight does
not block on such rules; the CLI enforces them.

The list is part of the payload and its hash, like the Codex options. Receipts, `status`/`wait`,
terminal/unknown events and notifications expose `allowed_tools` as requested permissions
(`[]` by default). `doctor` explains the opt-in and its risk: these tools reach the network, so
fetched pages are untrusted input and a fetched URL can carry data out. With `--engine claude`
it also reports whether the checked settings layers already allow, ask for or deny each
allowable tool, by tool name and boolean only.

Permission prompts that no host can answer are denied (`--permission-prompts none`).
A Claude run with structured denials ends as `needs_input`, even if it also reports an
error, and is never auto-approved. A Claude configuration or init event in
`bypassPermissions` mode is blocked, because then nothing would be denied. Codex reports
no structured denial in `exec --json`, so a Codex outcome follows its own result. The
Codex sandbox is always explicit (`read-only` by default).

Before spawning, the keeper refuses (`blocked`) when it sees:
- API-key or gateway environment variables (names are reported, values never);
- a configuration layer that applies to the task directory and routes away from the
  subscription, or that cannot be read (layers and keys: [`reliability.md`](reliability.md));
- a non-subscription login (`claude auth status` must report `claude.ai` /
  `firstParty`; `codex login status` must report ChatGPT).

A login check alone is not enough. A Codex custom provider can take its key from any
environment variable while the ChatGPT login stays in place (audit #3).

The keeper also stops the CLI if the Claude init event reports an API-key source. After a
Codex run, the thread's own rollout must name the `openai` provider. Keeper and
reconciliation share one outcome function, so the result is the same when the keeper
died before committing (audit #8). `--bare` is never used because it bypasses the
subscription login.

## D9. Delivery and retention semantics

- Event ids are stored and stable across re-reads.
- ACK binds `(task, attempt, event, consumer)`. It is idempotent and rejects mismatches.
- Nothing is deleted automatically. There is no TTL and no GC.
- "The CLI exited", "stored and re-delivered on every read", "the consumer ACKed" and "the
  user saw it" are different levels. The relay records the first three. It cannot know the
  fourth.

## D10. What v0.1 does not promise

- Exactly-once external side effects.
  - A re-run is never automatic, but an explicit resume can repeat a last step.
- Delivery of Grok, iOS or any other notification.
- Execution across sleep, power loss or reboot.
  - After the machine comes back, the ledger is readable and sessions can be resumed explicitly.
- Cross-machine or cross-engine migration.
- Protection against a malicious local user with the same UID.

## D11. External audit of v0.1 (2026-10-19): handling and deviations

An adversarial review of commit `6c8d2f3` (product code `b72618d`) listed 9 must-fix, 6
should-fix and 2 clean-up items. All 17 were addressed, one commit each. Each fix has a
regression test that failed on the code before the fix ([`acceptance.md`](acceptance.md)).
No finding was rejected. Where the implementation differs from the suggested fix, the
reason and evidence are recorded here.

1. **Cancel without a verifiable target (#1).** The review asked to refuse when identity is
   insufficient. We refuse to *signal*, not to record the request:
   - `cancel` still stores the request, sends nothing, and says why (`not_signalled_reason`).
   - A keeper that is alive but unverifiable (for example, the kernel query failed) polls
     the ledger and honours the request within about 2 s.
   - If everything is gone, reconciliation closes the attempt as `cancelled`.
   - Refusing the command would drop the caller's intent without making anything safer.

   Evidence: `test_cancel_does_not_signal_a_recycled_process_group`,
   `test_cancel_still_stops_a_verified_orphan_cli`.
2. **"Verify the final provider, endpoint and auth source" (#3).** The CLIs do not hand a
   caller their resolved configuration, so cli-relay reads the files itself:
   - Known routing keys block.
   - Unreadable layers block.
   - Project layers are read even where Codex would ignore an untrusted project (fail closed).

   As a second, independent check, the Codex thread's own rollout must name `openai`. Two
   things stay open and are documented as limits, not claimed as covered: a routing setting
   cli-relay does not know about, and a resumed Codex turn (the rollout records the provider
   at thread creation).
3. **Real-CLI evidence for #2.** On the target machine Codex's default sandbox is already
   `read-only`. A real resumed turn running `read-only` is therefore consistent with both the
   fixed and the unfixed code.
   - The fix is shown by the argv tests.
   - The real loop records the resumed turn's sandbox from the rollout as supporting evidence
     only.
   - We did not run a wider sandbox just to make the difference visible.
4. **Beyond the review: the same root cause as #2, and G1.**
   - Tasks without a sandbox option took the sandbox from the configuration of the moment on
     both runs. Codex now always gets an explicit sandbox.
   - A Claude `permissions.defaultMode: bypassPermissions` would have made "never
     auto-approved" untrue. It now blocks.
5. **Soak thresholds (#9).** "Ran its full duration" means at least 95% of the configured
   long-task duration. The run must also last at least 99% of the planned time.
   - The fixture sleeps on a monotonic clock that does not advance during system sleep, so a
     real run only ever lasts longer.
   - The margin covers timestamp granularity and scheduling. A truncated run (1 s of 24.5 h
     in the review's counterexample) fails.
6. **Events query (#13).** The unacknowledged filter is a `NOT EXISTS` on the acks primary
   key, as suggested. No index was added:
   - an index changes the schema (version 2, with a migration);
   - the remaining cost is a scan inside SQLite, not rows moved into Python.
7. **Process-identity fallback (#4).** Agreed and removed. Consequence: on platforms other
   than macOS and Linux every identity is "cannot verify", so reconciliation there never
   closes an attempt as lost. Those platforms were already unsupported.
8. **Python floor.** `tomllib` needs 3.11 (D5). CI moved from 3.10/3.12/3.14 to
   3.11/3.12/3.14.

## D12. v0.2: proactive notification on top of the ledger (design record)

**Problem.** In v0.1 a finished task waited in the ledger until someone asked. A Grok Bot that
had dispatched it would only learn the result if the user asked again. Notification itself is a
solved problem: the review of existing systems found several working implementations. What
was missing was the last link from this ledger to the consumer's existing wake-up entry.

**Decision: reuse, don't add platforms.**
- **Ledger.** The ledger, its events and its ACKs stay the source of truth. There is no second
  database, message server, networked relay, model harness or phone app.
- **Pending fact.** It is the event the ledger already commits with the terminal state (a
  transactional outbox without a second write). A new `notifications` table records only the
  transport per (event, route).
- **Notifier.** It is a deterministic local program, `cli-relay notify flush` / `notify run`:
  - it never calls a model;
  - nothing polls the Bot;
  - the keeper starts one pass right after a terminal event;
  - the periodic pass covers retries, reminders and restarts.
- **Wake-up entry.** The machines' existing notifier (`notify.sh <job> <stage> <status>
  <summary> [artifact]`) feeding a Grok webhook routine. The command adapter speaks exactly that
  interface. An optional HTTP adapter calls the webhook directly when transport acceptance must
  be known.
- **Two acknowledgements, never merged.** The transport result (HTTP 2xx, or only "handed off"
  for `notify.sh`, which always exits 0) is one fact. The consumer's ACK, recorded by the Bot
  through `cli-relay ack` after it has reported, is the other. HTTP 200 from a routine means a
  run was started, not that a chat message exists.
- **Bounded by design.** The routine has no idempotency key, and each run uses the Bot's quota:
  - a lost response is `unconfirmed` and is never resent at once;
  - reminders are limited (`max_wakes`, default 2);
  - transport retries are limited (`max_attempts`, default 6);
  - due events of one route are batched into one wake-up;
  - an event that is never acknowledged stays visible (`unacknowledged`/`failed`) and is never
    cleared.
- **Two machines.**
  - Each machine has its own ledger, notifier and `source_id`.
  - Every payload names `source_id`, `ledger_id`, route and the event/task/attempt ids.
  - `ack --ledger-id` refuses an event of another ledger.
  - Nothing is read across machines.
- **Default wake-ups.** Only `succeeded`, `failed`, `blocked`, `needs_input` and `unknown`.
  Ordinary logs and heartbeats are not events.
- **Compatibility.** The schema change is additive and `schema_version` stays 1. v0.1.0 opens a
  migrated ledger, which is the rollback path. `migrate` writes a consistent SQLite backup
  first.

**Not done.**
- Merging progress messages: there are none; only outcomes are events.
- Reading `notify.sh`'s own log to infer HTTP results: another program's log format is not an
  interface.
- A daemon of our own: `notify run` is meant for the machine's existing service manager.

Usage, the routine contract, install and rollback are in [`notifier.md`](notifier.md).

## D13. Independent audit of v0.1.0 (2026-10-19): handling and deviations

An independent review of the published v0.1.0 (`baff533`) found defects that the earlier
review had not. They were handled as new findings, one commit each, each with a regression test
that failed on the unfixed code ([`acceptance.md`](acceptance.md), section 2.3).

1. **Soak verdict passed with zero events.** Every attempt must now have its event, delivered
   to and acknowledged by the soak consumer.
2. **Codex permissions beyond `--sandbox` (conditional).** On the target machine (codex-cli
   0.163.0) no requirements or managed layer exists, and every recorded turn ran in the
   requested sandbox with no named profile. So the conditional branch is not active there, and
   nothing was widened to try it. The fix fails closed in two places:
   - before the run, any permission setting, requirements or managed-preference layer, or an unsafe or
     unreadable selected config-profile blocks the run; benign profiles are parsed;
   - after the run, every recorded turn must show the requested sandbox or a narrower one, with
     no named profile.

   Deviation: this can block a configuration that is in fact harmless. The reason is reported
   by key name.
3. **Cancel vs. reconciliation race left `unknown`.** The closing write re-reads the attempt in
   its own transaction. Attempts already left that way converge to `cancelled` once their
   processes are provably gone.
4. **Descendants in the process group outlived a cancel.** After a cancel or a guard stop the
   keeper stops every member of its group and commits only once the group is empty.
   Deviation: after a *normal* exit, lingering members are recorded but not killed, because
   they may be intended (for example a started server). Resume still refuses while they run.
5. **Empty Linux boot ids compared as equal.** Any missing part of an identity is now
   "cannot verify". Consequence: on a Linux host without a readable boot id, reconciliation
   cannot prove a keeper gone and keeps observing.
6. **Unexpected configuration structure crashed the preflight.** It now converges to `blocked`:
   - routing and permission values are checked; this is not a complete vendor-config validator;
   - any remaining exception in the preflight or argv construction closes the attempt as
     blocked, instead of re-kicking a keeper that crashes again.
7. **Tests read the host environment.** Test environments are built from scratch, with a minimal
   `PATH` and an empty home. Before the fix, a re-kicked keeper in one test could find the
   host's real `codex` through the inherited `PATH`. It created no session and made no model
   request.
   Deviation: system and managed CLI configuration cannot be redirected for a test without
   weakening the production check, so tests that need a clean configuration skip on hosts that
   have it.
8. **An efficiency statement exceeded the evidence.** The unacknowledged-events query fetches no
   acknowledged rows into Python, but SQLite still scans the whole event table. The
   documentation now says so; no performance claim is made.

## D14. Independent review of the v0.2 candidate (2026-10-19)

The original candidate `d27b849` passed its 156 tests but failed new counterexamples. The
repairs keep the existing CLI, SQLite ledger and adapters; they add no gateway or message service.

- Invalid credentials and adapter failures use fixed error codes, never raw exception text.
  A finished or failed pass releases its in-process claim even when result recording fails;
  recovery keeps an ambiguous send distinct from a send known not to have happened.
- Notification route, consumer and chat are saved once. Configuration changes cannot transfer
  an old task or reinterpret another consumer's ACK. An identical submit recovers its receipt
  even if the route has since been removed. Historical missing owners need explicit restoration.
- Recover abandoned claims before checking ACKs and sending. An already acknowledged event
  must not be posted again merely because its sender died.
- Server `Retry-After` is a lower bound after local exponential backoff is capped. Responses
  indicating failure do not prove the receiver did no work; possible wake-ups count toward the
  budget. The transport bounds are not exactly-once or measured model-call counts.
- A requested cancellation is not proof of stopping. If descendants remain or cannot be queried,
  retain an observable unknown outcome until their absence is proved. Never signal a stale,
  unverified process group to make this check pass.
- Codex resume permission evidence must include the current attempt's new turn, anchored to a
  saved pre-run rollout boundary. Keeper and reconciliation use the same evidence.
- Normal rollback preserves the current ledger. Stop periodic, immediate and in-flight sends
  before enabling a previous notifier. Restoring an old pending-task backup can repeat work;
  it is a manual data-recovery operation, not the code rollback path.

Merge checks are isolated regressions, the complete suite and CI for the fixed code. Real
Grok chat delivery, the two host installations, exact model/Max settings and the 30-minute
window remain deployment checks. The 25-hour soak remains uncompleted and nonblocking.

# Usage

Reference for running cli-relay: commands and exit codes, per-task notification routes, engines
and task options, how a consumer should use the ledger, limits, and tests. The
[README](../README.md) has the overview and a quick start.

## Commands

```sh
pip install .            # or run in place: PYTHONPATH=. python3 -m cli_relay ...
cli-relay init           # ledger in $CLI_RELAY_HOME or ~/.cli-relay

# submit under an idempotency key; the CLI runs detached from this shell
cli-relay submit --key fix-tests-42 --engine claude --cwd ~/code/app --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --prompt "Run the unit tests and summarise failures."
# {"accepted": true, "status": "created", "task_id": "t-…", "native_session_id": "…", "state": "accepted", …}

cli-relay submit --key fix-tests-42 --engine claude --cwd ~/code/app --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --prompt "Run the unit tests and summarise failures."     # same key + payload: same task
# {"status": "duplicate", "task_id": "t-…", "submit_count": 2, …}

cli-relay wait t-… --timeout 600   # a timeout only stops waiting; the task keeps running
cli-relay events --consumer grok   # waiting/result events not yet ACKed by this consumer
cli-relay ack --consumer grok --task t-… --attempt 1 --event e-…
cli-relay brief --consumer grok --text # read-only message lines and one batch ACK command
cli-relay status t-…               # full record: attempts, identities, exit status, native result
cli-relay resume t-… --prompt "Continue with the next failing test."   # exact native session
cli-relay submit --key follow-up-1 --engine claude --cwd ~/code/app --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --session 5b2f0c1e-… --prompt "Also update the changelog."   # next turn of an existing session
cli-relay cancel t-…

# v0.2: wake a consumer when tasks end (configuration in <home>/notifier.json, see docs/notifier.md)
cli-relay migrate                  # once per existing v0.1 ledger: backup, then add the notifier tables
cli-relay submit --key k --engine claude --cwd ~/code/app --prompt "…" --notify-route bot --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"'
cli-relay notify run --interval 60 # periodic pass (retries, reminders, catch-up after a restart)
cli-relay notify status            # per event: transport state and consumer ACK
```

Every command prints exactly one JSON object on stdout. `brief --text`, `--help` and `--version`
print plain text. Exit codes:

- `0` ok
- `1` internal error (`"error": "internal"`)
- `2` usage
- `3` key conflict
- `4` not found
- `5` ledger or storage error
- `6` refused (a precondition does not hold)
- `130` interrupted

## Dependencies and queues

`submit` accepts one `--after REF`, where REF is an existing task id (`t-…`) or request key
in the same `--scope` and relay `--home`. An exact task id takes precedence over a matching key.
The stored `after` is the resolved task id, so submitting by key or by id gives the same payload.
An unknown or out-of-scope REF is rejected with exit 6 / `dependency_not_found`, before any task
or submission is created. Forward references and self references cannot create tasks; changing
an existing key's payload is still a conflict, so cycles cannot be introduced.

`--after-require` needs `--after` and accepts:

| Value | Release condition once the dependency is terminal |
|---|---|
| `ok` (default) | outcome `succeeded` **and** business status `ok` |
| `ok_or_partial` | outcome `succeeded` **and** business status `ok` or `partial`, with no business stop reason |
| `any_terminal` | any terminal outcome, including `failed`, `cancelled`, `blocked`, `needs_input` and `unknown` |

The check uses the dependency's current attempt at admission, and reads the business-status
observation already committed by the [business-status rules](#business-status). It does not
interpret prose or reread the dependency's result file. Historical attempts without that
observation count as `unreported`; they do not satisfy either success requirement. An `unknown`
outcome means execution is uncertain, even though `any_terminal` permits it.

An unsatisfied terminal dependency ends the dependent as `blocked` without starting its engine.
Attempt evidence, the terminal event and the notification carry
`reason_code: "dependency_not_satisfied"`, a one-line `summary` naming the dependency id and
its business status, and `details`:

```json
{"dependency_task_id":"t-…","dependency_outcome":"succeeded",
 "dependency_business_status":"partial","require":"ok"}
```

For example, submit the English task immediately after the Chinese task, without a dispatcher
poller. The Chinese task must explicitly report business completion:

```sh
cli-relay submit --scope friday --key what-is-dify-zh-10 --engine claude --cwd ~/code/dify --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --prompt 'Write the Chinese article. End with status=ok only if fully complete; otherwise report status=partial with a separate stop_reason where needed.'
cli-relay submit --scope friday --key what-is-dify-en-04 --engine claude --cwd ~/code/dify --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --after what-is-dify-zh-10 --queue articles \
  --prompt 'Write the English article from the completed Chinese article.'
# A succeeded Chinese run reporting status=partial blocks the English task by default.
# Use --after-require ok_or_partial only when a partial result is acceptable.
```

`--queue NAME` accepts 1–64 characters from `[A-Za-z0-9._-]`. Queues are isolated by relay home
and scope. At most one task in a queue is admitted to preflight/execution at a time. Other
tasks wait FIFO by their original submission time; ties use ledger insertion order. Pending
keepers also hold their place. A queue head waiting for a dependency holds up later tasks.
All engines/scopes share a home-wide [memory-aware concurrency budget](#local-concurrency-and-available-memory)
and a computer-use singleton, including tasks without `--queue`. One canonical relay home must
serve each Mac. See [capacity and rollout](migration-20261106.md#shared-local-admission).
Combine `--after` and `--queue` to require both gates. Failed/blocked/cancelled queue tasks
release their place; an `unknown` holder retains it because its engine may still be alive and
needs the existing explicit recovery decision. Resume/fresh reruns retain both gates and the
task's original queue order, and cannot preempt an already admitted task.

| Task state | Meaning |
|---|---|
| `accepted` | committed; keeper has not claimed it yet |
| `waiting` | keeper owns the attempt; a dependency, earlier queue task or shared capacity prevents execution |
| `running` | admitted to preflight or engine execution |
| `succeeded`, `failed`, `blocked`, `needs_input`, `cancelled`, `unknown` | existing outcomes; `unknown` still requires a human recovery decision |

`status` and `wait` include current `task.waiting_on` and current-attempt `waiting_on`, with
`dependency` (the four fields above) and/or `queue` (`name`, one-based `position`,
`ahead_task_ids`). A single `kind: "waiting"` event per waiting attempt records its initial
waiting details; it remains an immutable snapshot and can be ACKed independently of the later
terminal event. Waiting events do not wake the notifier. Position changes appear in status,
not as repeated events. Waiting is non-terminal: `wait --timeout` can time out while it continues,
and `cancel` ends it as `cancelled` without starting the engine. Waiting attempts cannot be resumed.

Each waiting keeper polls the ledger after 2, 4, 8, then at most 10 seconds between checks;
dependency notifications are not needed for release. No scheduler daemon is added. If a waiting
keeper is provably dead, `reconcile` (also invoked by normal status/wait/events reads) closes
the task as `blocked` with `reason_code: "keeper_lost_while_waiting"`. It never launches that
engine or replaces that waiting keeper. A keeper that never claimed a pending attempt still
uses the existing safe recovery path.

The optional top-level payload fields are `after` (resolved id), `after_require` (only when
explicitly supplied) and `queue`. Supplied fields participate in idempotency: changing them
under the same key conflicts. Omitting all three preserves the previous payload and hash.
No ledger schema change or migration is required; existing databases and events remain readable.

### Local concurrency and available memory

Admission checks available memory before claiming each new or resumed attempt. Defaults:

| Setting in `local_concurrency` | Environment override | Default | Meaning |
|---|---|---|---|
| `hard_max` | `CLI_RELAY_LOCAL_HARD_MAX` | `4` | Total active jobs allowed when memory is sufficient; always an upper bound |
| `min_available_mib` | `CLI_RELAY_LOCAL_MIN_AVAILABLE_MIB` | `1536` | Minimum available memory for an additional parallel job (1.5 GiB; 1 MiB = 1,048,576 bytes) |
| `fallback_max` | `CLI_RELAY_LOCAL_FALLBACK_MAX` | `2` | Fixed cap if the memory observation fails, clipped to `hard_max` |

When available memory is below the threshold, the effective cap is one. A task waits in
`waiting`, keeps its original attempt and key, and starts once memory or capacity recovers;
low memory never rejects or ends it. At least one eligible task can run even at zero available
memory, subject to the existing dependency, queue, session and computer-use gates. Already
running work is never killed or preempted when memory falls or a setting is lowered.
Claimed/preflight work and unknown attempts without proof of stopped processes still count.
Computer-use work continues to share the total budget and its separate cap of **one**.

Settings use the existing JSON configuration in the relay home (`--home`, `$CLI_RELAY_HOME`,
or `~/.cli-relay`). Add `local_concurrency` to `<home>/notifier.json`, keeping existing routes
and other settings. It is optional; an absent file or section uses the defaults. A ledger
with no notification routes can use this complete file:

```json
{
  "source_id": "mac-mini",
  "routes": {},
  "local_concurrency": {
    "hard_max": 4,
    "fallback_max": 2,
    "min_available_mib": 1536,
    "sources": {
      "mac-mini": {"hard_max": 4, "min_available_mib": 1536},
      "mbp": {"hard_max": 6, "min_available_mib": 2048}
    }
  }
}
```

Precedence, highest first: environment variable → `sources[source_id]` → shared
`local_concurrency` field → default. The machine identity is the existing top-level
`source_id` in `notifier.json`; unmatched identities use the shared settings. Machine
entries may override any of the three fields independently. `hard_max` and `fallback_max`
must be integers ≥ 1; `min_available_mib` must be an integer ≥ 0. Zero disables the memory
threshold when a reading is available; `hard_max: 2` restores a two-job ceiling. Unknown
keys, booleans and fractional values are invalid. A bad file or environment setting leaves
new admissions waiting with `invalid_concurrency_config` until corrected, while status
remains readable and running tasks continue. Route registration preserves this section.

For a one-command environment override:

```sh
CLI_RELAY_LOCAL_HARD_MAX=3 CLI_RELAY_LOCAL_MIN_AVAILABLE_MIB=2048 \
  cli-relay submit --key example --engine fixture --cwd "$PWD" --prompt "fixture example"
```

Use the same environment for every relay process using that home. A keeper inherits the
environment of the command that starts it; changing another shell's environment cannot
change an already-waiting keeper. File settings and memory are reread at every admission
poll (the existing keeper polls every 2–10 seconds); replace JSON files atomically when
editing them. No install or daemon restart is needed for file changes in an upgraded keeper.
Do not mix old installed keepers with the upgraded code during a later rollout.

On macOS, a single `/usr/bin/vm_stat` call with a one-second timeout estimates available
bytes as `(Pages free + Pages inactive + Pages purgeable) × reported page size`, including
both 4 KiB and 16 KiB pages. It needs no `sudo`. These fields and the output format are
documented in [Apple's vm_stat source](https://github.com/apple-oss-distributions/system_cmds/blob/main/vm_stat/vm_stat.c).
On Linux, the value is `/proc/meminfo`'s `MemAvailable` in KiB, the kernel's estimate of
memory available to new applications ([kernel documentation](https://www.kernel.org/doc/html/latest/filesystems/proc.html#meminfo)).
An unsupported system, missing command, timeout, unreadable file or malformed output uses
the fixed fallback and reports the probe error. No memory-reading failure crashes the keeper.
This is a point-in-time admission check, not a reservation of future task memory or an
out-of-memory guarantee. CPU idle percentage does not affect admission.

`cli-relay doctor` and `cli-relay status TASK_ID` include `local_concurrency` with the current
`source_id`, `hard_max`, `fallback_max`, threshold, per-setting sources, effective `max_active`,
`available_bytes`, `memory_source`, `memory_error`, `mode` and `reason`. Available memory is
`null` when unreadable. Doctor marks an invalid concurrency configuration unhealthy; a probe
failure alone is a reported fallback, not a failed health check.

For a waiting task, `task.waiting_on.capacity` and the current attempt's `waiting_on.capacity`
also show holders, eligible tasks ahead and `reasons`: `hard_max`, `low_available_memory`,
`fixed_fallback_limit`, `computer_use_limit`, or `invalid_concurrency_config`. Multiple limits
can apply. Other gates keep their existing dependency/queue/session diagnostics. The waiting
event is one durable initial snapshot; status shows the current decision. For example, with
two tasks active and only 200 MiB available, a third stays `waiting` with
`reasons: ["low_available_memory"]`, `hard_max: 4`, `max_active: 1` and
`available_bytes: 209715200`. The two active tasks keep running.

### Business-status dependency diagnostics (issue #32)

For `--after` with the default `ok` or explicit `ok_or_partial`, submit receipts warn with
`dependency_business_status_not_declared` when the active upstream task declares neither
`result_file` nor `require_business_status=true`. A terminal upstream whose business status is
`unreported` instead produces `dependency_business_status_unreported`, even if it declared a
result file. A terminal upstream with a reported status, `any_terminal`, and tasks without
`--after` do not produce these warnings. Warnings describe the upstream's current attempt and may
change when a duplicate submit reads a later state; they never change the payload hash or gate.

The hint gives the result options and canonical values: `status` is `ok|partial|failed`, and JSON
`stop_reason` is `null|blocked|needs_user|stopped_for_approval`. Ask the upstream producer for those
values, or a plain final `status=...` line. Merely adding the options does not make a model report
correctly. An already finished upstream needs an explicit resume or a corrected task with a new key;
the relay does not rewrite its prompt, retry it, or treat an unreported result as success.

When an unreported result blocks the downstream, `details.dependency_business_reports` contains
the frozen upstream `result_file`/`status_line` raw status and stop reason (strings bounded to 128
characters, or null). The one-line block summary includes `raw_reports`, with control characters
escaped; terminal events and notification entries carry the same details. No result file is reread,
no prose or arbitrary JSON fields are copied, and historical events are unchanged.

## Sending a message to an existing session

`submit --session SESSION_ID` delivers the prompt as the next turn of an existing Claude Code
session or Codex thread, instead of starting a new one (issue #18). It is a submit mode: key,
scope, cwd, engine, task/resource class, options, routes, origin, `--after` and `--queue` work as
for any task, and the result goes through the same ledger, events, notifications, `brief` and ACK.

```sh
cli-relay submit --key follow-up-1 --engine codex --cwd ~/code/app --task-class complex --resource-class standard --option 'model="gpt-6.1-sol"' --option 'effort="max"' --option 'sandbox="workspace-write"' \
  --session 01a1bb19-1c70-70b0-af39-97a899ae2e77 --prompt "Also handle the empty-list case."
```

| Engine | The turn runs as | `--session` |
|---|---|---|
| `claude` | `claude -p … --resume <id> --permission-mode dontAsk --permission-prompts none` | the Claude session UUID |
| `codex` | `codex exec --sandbox <s> resume -c sandbox_mode="<s>" … <id> -` | the Codex thread UUID |
| `fixture` | the fixture's own `--resume <id>` | a fixture session (tests) |

The argv is the existing exact-resume argv, built from this task's own options: permission mode,
sandbox, network pin, `--allowedTools`, model and effort. Nothing is taken from the settings the
session ran with before.

Where session ids come from (checked locally with Claude Code 2.1.312 and codex-cli 0.162.1
unless noted):

- cli-relay: `native_session_id` in a Claude submit receipt (the relay chooses it), and in
  `status`/`wait` attempts and terminal events once a Codex run has started (`thread.started`).
- Claude Code: `session_id` in the output of `claude -p --output-format json` or `stream-json`, and
  the transcript name `~/.claude/projects/<project>/<session-id>.jsonl` (under `CLAUDE_CONFIG_DIR` if
  set). The interactive `/status` view was not checked.
- Codex: `thread_id` of the `thread.started` event of `codex exec --json`, and the rollout name
  `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread-id>.jsonl` (under `CODEX_HOME` if set). The
  Codex App and the `codex resume` picker were not checked.

Only an exact UUID is accepted, normalised to lower case; names, `--last` and patterns are usage
errors. The session must exist on this machine, for the user and configuration directories the
keeper inherits from `submit`.

**Checks and failure reasons.** For a new key, `submit` refuses with exit 6 and creates nothing.
The keeper checks again right before the turn is sent; a failure then closes the attempt as
`blocked` without starting the CLI, with `preflight`, `reason_code` and a one-line `summary` in
attempt evidence, the terminal event body and the notification summary:

| Reason | When |
|---|---|
| `session_not_found` | no Claude transcript, Codex rollout or fixture state for the id; `searched` names the pattern |
| `session_busy` | another task in this relay home holds the session or waits for it (`holder: "relay_task"`, `holder_task_ids`); a live Claude Code process has it open (`holder: "claude_process"`, `pid`, `kind`, `entrypoint`, `status`); or the Codex thread-writer lock is held or cannot be checked (`holder: "codex_thread_writer_lock"`, `lock`) |
| `provider_not_subscription`, `provider_unverified` | the Codex thread's own record does not name the built-in `openai` provider, or has no such record |
| `session_permissions_wider` (submit only) | the Codex thread belongs to an ordinary relay task whose sandbox or network is narrower than this message's: that task's resumes check every turn of its thread, so a wider turn would block them for good |
| the existing preflight reasons | billing variables, configuration layers and login, as for every task (`billing_env_present`, `config_not_subscription`, `auth_not_subscription`, …) |

After the run the existing checks apply: Claude's init event must show `dontAsk` and no API-key
source; the Codex thread must name `openai`, and the turn must show the requested sandbox and
network. For a message to an existing thread only the turns after the attempt's pre-run checkpoint
are checked: earlier turns were not run by this task, so a user's `workspace-write` history neither
blocks a `read-only` follow-up nor counts as permission for it. A Codex resume that loses a race to
another writer exits without a turn record: it ends `blocked` (`permissions_unverified`) with a
`session_busy` warning. A receipt warning `session_cwd_differs` notes a session created in another
directory: the turn runs in `--cwd`, where its tools and the relay's git, result-file and artifact
checks apply. In local checks a turn resumed from another directory recorded that directory, in the
Claude transcript and in Codex's `turn_context.cwd`.

`resume` of such a task makes the same session checks as `submit --session`. Scopes are not a
security boundary here: any caller of a relay home can send to any session of the machine's user,
and the reply carries that session's context into the caller's own events. Treat a session id like
access to the session. A keeper that a later `status`, `wait` or `events` restarts inherits that
command's environment, so poll with the same `HOME`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` as the
submit. Before rolling back to a release without `--session`, let waiting and pending messages end
or cancel them: an older keeper would resume the session without these checks.

**Records.** The payload gains `target_session_id`. It is part of the hash: the same key with
another session is a conflict, and without `--session` the field is omitted, so existing hashes are
unchanged. The first attempt has `reason: "send"` and `resume_session_id = native_session_id = <id>`.
Receipts, `status`/`wait` task records, terminal and unknown event bodies, notification entries and
`brief` JSON carry `target_session_id`. No ledger migration is needed. `resume` continues the same
session with another message; `resume --fresh` is refused (`fresh_not_supported`), because a new
session would lose the context the message was meant for.

**Concurrency.**

- In one relay home at most one attempt runs a session at a time, across scopes. While another
  task holds the session or waits for it, `submit --session` and `resume` refuse with
  `session_busy`. To queue a message behind a running task, submit it with
  `--after <that task> --after-require any_terminal`: it waits (`waiting_on.dependency` and
  `waiting_on.session`) and runs right after, in the same session. At admission the keeper also
  waits, first come first served, for any other holder, so two relay attempts never overlap on one
  session. An `unknown` holder keeps the session until its processes are proven stopped.
- Claude Code takes no lock of its own. With 2.1.312, `claude -p --resume <id>` ran while another
  process had the session open; the open process did not see the appended turn, its next turn forked
  the transcript, and a later `--resume` continued that fork, so the appended turn was lost. The
  relay therefore refuses while `<config>/sessions/<pid>.json` names the session with a live pid.
  Claude Code registers interactive (`entrypoint: "cli"`) and `-p` (`"sdk-cli"`) sessions there. An
  entry whose pid is gone, a zombie, or a process that started more than a minute after the
  registered `startedAt` (a pid reused after a crash) is ignored; a refusal names the entry's file.
- Codex holds `<CODEX_HOME>/thread-writer-locks/<id>.lock` while a turn writes the thread, and a
  second writer fails with `thread … already has an active writer`. The relay probes that lock with
  a non-blocking shared lock, released at once, so it only conflicts with a writer. This was
  observed on macOS; on Linux, where `flock` and `fcntl` locks are independent, the probe is
  **not verified** against Codex, and a missed writer is still caught by Codex's own refusal.
- Submit and resume repeat the relay check inside their write transaction, so concurrent submits
  of two messages for one session cannot both be accepted.
- A message cannot be injected into a turn that is already running: `claude -p` and `codex exec`
  read their prompt once at start. Queue it with `--after` as above.
- Not detected: a session used from another machine, user or configuration directory, or by a
  process that does not register as above. Whether the Codex App holds the writer lock while a
  thread is open but idle, and how an open Claude Code TUI or Codex App window displays a turn added
  by another process, were **not verified**.

## Per-task routes and origin (issue #4)

Register an approved receiver using **references**, never a URL or token value on the command
line. The first registration needs a machine-specific `source_id`. The named environment
variables must already be provided by the service environment; alternatively use `--url-file`
and `--token-file` with existing absolute, owner-only file paths.

```sh
cli-relay --home /path/to/ledger route add github --source-id mac-a \
  --consumer github-bot --chat github-original-chat \
  --url-env GITHUB_RELAY_URL --token-env GITHUB_RELAY_TOKEN
cli-relay --home /path/to/ledger route list
cli-relay --home /path/to/ledger doctor

# Only after the receiver and the visible test message are approved:
cli-relay --home /path/to/ledger route verify github
# A synchronous visible-message receipt with message_id proves it immediately (exit 0).
# A bare 2xx leaves state=awaiting_confirmation (exit 6) and prints verification_id.
# The bot posts: cli-relay 接通测试，无需处理
cli-relay --home /path/to/ledger route confirm github --verification-id v-… --message-id platform-message-id
cli-relay --home /path/to/ledger route set-default github  # optional, proven only
cli-relay --home /path/to/ledger route set-fallback github friday # optional; friday must already be proven

cli-relay --home /path/to/ledger submit --key issue-4 --engine claude --cwd ~/code/app --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --prompt 'Implement the agreed change.' --notify-route github \
  --origin '{"bot":"github-bot","chat":"github-original-chat"}'

cli-relay --home /path/to/ledger route remove github
# Explicit recovery after fixing a failed/exhausted verification:
# cli-relay --home /path/to/ledger route verify github --retry
# Explicit recovery of a failed task notification (never reruns the task):
# cli-relay --home /path/to/ledger notify retry --event e-...
```

Managed registration is idempotent: identical settings keep the receipt; conflicting settings are
refused. New routes are **unproven**, and neither explicit `--notify-route` nor default routing
can use them for new tasks. New routes need Evan's approval and a separate acceptance step with
`route verify`. There is no bypass flag. A duplicate submission still recovers its original
receipt even if the route has since disappeared. Removal clears that default, but keeps tasks,
events and ACKs.

**Upgrade:** leave the live route configuration unchanged. Routes with **no `verification` key**,
including the installer's existing `friday` route, keep working for explicit/default submissions,
delivery and bounded reminders. `doctor` reports `proven: true, proof: legacy_config`; loading
the configuration does not rewrite it. No receipt or installer-template change is needed.
An explicit `verification: null` or pending verification is **not** exempt. Re-registering a
legacy route with `route add`, even with identical settings, drops the exemption; changing its
settings requires remove/add and then verification. Managing another route preserves the
untouched legacy route's exemption.

`--origin` requires exactly `bot` (1–64 characters from `[A-Za-z0-9._:@/+-]`) and `chat`
(1–256 printable characters, no surrounding whitespace), in at most 4096 UTF-8 bytes of JSON.
It appears in the stored payload, hash, submit/resume receipts, status, terminal/unknown event
bodies and each HTTP notification event. Changing it under the same scope/key is a conflict.
It is descriptive metadata; the selected route determines the destination. Omit it to retain
the previous payload/hash and omit the field from outputs.

Default budgets are 6 attempts with backoff of 30, 60, 120, 240, 480 seconds (cap 1800 seconds),
and at most 2 possible wakes, separated by a 1800-second ACK timeout. Unproven task routes consume
bounded readiness checks without sending. Exhaustion keeps the event/receipt for inspection;
only an explicit retry grants a new budget. See the [receiver protocol and commands](notifier.md)
and [D16](decisions.md#d16-issue-4-registered-routes-visible-message-proof-and-origin).

Async confirmation must use the current verification id and a visible chat message id on the
same Mac/ledger, within 24 hours of probe creation. Route, source, consumer, chat and adapter
references must still match. Optional `route confirm --source-id S --ledger-id L --consumer C
--chat LABEL` assert those identities explicitly. Wrong, expired or superseded ids are refused.
Budgets and `next_at` still bound sends; only `route verify NAME --retry` after expiry/exhaustion
creates a new id. Confirmation never sends or ACKs a task. Managed proofs and registrations are
also stored in the ledger, so editing a receipt or removing its verification key cannot prove a
registered route. The current verification id/budget also belongs to the ledger; an old config
snapshot cannot restore a superseded or removed proof. Run `migrate` for existing ledgers; older config-only managed proofs require a
fresh `verify` (and `confirm` for an async receiver). Keep the existing legacy Friday route intact.

`route add … --fallback-route friday` or `route set-fallback NAME friday` selects an existing
proven route other than NAME. `route set-fallback NAME --clear` removes the setting. Once an
original notification has no owner ACK for `reminder.ack_timeout_s` (default 1800 seconds), or
ends `failed`, the next notifier pass attempts one fallback reminder. The fallback is recorded
across restarts and is never repeated, even after `notify retry`. It names the original route,
consumer, chat, event ids, age and reason, and never ACKs for that consumer. `notify status`
includes the fallback state/result. This setting does not change the default route or the
Friday route. Routes with no fallback retain their previous behavior. See [fallback details](notifier.md#fallback-reminders).

This change has only local fake/mock test evidence. No new live webhook route was approved or
verified, and it makes no new 25-hour reliability claim.

## Per-task environment cleanup

### Codex billing default and explicit API selection

Codex defaults to `billing_mode=subscription`. Before configuration/login preflight and process
spawn, cli-relay removes every name in the existing `BILLING_ENV["codex"]` set from a copy of the
task's child environment: `OPENAI_API_KEY`, `CODEX_API_KEY`, `OPENAI_BASE_URL` and
`AZURE_OPENAI_API_KEY`. Empty values are removed too. Their inherited presence no longer causes
`billing_env_present` for Codex. Unrelated variables and the relay/parent environment are preserved;
existing session-marker and test-variable scrubbing still applies. This never selects API billing
or changes credentials, configuration files, launchd or shell profiles. Claude defaults are unchanged.

For a deliberately API-billed Codex task, select it explicitly at submission:

```sh
cli-relay submit --key intentional-api-1 --engine codex --cwd ~/code/app --prompt "Run the tests." --task-class complex --resource-class standard --option 'model="gpt-6.1-sol"' --option 'effort="max"' --option 'sandbox="workspace-write"' \
  --billing-mode api
# Equivalent: --option billing_mode=api
```

This opt-out preserves inherited Codex billing variables, except names explicitly removed by
`--unset-env`, and permits a successful `codex login status` reporting API-key authentication.
It authorizes possible API charges; it does not configure a key or guarantee which billing route
the CLI ultimately uses. A successful ChatGPT login is still accepted. Failed or unknown login,
configuration-layer billing/provider overrides (including a configured API login method),
requirements, permissions and the runtime `openai` provider/sandbox checks still block as before.
This targeted opt-out does not enable arbitrary custom providers or alter Claude guards.
No CLI flag or environment value can implicitly opt a task into API billing.

`--billing-mode subscription` makes the default explicit. Both flag and JSON forms are Codex-only
and accept only `subscription` or `api`; if both are supplied they must agree. The explicit choice
is stored in `options.billing_mode` and participates in the payload hash. Changing it under an
existing scope/key is a conflict; submit a new key instead. Resume has no billing override and
reuses the saved choice on exact resume and explicit fresh rerun.

Compatibility: an omitted stored mode means subscription, including old tasks. Omitting the new
option adds no payload field and keeps existing payload hashes/idempotency behavior. No ledger
migration is required. After upgrading, future attempts of old Codex tasks also use isolation;
already-running children and historical evidence are unchanged. This is a documented default
change for Codex only. Until the upgraded relay is deployed, dispatchers should continue to add
`--unset-env OPENAI_BASE_URL` (and other reported names) to new tasks. It remains supported after
upgrade. A bare `env -u` workaround applies only to that command invocation.

Receipts, task views and terminal events report `billing_mode` and `billing_env_policy`
(`subscription_isolation` or `explicit_api`) for Codex. Once a keeper observes an attempt,
its evidence, keeper log, terminal event and structured notification also report
`billing_env_present`, `billing_env_removed` and `billing_env_preserved`: sorted lists of Codex
variable **names only**, including presence with empty values. Empty lists mean observed absence;
missing snapshot fields mean the keeper never observed the environment. Each attempt takes a new
snapshot, so resume may report different presence while keeping the same policy.
`doctor --engine codex` reports the default subscription policy and its names-only snapshot without starting a task.

### Explicit cleanup (`--unset-env`, unchanged)

`submit --unset-env NAME` removes an inherited billing environment variable for that task only.
Repeat the flag for more names:

```sh
cli-relay submit --key clean-env-1 --engine codex --cwd ~/code/app --prompt "Run the tests." --task-class complex --resource-class standard --option 'model="gpt-6.1-sol"' --option 'effort="max"' --option 'sandbox="workspace-write"' \
  --unset-env OPENAI_BASE_URL --unset-env OPENAI_API_KEY
```

Like `--network-access`, this is a CLI flag mapped to a persisted task option, rather than an
argv option passed to the engine. The equivalent is
`--option 'unset_env=["OPENAI_BASE_URL","OPENAI_API_KEY"]'`. Both forms store a sorted, unique
`options.unset_env` list; duplicate names are harmless. If both forms are supplied, their
normalized lists must agree. Omitting both adds no field and preserves existing payload hashes.
An explicit JSON `[]` is stored as an empty list. Changing the list under an existing scope/key
is a payload conflict, so use a new key when adding it to an already-created task.

Names must match `[A-Za-z_][A-Za-z0-9_]*`; assignments such as `NAME=value` are rejected without
echoing their values. Cleanup is restricted to the union of the engines' billing variables:
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `ANTHROPIC_BEDROCK_BASE_URL`,
`ANTHROPIC_VERTEX_BASE_URL`, `ANTHROPIC_VERTEX_PROJECT_ID`, `OPENAI_API_KEY`, `CODEX_API_KEY`,
`OPENAI_BASE_URL`, and `AZURE_OPENAI_API_KEY`. Any of these names is accepted for either engine
and the fixture. Other names, including HOME, configuration directories and relay controls,
are refused. The option only removes variables; it cannot set values or alter configuration files.

### Preflight

Cleanup happens before the billing/configuration/login checks and in the CLI child environment
on first runs, exact resumes and explicit fresh reruns. The stored list is reused; resume cannot
change it. The relay's own environment, launchd, shell profiles and unrelated tasks are unchanged.
Existing session-marker and test-variable scrubbing still applies. Codex also applies the billing
policy above; an empty `unset_env=[]` does not disable subscription isolation. Explicit cleanup
always wins over `billing_mode=api`. Subscription/configuration guards otherwise still apply.

For Claude without cleanup, `billing_env_present` keeps its reason code and `names` list. Its preflight
details also contain `fix.submit_flags` (for example `--unset-env ANTHROPIC_API_KEY`),
`fix.env_prefix` (`env -u ANTHROPIC_API_KEY`) and a `message` containing both fixes. The prefix goes
before `cli-relay`; flags go on `submit`. The `env -u` prefix applies only to that invocation;
use `--unset-env` to retain cleanup for future resumes. `doctor --engine claude` shows
the same hint when billing variables block it. Blocked terminal events
include these billing preflight details too. The notification summary, which is all a command
adapter receives, ends with the same copyable fix (`billing_env_present fix: submit with a new key
and --unset-env NAME …`), and `brief` JSON events carry the `preflight` object.

Submit/resume receipts, task records and terminal events include `unset_env` only when requested.
Once the keeper observes the attempt's environment, attempt evidence and terminal events also
include `unset_env_present`: the requested names that existed before cleanup, even if their
values were empty. An absent requested name is harmless. Values are never recorded by cleanup.
The snapshot is taken again for each attempt and survives reconciliation; if the keeper never
observed the environment, `unset_env_present` is omitted rather than guessed. Structured
notification event entries carry the same fields. No ledger migration is needed.

## Engines

| Engine | Runs | Resume |
|---|---|---|
| `claude` | `claude -p --output-format stream-json --verbose --session-id <uuid> --permission-mode dontAsk --permission-prompts none` (prompt on stdin) | `--resume <exact uuid>` (same permission mode) |
| `codex` | `codex exec --json -C <cwd> -o <file> --sandbox <s> -`, where new production requests must explicitly select `<s>` (legacy requests default to `read-only`); `workspace-write` or explicitly opted-in `danger-full-access` may be requested. `workspace-write` also gets `-c sandbox_workspace_write.network_access=<n>`: `false` unless the task opts in with `allow_network=true` | `codex exec --sandbox <s> resume -c sandbox_mode="<s>" [-c sandbox_workspace_write.network_access=<n>] --json -o <file> <exact id> -` (same `<s>` and `<n>`) |
| `fixture` | a deterministic local stand-in used by tests and the soak run | same, against its own state |

Never used: `--dangerously-*`, `bypassPermissions`, `--approve-for-me`, `--yolo`, `--full-auto`,
`--continue`, `--last`, `--bare`.
- The Codex sandbox is always passed explicitly, so neither the first run nor a resume takes it
  from the configuration of the moment. The same holds for the network access of `workspace-write`.
- Claude always pins `dontAsk` for the task, including resume. A `bypassPermissions` default
  in any settings layer is rejected; other settings, permission rules and subscription login remain loaded.
  A conflicting managed mode, a stricter `plan` default, malformed checked settings, and a
  missing/mismatched runtime mode still block the run. `dontAsk` permits tools already allowed
  by the existing rules, plus the web tools, scoped writes and Bash prefixes a task opts in to; it is not a
  no-tools mode or a filesystem sandbox.
- Claude: permission prompts that nobody can answer are denied by the CLI. A result that lists
  permission denials ends as `needs_input` unless native success and a known zero exit show the
  run recovered; then the denials are warnings and the completion marker gate still applies.
- Codex: the `codex exec --json` events that cli-relay reads carry no structured permission
  denial. A command refused by the sandbox reaches the model as a failed command, and the
  outcome follows Codex's own result: a run whose commands were refused still ends `succeeded`
  if Codex ended its turn normally. Inspect the separate
  [business status](#business-status), or let the task require the
  [completion marker](#when-the-cli-finished-is-not-the-task-is-done-the-completion-marker).
- The keeper refuses a run (`blocked`) before the CLI starts if it finds any of these:
  - Claude API-key or gateway variables; Codex isolates its billing variables by default
    (see [billing policy](#codex-billing-default-and-explicit-api-selection));
  - a configuration layer that applies to the task directory and routes away from the
    subscription (e.g. a Codex custom provider or base URL, a Claude `apiKeyHelper`);
  - a configuration layer it cannot read;
  - a login that is not a subscription, except successful Codex API-key authentication with
    the task's explicit `billing_mode=api` selection.
- It also stops a Claude run if the CLI itself reports an API-key source.
- After a Codex run, the thread's own record must name the built-in `openai` provider, and
  every turn must have run in the requested sandbox (or a narrower one), without network access
  unless the task opted in, and with no named permission profile. An exact resume also needs a new turn record after its saved pre-run file boundary; old history alone
  cannot verify that attempt. Before the run, a Codex permission setting, requirements layer, or an unsafe or unreadable selected profile blocks it.
- These outcomes do not depend on whether the keeper survived: reconciliation applies the same
  checks. See [`reliability.md`](reliability.md) for the exact layers and limits.

Codex **cannot commit under `workspace-write`**: `.git` is read-only by design, so commands
that need `.git/index.lock` fail even though source files can be edited. Have the caller review
and commit the changes outside that sandbox. There is no option to make just `.git` writable.

For any engine whose task directory is in a Git work tree, the relay records `git` evidence:
`head_before`, `head_after`, and `dirty` (the final tree has staged, unstaged or untracked changes).
It uses read-only argv calls with optional index writes disabled and a two-second timeout per
call. An unavailable HEAD or dirty check is `null`; an undetected/non-Git work tree adds no
fields. The observations appear in `status`/`wait` attempt evidence, the terminal event body,
and notification event entries. A dirty tree also adds `warnings: [{"code": "uncommitted_changes"}]`.
This reports the whole tree, including pre-existing changes; it does not attribute edits to the
task or change its outcome. These observations never enter the request payload or its hash.

Codex **Git repository checks are automatic by default**, including on exact resume:

- Omit `skip_git_repo_check` for **auto**: a read-only argv call to
  `git -C <cwd> rev-parse --is-inside-work-tree` detects whether the task directory is in a
  Git work tree, including a subdirectory. Outside a work tree, the adapter adds
  `--skip-git-repo-check`; inside one, it leaves the Codex argv unchanged. The probe disables
  optional Git writes and has a two-second timeout. Missing Git, a timeout or any failed probe
  is treated as no detected work tree, so the flag is added.
- `--option skip_git_repo_check=true` always passes `--skip-git-repo-check`.
- `--option skip_git_repo_check=false` never passes it, preserving Codex's strict check even
  in a plain temporary directory. When set, the option must be a JSON boolean; strings,
  numbers and `null` are usage errors. Omit the option to select auto.

Each built Codex attempt records `codex_git_repo_check` in `status`/`wait` attempt evidence,
terminal event bodies and notification event entries: `auto_skipped` for an automatic skip,
`explicit_skip` for `true`, and `enforced` when no flag is passed. Detection does not add an
option to the stored request or change its hash. An explicit boolean remains part of the
payload and its hash, as with other options. Sandbox, network and approval settings are
unchanged.

If a failed or blocked Codex attempt still writes `Not inside a trusted directory` to stderr,
its `warnings` include `codex_trusted_directory` with a hint to submit a new task using
`--option skip_git_repo_check=true`. Notifications include the hint too. This can also happen
inside a Git work tree whose directory Codex does not trust; the warning does not change the
attempt's outcome or automatically retry it.

Codex **full access is off by default**. Requesting `sandbox="danger-full-access"` alone is a
usage error. Full access needs a separately authorized, explicit per-task opt-in; computer use
never selects it automatically. Prefer the minimum explicit sandbox for the task.

`allow_full_access` is a typed boolean: use `true`, not the JSON string `"true"` or the number
`1`. It is harmless without `sandbox="danger-full-access"`: the requested sandbox stays in
effect, or defaults to `read-only`. Full access disables the Codex sandbox, allowing commands
to access files outside the task directory and use the network, subject to host permissions.
Only opt in when you accept that risk. Config-layer permission checks, subscription/billing
checks, and the forbidden flags above still apply. `doctor` reports this policy and risk.

The options are stored in the task payload and included in `payload_sha256`; changing the
opt-in under the same key is a payload conflict. Exact resume and fresh reruns reuse the stored
choice. Submit/resume receipts, `status`/`wait` task records, and new terminal/unknown event
bodies report `sandbox` and `full_access`; `status` also exposes the stored `options`.
These fields describe the requested permissions even if the attempt is blocked before spawn.
Notifications carry the choice too; see [`notifier.md`](notifier.md).

Codex **network access in `workspace-write` is off by default, loopback included**. In that sandbox
commands cannot open any network connection, not even to `127.0.0.1`, so a test suite that starts
a local server (for example a fake webhook) fails inside it. On Codex 0.163.0 (macOS Seatbelt) a
bind to `127.0.0.1` fails with `Operation not permitted` in `read-only` and in `workspace-write`, and
works in `workspace-write` with `sandbox_workspace_write.network_access=true`. To allow it for one
task, without full access:

```sh
cli-relay submit --key tests-1 --engine codex --cwd ~/code/app --prompt "Run the full test suite" --task-class complex --resource-class standard --option 'model="gpt-6.1-sol"' --option 'effort="max"' \
  --option 'sandbox="workspace-write"' --option allow_network=true
```

- `allow_network` is a typed boolean (`true`, not `"true"` or `1`) and is accepted only together with
  `sandbox="workspace-write"`. With `read-only`, the default, which never has network access, it is a
  usage error. With `danger-full-access` it is a usage error too: full access already runs without
  the sandbox, network included. No task is created in either case.
- The adapter adds `-c sandbox_workspace_write.network_access=true` to the first run and to every
  exact resume; the sandbox stays `workspace-write`. A `workspace-write` task without the opt-in gets
  `-c sandbox_workspace_write.network_access=false`, so a user, project or profile configuration that
  enables the network does not reach relay tasks. Argv checks accept only the one value built from
  the task's options; no other `-c` override is accepted, and no option passes arbitrary config.
- Codex has no loopback-only setting: the opt-in allows all network access from the sandbox, so
  commands can also reach other hosts and send data out. File writes stay limited to the workspace.
  Only opt in for tasks that need it.
- After the run, every turn in the thread's own record must show no network access unless the task
  opted in. A turn with network access the task did not ask for makes it `blocked`
  (`permission_check.reason: network_not_as_requested`).
- Like every option, it is part of the payload and `payload_sha256` (another value under the same
  key is a conflict) and cannot be changed by `resume`. Submit/resume receipts, `status`/`wait` task
  records, new terminal/unknown event bodies and notifications report `network_access`: `true` for
  `workspace-write` with the opt-in and for opted-in full access, otherwise `false`. It states the
  requested permission, not that a connection was made.
- `doctor` describes this in `codex_sandbox_policy`. `doctor --engine codex --cwd DIR` also reports,
  in `engine.workspace_network`, what the checked configuration layers set for
  `sandbox_workspace_write.network_access`, which is what a direct `codex` run there would get. It
  reports the boolean, layer names and whether every layer was readable, nothing else:

  ```json
  {"config_network_access": true, "set_by_layers": ["user"], "complete": true}
  ```

Scoped Edit/Write and Bash opt-ins are available through `--allow-write-dir` and `--allow-bash`;
Codex also accepts `--network-access` or the `network_access` JSON alias. See the
[README permission examples](../README.md#permission-opt-ins) for syntax, validation and limitations.

### allowed_bash_commands

`--allow-bash 'cp -n a b'` and `--option 'allowed_bash_commands=["cp -n a b"]'` request the same
`Bash(cp -n a b:*)` prefix rule. All rules go into one `--allowedTools` argv element, comma
separated, without a shell. Claude Code itself decides whether a command matches.

**Isolated reproduction (2026-11-08, issue #22).** Claude Code 2.1.312, `dontAsk`, one fresh
non-Git directory per case, a minimal environment without billing or session variables, and
the argv from `cli_relay.engines.build` wherever cli-relay can express the rule. The user's
`~/.claude/settings.json` (its allow list, `bypassPermissions` default, hooks and plugins) was
excluded with `--setting-sources project,local`; an empty `HOME` cannot be used on macOS because it
loses the keychain login. Each case asked the model to run one exact command once; 60 real runs.
Nine cases repeated with the user's settings loaded gave the same results, so that configuration
is not a factor.

| Result | Commands |
|---|---|
| Denied under every scoped rule: `Bash(<cmd>:*)` as cli-relay builds it, the exact `Bash(<cmd>)`, `Bash(cp:*)`, `Bash(cp -n:*)`, `Bash(cp *)`, `Bash(cp -n *)`, and with `Edit(./**)`/`Write(./**)` added | `cp -n a.example a.txt`, `cp -p …`, `cp -f …`, `cp -r adir bdir`, `cp -n .env.example .env`, `cp -n a.example out/c.txt` (with `allowed_write_dirs=["out"]`), `mv -n x.example y.txt` |
| Allowed by an unrestricted `Bash` rule only | the same `cp -n a.example a.txt` |
| Allowed by its `Bash(<cmd>:*)` rule | `cp a.example b.txt`, `cp .env.example .env`, `mv x.example y.txt`, `rm -f x.example`, `rm x.example`, `mkdir -p k/l`, `ln -s …`, `ln -sf …`, `sed -i.bak s/x/y/ x.example`, `chmod -R u+w adir`, `./tool.sh hello`, `cp <task cwd>/a.example <task cwd>/o2.txt`, and `test -e a.txt \|\| cp a.example a.txt` with rules for both parts |
| Denied under its exact rule: a path outside the task directory | `cp <outside>/a.example <outside>/o1.txt`, `cp ../outside/a.example o6.txt`, `ls -la <outside>/a.example`, `cat <outside>/a.example`, `mkdir <outside>/m`, `touch <outside>/t.txt`, `mv <outside>/a.example <outside>/b.txt`, `rm <outside>/a.example` |
| Allowed although outside | `python3 <outside>/s.py`; the outside `cp` when the directory is added with `claude --add-dir` (cli-relay never passes it) |
| Without any rule | `ls -la a.example` allowed (read-only); `cp a.example b.txt`, `cp -n …`, `mkdir -p k/l`, `touch d.txt` and `./tool.sh hello` denied |
| Write tool under `Edit(./cfg/**)`/`Write(./cfg/**)` | `cfg/.env`, `cfg/.env.local` and `cfg/notes.txt` all allowed |

So the relay's rule construction is not at fault: multi-word rules match, and for `cp`/`mv` with a
flag no scoped spelling helps. Claude Code does not protect `.env`-like files specially either: the
flag decides. Two Claude Code checks explain every denial: `cp`/`mv` called by bare name with any
flag, and file commands called by bare name with a path outside the working directories. Both
apply even when a rule matches.

The relay keeps the rule construction, settings and permission mode unchanged, and offers no
unrestricted Bash grant. Workarounds: use `cp <src> <dst>` without flags (remove `-n` only when
overwriting the destination is acceptable), or run `test -e <dst> || cp <src> <dst>` and allow the
two prefixes `test -e <dst>` and `cp <src> <dst>` (an existence check followed by a copy is not
atomic). For paths outside the task directory, submit with a `--cwd` that contains them.

Caution: the same commands called by absolute path were allowed under their rules
(`/bin/cp -n a.example a.txt`, `/bin/mv -n x.example y.txt`, and even `/bin/cp ../outside/a.example
o8.txt`), because Claude Code applies those two checks to the bare names only. cli-relay does not
recommend this: a prefix such as `/bin/cp …` then reaches outside the task directory with nothing
but the prefix rule in the way.

- Submit receipts include one `{"code": "bash_rule_not_honoured", "command": "cp -n a b", "hint": "…"}`
  per affected entry: its first word is `cp` or `mv` and a later token starts with `-`. Other
  commands with flags (`rm -f`, `mkdir -p`, `ln -s`, `sed -i`) were allowed and do not warn.
- Submit receipts include one `{"code": "bash_rule_path_outside_cwd", "command": "…", "paths": […],
  "hint": "…"}` per entry whose first word is `cat`, `cp`, `ls`, `mkdir`, `mv`, `rm` or `touch` and
  that has a path argument resolving outside the task cwd, `..` and symlinks included. `paths`
  lists those arguments. Interpreters such as `python3` and absolute binary paths are not checked.
- The hints name the observed Claude version and workarounds; the observations may change with
  another Claude Code version. Submission still succeeds; the warnings are receipt metadata and do
  not change the stored payload or its hash.
- After a Claude run, structured Bash denials whose stripped `tool_input.command` equals a
  requested prefix or starts with that prefix plus a space add one
  `{"code": "allowed_rule_denied", "commands": ["cp -n a b"]}` warning. This applies to any
  requested Bash prefix. The diagnostic copies only matching requested prefixes, never runtime
  arguments added to them. Prefixes are matched before truncation, then each is limited to 200
  characters; the displayed list is unique and sorted. No tool-input fields are copied into
  the diagnostic or native summary. Missing command details and older name-only denials add
  no diagnostic.

`allowed_rule_denied` follows the existing warnings path through `status`/`wait` attempt
evidence, terminal events and notification entries. The notifier summary includes the command
prefix count and first prefix. These diagnostics do not change the outcome, grant permissions or
retry a task. An inherited deny or ask rule can also explain a matching denial.

### Claude web tools

Claude **adds no tool by default**. Under `dontAsk`, a tool that needs permission and is not
already allowed by your settings is denied. A native failure with a permission denial ends
`needs_input`. If Claude reports native success and exits 0, denials are warnings and the outcome
is `succeeded`, subject to the completion marker gate below. Without a confirmed zero exit,
denials still mean `needs_input`; cancellation keeps its existing precedence. `status`/`wait`
attempt evidence, terminal events and notification event entries carry, for example,
`warnings: [{"code": "permission_denials", "tools": ["Monitor"]}]`. This warning includes only
tool names; the separate `allowed_rule_denied` diagnostic can include matching Bash prefixes
requested by the user as described above. `Monitor` is not added to the allowlist. To allow
`WebSearch` or `WebFetch` for one task, name them:

```sh
cli-relay submit --key research-1 --engine claude --cwd ~/code/app --prompt "Compare the current options for X" --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --option 'allowed_tools=["WebSearch","WebFetch"]'
```

`allowed_tools` is a JSON list of exact tool names from a fixed allowlist: `TodoWrite`,
`WebSearch` and `WebFetch` (any subset is fine). To opt into todo tracking, use
`--option 'allowed_tools=["TodoWrite"]'`. This adds no Bash, Edit or Write grant.
A plain string such as `"WebSearch"`, any other tool, a
rule pattern such as `WebFetch(domain:example.com)` or `Bash(curl *)`, a wildcard, or a repeated
name is a usage error, and no task is created. The list is stored sorted, so its order is not
part of the request.

`Task` and `Agent` opt-ins are deliberately not enabled. Custom subagent definitions can
override a parent's `dontAsk` mode; sharing a Claude session does not prove the same command
and write-directory limits. The [investigation](claude-subagents.md) explains the official
documentation, current limits and proposed acceptance gate. This option is an approval list,
not a switch that removes Claude's native tools; a rejected name is not evidence that the
tool is absent from the underlying CLI.

- The adapter adds `--allowedTools WebFetch,WebSearch` (only the named tools) for that task,
  including exact resume and fresh reruns. Nothing else changes: the permission mode stays
  `dontAsk`, no `--settings` layer is added and no settings file is written. Argv checks accept
  `--allowedTools` only with the task's stored list.
- Claude's own rules still decide. It evaluates deny, then ask, then allow rules, so a deny or
  ask rule in any settings layer wins over the option. A managed deny cannot be overridden by
  `--allowedTools`, and managed `allowManagedPermissionRulesOnly` ignores `--allowedTools`
  altogether. A denial that the task does not recover from still ends it `needs_input`.
- `WebFetch` and `WebSearch` reach the network: fetched pages are untrusted input, and a fetched URL can carry
  data out of the task. Only opt in for tasks that need it.
- Like every option, `allowed_tools` is part of the payload and `payload_sha256`, so another
  list under the same key is a payload conflict. `resume` cannot change it either: to rerun a
  task that ended `needs_input` with the tools, submit it again under a new key with the
  option. Submit/resume receipts, `status`/`wait` task records, new terminal/unknown event
  bodies and notifications report `allowed_tools` (`[]` by default). Like `sandbox`, it states
  the requested permissions, not proof that a tool ran.
- `doctor` describes this policy in `claude_tool_policy`. `doctor --engine claude --cwd DIR`
  also reports, in `engine.network_tools`, what this machine's checked settings layers already
  say about these tools, before any task option. It reports tool names and booleans only, never
  other settings content:

  ```json
  {"allowed_by_default": false, "default_allowed": [], "ask": [], "denied": ["WebFetch"],
   "managed_rules_only": false, "complete": true}
  ```

  `default_allowed` lists tools that a bare allow rule allows and no ask or deny rule names.
  `ask` and `denied` list tools that an ask or deny rule names, even if the rule only covers
  some calls (for example `WebFetch(domain:…)`); the option cannot lift those. If
  `managed_rules_only` is true, the option has no effect on this machine. If `complete` is
  false, a checked layer could not be read, so a rule may be missing from the lists.

### Production tiers and effective settings

New production submits require an explicit task/resource class, engine/model/effort and Codex
sandbox. See the [routing table and migration rules](migration-20261106.md#new-production-requests).
Use `simple` + Codex Terra/high, `complex` + Codex Sol/max (including computer use), or
`highest_quality` + either Claude Opus 5.5/max or Claude Fable 5.2/xhigh (Extra High).
Unknown/deprecated models and mismatches are usage errors.
Only `engine=fixture` has a test-only exemption. No defaults or global settings are changed.

`effective_settings` appears in submit/status/events/brief/notification records. It records
adapter invocation settings with `verification=invocation_only`, not a provider-side observation.
Legacy duplicates and resumes retain their payload and hash; new requests use the new contract.

### Business status

Process outcome, business completion and artifact existence are separate fields. Canonical
producer JSON uses `status=ok|partial|failed` and a separate
`stop_reason=null|blocked|needs_user|stopped_for_approval`. The final stdout line must be
`status=<same value>`. Legacy stop aliases map to partial with a separate stop, never ok.
Unknown/missing/malformed reports remain visible/unreported. No status is inferred from prose.

All engines support `--option 'result_file="out/result.json"'`,
`--option 'required_artifacts=["report.md"]'` and `--option require_business_status=true`.
The result must be a bounded JSON object in the task cwd. Declared artifacts are checked for
local existence; this does not validate their content. Invalid/missing results cannot be replaced
by a success marker or an alternate root result.json. Permission denials do not invalidate every
output, but prevent an assumed complete result.

Per-attempt snapshots/hash and terminal-time artifact checks survive reused paths and keeper
crashes after exit.json. Brief never refreshes historical file existence. Historical rows are
readable without rewriting them. Full alias/precedence, diagnostics, snapshot limits and recovery
rules are in the [result contract](migration-20261106.md#result-contract).

### When "the CLI finished" is not "the task is done": the completion marker

`succeeded` means that the CLI ended its turn without an error (and exited 0). It does not mean
that the task's goal was reached: a test run whose tests could not start still ends `succeeded` when
the CLI writes that into its result and finishes normally. A dispatcher that needs more can require
the CLI's own completion report for a task, on any engine:

```sh
cli-relay submit --key tests-2 --engine codex --cwd ~/code/app --task-class complex --resource-class standard --option 'model="gpt-6.1-sol"' --option 'effort="max"' \
  --option 'sandbox="workspace-write"' --option allow_network=true --option require_completion_marker=true \
  --prompt "Run the full test suite and fix what fails. End your final message with the line
CLI-RELAY-STATUS: complete if everything asked is done and verified, otherwise with the line
CLI-RELAY-STATUS: incomplete."
```

- With the opt-in, a run that would end `succeeded` does so only if the last non-empty line of the
  CLI's final message is exactly `CLI-RELAY-STATUS: complete` (surrounding whitespace aside).
  `CLI-RELAY-STATUS: incomplete`, any other last line (backticks, bold or a trailing period included)
  and no final message end it `needs_input`. `failed`, `blocked`, `cancelled` and the `needs_input` of
  an unresolved Claude permission denial are never changed: the marker cannot turn a failure into a success.
- Ask for the line in the task prompt, and again in resume prompts; the relay appends its separate completion self-report requirement.
  Each attempt is judged by its own final message, so a `needs_input` task can be continued with
  `resume`.
- The attempt's evidence records the check, for example `"completion_check": {"marker":
  "incomplete", "downgraded_from": "succeeded"}`. Receipts, task records, event bodies and
  notifications carry `completion_marker_required`; when it is `true`, event bodies and notifications
  also carry `completion_marker` (`complete`, `incomplete` or `null`), because the line is usually
  past the result excerpt. The option is part of the payload and its hash.
- It is the CLI's self-report, not a verification: the marker check reads no other text of the
  result and runs no verification command, so a CLI can still claim `complete` wrongly. Without the opt-in nothing
  changes. `doctor` describes the contract in `completion_marker_policy`.

## How a consumer (e.g. a chat bot calling over SSH) should use it

1. Submit with a key derived from the request. If the reply is lost or uncertain, submit the
   same key again: you get the same task, and nothing runs twice.
2. Wait with a bounded `wait`, or poll `status`. A timeout is not a failure. Polling is also
   the recovery path: a task whose keeper never started is started by `status`, `events` or
   `wait` once it has been pending for 5 s.
3. Read `events --consumer <you>`. After the result has reached its destination, `ack` the event.
   Until then the event is delivered again on every read, with the same id. `succeeded` only says
   that the CLI finished without an error. Check `business_status` and `denials` for the reported
   work result; `unreported` makes no completion claim. Tasks can also require the completion
   marker (above) so that a run the CLI itself reports unfinished ends `needs_input`.
4. If a task is `unknown` (the relay could not prove what happened), a human decides:
   - `resume` continues the exact native session;
   - `resume --fresh --confirm-rerun` starts over and may repeat side effects.

Since v0.2 the notifier wakes the consumer when a task ends, so it does not have to poll. It uses
the machine's existing notifier script or a webhook, with bounded retries and per-event
deduplication. The consumer still reads the task by id and ACKs it after reporting. What a
wake-up means, and does not mean, is in [`notifier.md`](notifier.md). Whether a chat,
a phone or a person ever sees the result is outside what the relay can know.

### Direct delivery after a bot's own route wakes it

1. On the named Mac run `cli-relay --home /path/to/ledger brief --consumer C --route R --text`
   (omit `--text` for JSON). Optional `--task T` restricts it to one task.
2. Send all printed message lines together as one visible message to the route's chat.
3. Obtain that message's platform message id.
4. Execute the printed batch `ack` command after replacing `<MESSAGE_ID>` with that id.

One message line per event is computed by the tool, for example:

```text
CLI_RELAY mac-a 12345678/90abcdef article：部分完成 | process=succeeded business=partial stop_reason=None artifacts=not_declared | /absolute/task/cwd/out/result.json
```

The conclusion uses outcome and business status; an unreported status makes no completion claim.
JSON has `events[]` with full `event_id`, `task_id`, `attempt_no`, `ledger_id`, `source_id`,
`task_last8`, `event_last8`, `key`, `origin`, `outcome`, `business_status`,
`business_status_source`, `warnings`, `result_file` (object above or null) and `message`;
billing-blocked events also carry `preflight` (names and the copyable fix, never values).
`ack_command` is shell-quoted for the selected home and all listed event identities. No
text-mode ids need to be copied by hand: each `CLI_RELAY` line is preceded by its full metadata
as `field=value` pairs, and the final line is the batch command. Send only the `CLI_RELAY` lines.
Control characters in a display path are escaped to keep its message on one line. No
unacknowledged terminal events means `events: []`, `ack_command: null`, and empty text output.
Brief includes events owned by C and events without a known consumer; it excludes other consumers'
bound events and informational `waiting` events. It reads committed state only: no reconciliation,
owner binding, send, ACK or delivery-count write. It exports no credentials, task prompt or native
result text. Run `migrate` before using brief on an old ledger, so its printed message-id ACK can
execute. Use `status` separately if more evidence is needed.

`ack --message-id M [--chat C]` adds an optional delivery receipt. `events --include-acked` shows
`message_id` and `chat` for that consumer; `status` keeps `acked_by` and adds an `acks[]` list
containing consumer, time, message id and chat per event. The first ACK wins: duplicate ACKs do
not replace its time or receipt. Historical ACKs have null message/chat. The ledger, specifically
`acks.message_id`, is the record of delivery; no separate registry is needed.

The original single-event command keeps working. The batch form is:

```sh
cli-relay --home /path/to/ledger ack --consumer bot --events \
  '[{"task_id":"t-…","attempt_no":1,"event_id":"e-…","ledger_id":"l-…","source_id":"mac-a","consumer":"bot"}]' \
  --message-id MESSAGE_ID --chat original-chat
```

Use the exact JSON printed by `brief`. Each reference requires those six fields; `source_id`
is null for an unconfigured ledger. Every reference must match the current ledger/source and
the command's consumer, and its event must belong to its task/attempt. Like single-event ACKs,
consumer records are independent. All events commit in **one transaction** or none commit;
duplicates return `duplicate: true` per entry and are harmless. Batch output is `events[]`
with a result per input entry. `--events` cannot be mixed with `--task/--attempt/--event`.
Optional top-level `--ledger-id` and `--source-id` remain extra assertions for either form.

## Limits

- No exactly-once guarantee for external side effects. The relay never re-runs on its own,
  but an explicit resume or re-run can repeat a last step.
- Single host; the ledger must be on a local disk. Windows is unsupported.
- Machine sleep, power loss or reboot interrupt running CLIs. Afterwards the ledger is readable,
  open attempts are reconciled (usually to `unknown`), and sessions can be resumed explicitly.
- If the keeper is killed, the CLI's exit status cannot be known afterwards. The attempt
  is closed from the CLI's own final result with `exit.known=false`, or marked `unknown`.
- A CLI that leaves its process group *and* whose pid was never recorded (a microsecond window
  after spawn) cannot be seen after a crash; see [`reliability.md`](reliability.md).
- Results are never deleted automatically. Disk usage grows with history. Clean-up is a
  deliberate, manual decision.
- The subscription check reads the CLIs' configuration files itself, not through their own
  resolvers. A routing setting it does not know about is not detected before the run.
- The run's own provider record is checked for Codex only. It is written when a thread is
  created, so a resumed Codex turn relies on the preflight check.
- Notifications are at least one attempt and at most a bounded number, not exactly once.
  - A lost webhook response or a reminder can wake the consumer twice for one event.
  - A wake-up that the receiver accepted may still never produce a chat message.
  - `notify.sh`-style programs that always exit 0 tell the notifier nothing about the HTTP
    result.
- A notification that is due while the machine sleeps or is off is sent by the next pass after
  it comes back.

## Tests and acceptance

```sh
python3 -m unittest discover -s tests -t tests -v   # fixture engine, real OS processes, local fake webhook, ~4 min
python3 scripts/soak.py run --dir /tmp/soak --hours 0.03                   # scaled-down soak
python3 scripts/soak.py start --dir ~/soak-cli-relay --hours 25            # the real 25 h soak
python3 scripts/soak.py verdict --dir ~/soak-cli-relay                     # re-check a finished soak
python3 scripts/real_cli_acceptance.py --engine claude --notify --home DIR --workdir DIR --out result.json
```

`soak.py run`, `soak.py verdict` and `real_cli_acceptance.py` exit with status 0 only on PASS.
`real_cli_acceptance.py --engine fixture` runs the same loop without a model; the test suite
uses it.

The fault-injection tests kill real processes (`SIGKILL`) at each crash window. They also
simulate SSH session teardown, check concurrent duplicate submits, and exercise lost, repeated
and mismatched ACKs, cancel/complete races and storage failures. See
[`acceptance.md`](acceptance.md) for the matrix and the latest recorded results. See
[`reliability.md`](reliability.md) for the state machine, crash windows and the
preconditions of every guarantee.

### Completion self-report

Every new submit and explicitly resumed or fresh attempt appends the same requirement to the
engine prompt. The final response must contain exactly one line for each label, with a full-width
colon and a nonempty value (at most 512 printable characters):

```text
做完没：部分
还差：审核
下一步谁动：Evan
```

`做完没` accepts only `做完`, `没做完`, or `部分`; use `无` for nothing remaining and `没有`
when no one needs to act. If a JSON result file is requested, the producer should also write
string fields `done`, `remaining`, and `next_owner` with the same values. The final response's
three lines are the authoritative source for `completion_report` in terminal evidence, events,
briefs, and notifications; the result file alone does not satisfy this check. A missing, empty,
invalid, or duplicated line produces `{"state":"没说做完没"}`. Historical events without the
field read as missing; no history is rewritten. Self-reports are not independently verified.
Command-adapter notification summaries include all three reported values outside excerpt
truncation, so the remaining work and owner are also visible without reading the JSON payload.

This report does not change the process outcome, business status, dependency rules, or cause a
retry. In particular, `succeeded` and business `ok` are not proof that the task is done; consumers
must inspect `completion_report`. The existing final `status=...` marker, if used, can follow the
three lines. Prompt augmentation does not alter the submitted payload hash or existing attempts.

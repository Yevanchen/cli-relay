# Usage

Reference for running cli-relay: commands and exit codes, per-task notification routes, engines
and task options, how a consumer should use the ledger, limits, and tests. The
[README](../README.md) has the overview and a quick start.

## Commands

```sh
pip install .            # or run in place: PYTHONPATH=. python3 -m cli_relay ...
cli-relay init           # ledger in $CLI_RELAY_HOME or ~/.cli-relay

# submit under an idempotency key; the CLI runs detached from this shell
cli-relay submit --key fix-tests-42 --engine claude --cwd ~/code/app \
  --prompt "Run the unit tests and summarise failures."
# {"accepted": true, "status": "created", "task_id": "t-…", "native_session_id": "…", "state": "accepted", …}

cli-relay submit --key fix-tests-42 --engine claude --cwd ~/code/app \
  --prompt "Run the unit tests and summarise failures."     # same key + payload: same task
# {"status": "duplicate", "task_id": "t-…", "submit_count": 2, …}

cli-relay wait t-… --timeout 600   # a timeout only stops waiting; the task keeps running
cli-relay events --consumer grok   # terminal events not yet ACKed by this consumer
cli-relay ack --consumer grok --task t-… --attempt 1 --event e-…
cli-relay status t-…               # full record: attempts, identities, exit status, native result
cli-relay resume t-… --prompt "Continue with the next failing test."   # exact native session
cli-relay cancel t-…

# v0.2: wake a consumer when tasks end (configuration in <home>/notifier.json, see docs/notifier.md)
cli-relay migrate                  # once per existing v0.1 ledger: backup, then add the notifier tables
cli-relay submit --key k --engine claude --cwd ~/code/app --prompt "…" --notify-route bot
cli-relay notify run --interval 60 # periodic pass (retries, reminders, catch-up after a restart)
cli-relay notify status            # per event: transport state and consumer ACK
```

Every command prints exactly one JSON object on stdout. Only `--help` and `--version` print
plain text. Exit codes:

- `0` ok
- `1` internal error (`"error": "internal"`)
- `2` usage
- `3` key conflict
- `4` not found
- `5` ledger or storage error
- `6` refused (a precondition does not hold)
- `130` interrupted

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
# Exit 0 / proven=true only after a matching visible-message receipt with message_id.
# Exit 6 leaves it unproven; inspect verification.next_at before invoking again.
cli-relay --home /path/to/ledger route set-default github  # optional, proven only

cli-relay --home /path/to/ledger submit --key issue-4 --engine claude --cwd ~/code/app \
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

This change has only local fake/mock test evidence. No new live webhook route was approved or
verified, and it makes no new 25-hour reliability claim.

## Engines

| Engine | Runs | Resume |
|---|---|---|
| `claude` | `claude -p --output-format stream-json --verbose --session-id <uuid> --permission-mode dontAsk --permission-prompts none` (prompt on stdin) | `--resume <exact uuid>` (same permission mode) |
| `codex` | `codex exec --json -C <cwd> -o <file> --sandbox <s> -`, where `<s>` defaults to `read-only`; `workspace-write` or explicitly opted-in `danger-full-access` may be requested. `workspace-write` also gets `-c sandbox_workspace_write.network_access=<n>`: `false` unless the task opts in with `allow_network=true` | `codex exec --sandbox <s> resume -c sandbox_mode="<s>" [-c sandbox_workspace_write.network_access=<n>] --json -o <file> <exact id> -` (same `<s>` and `<n>`) |
| `fixture` | a deterministic local stand-in used by tests and the soak run | same, against its own state |

Never used: `--dangerously-*`, `bypassPermissions`, `--approve-for-me`, `--yolo`, `--full-auto`,
`--continue`, `--last`, `--bare`.
- The Codex sandbox is always passed explicitly, so neither the first run nor a resume takes it
  from the configuration of the moment. The same holds for the network access of `workspace-write`.
- Claude always pins `dontAsk` for the task, including resume. This overrides a user/project
  bypass default while retaining their settings, permission rules and subscription login.
  A conflicting managed mode, a stricter `plan` default, malformed checked settings, and a
  missing/mismatched runtime mode still block the run. `dontAsk` permits tools already allowed
  by the existing rules, plus the `allowed_tools` a task opts in to (see below); it is not a
  no-tools mode or a filesystem sandbox.
- Claude: permission prompts that nobody can answer are denied by the CLI. A result that lists
  permission denials ends as `needs_input` unless native success and a known zero exit show the
  run recovered; then the denials are warnings and the completion marker gate still applies.
- Codex: the `codex exec --json` events that cli-relay reads carry no structured permission
  denial. A command refused by the sandbox reaches the model as a failed command, and the
  outcome follows Codex's own result: a run whose commands were refused still ends `succeeded`
  if Codex ended its turn normally. Read the result text, or let the task require the
  [completion marker](#when-the-cli-finished-is-not-the-task-is-done-the-completion-marker).
- The keeper refuses a run (`blocked`) before the CLI starts if it finds any of these:
  - API-key or gateway variables (only their names are reported);
  - a configuration layer that applies to the task directory and routes away from the
    subscription (e.g. a Codex custom provider or base URL, a Claude `apiKeyHelper`);
  - a configuration layer it cannot read;
  - a login that is not a subscription.
- It also stops the run if the CLI itself reports an API-key source.
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

Codex **full access is off by default**. Requesting `sandbox="danger-full-access"` alone is a
usage error. To opt in for one task, supply both options:

```sh
cli-relay submit --key full-access-1 --engine codex --cwd ~/code/app --prompt "Run the maintenance task" \
  --option 'sandbox="danger-full-access"' --option allow_full_access=true
```

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
cli-relay submit --key tests-1 --engine codex --cwd ~/code/app --prompt "Run the full test suite" \
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

Claude **adds no tool by default**. Under `dontAsk`, a tool that needs permission and is not
already allowed by your settings is denied. A native failure with a permission denial ends
`needs_input`. If Claude reports native success and exits 0, denials are warnings and the outcome
is `succeeded`, subject to the completion marker gate below. Without a confirmed zero exit,
denials still mean `needs_input`; cancellation keeps its existing precedence. `status`/`wait`
attempt evidence, terminal events and notification event entries carry, for example,
`warnings: [{"code": "permission_denials", "tools": ["Monitor"]}]`. Only tool names are included,
not inputs; `Monitor` is not added to the allowlist. To allow `WebSearch` or `WebFetch` for one
task, name them:

```sh
cli-relay submit --key research-1 --engine claude --cwd ~/code/app --prompt "Compare the current options for X" \
  --option 'allowed_tools=["WebSearch","WebFetch"]'
```

`allowed_tools` is a JSON list of exact tool names from a fixed allowlist: `WebSearch` and
`WebFetch` (either one alone is fine). A plain string such as `"WebSearch"`, any other tool, a
rule pattern such as `WebFetch(domain:example.com)` or `Bash(curl *)`, a wildcard, or a repeated
name is a usage error, and no task is created. The list is stored sorted, so its order is not
part of the request.

- The adapter adds `--allowedTools WebFetch,WebSearch` (only the named tools) for that task,
  including exact resume and fresh reruns. Nothing else changes: the permission mode stays
  `dontAsk`, no `--settings` layer is added and no settings file is written. Argv checks accept
  `--allowedTools` only with the task's stored list.
- Claude's own rules still decide. It evaluates deny, then ask, then allow rules, so a deny or
  ask rule in any settings layer wins over the option. A managed deny cannot be overridden by
  `--allowedTools`, and managed `allowManagedPermissionRulesOnly` ignores `--allowedTools`
  altogether. A denial that the task does not recover from still ends it `needs_input`.
- These tools reach the network: fetched pages are untrusted input, and a fetched URL can carry
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

Model and effort can be pinned per task, without changing either CLI's global configuration:

```sh
cli-relay submit --key review-claude-1 --engine claude --cwd ~/code/app --prompt "Review the change" \
  --option 'model="claude-opus-5-5"' --option 'effort="max"'
cli-relay submit --key review-codex-1 --engine codex --cwd ~/code/app --prompt "Review the change" \
  --option 'model="gpt-6-astra"' --option 'effort="max"'
```

`effort` accepts `low`, `medium`, `high`, `xhigh`, or `max`; the chosen model and installed CLI
must support it. Omit it to retain the CLI's default. Exact resume reuses the task's model and
effort. Claude gets `--effort` plus a generated one-key `--settings` environment override, since
`CLAUDE_CODE_EFFORT_LEVEL` takes precedence over `--effort`; Codex gets the allowlisted
`model_reasoning_effort` config override. No arbitrary settings or flags are accepted. Managed
effort constraints remain in force: a conflicting managed environment value or a lower
`maxEffortLevel` in any checked settings layer blocks before spawn;
server-delivered caps and the actual served model still require real-CLI acceptance evidence.
These options record requested configuration, not proof of the provider's effective effort.

### When "the CLI finished" is not "the task is done": the completion marker

`succeeded` means that the CLI ended its turn without an error (and exited 0). It does not mean
that the task's goal was reached: a test run whose tests could not start still ends `succeeded` when
the CLI writes that into its result and finishes normally. A dispatcher that needs more can require
the CLI's own completion report for a task, on any engine:

```sh
cli-relay submit --key tests-2 --engine codex --cwd ~/code/app \
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
- The relay never edits a prompt: ask for the line in the task prompt, and again in resume prompts.
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
   that the CLI finished without an error; for tasks such as test runs, require the completion
   marker (above) so that a run the CLI itself reports unfinished ends `needs_input`.
4. If a task is `unknown` (the relay could not prove what happened), a human decides:
   - `resume` continues the exact native session;
   - `resume --fresh --confirm-rerun` starts over and may repeat side effects.

Since v0.2 the notifier wakes the consumer when a task ends, so it does not have to poll. It uses
the machine's existing notifier script or a webhook, with bounded retries and per-event
deduplication. The consumer still reads the task by id and ACKs it after reporting. What a
wake-up means, and does not mean, is in [`notifier.md`](notifier.md). Whether a chat,
a phone or a person ever sees the result is outside what the relay can know.

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

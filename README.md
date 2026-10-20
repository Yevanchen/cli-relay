# cli-relay

**Experimental (v0.2.0, in review on the `v0.2.0` branch).** A thin local ledger for tasks run by
the official coding CLIs (Claude Code, Codex) on the machine where they are installed. It adds
three things the CLIs do not do by themselves, plus, since v0.2, a notifier:

1. **No blind re-run for a repeated request key.** The same `(scope, key)` returns the same
   task. The same key with a different payload is an explicit conflict.
2. **Reconciliation after a disconnect.** Every attempt durably records its host, cwd,
   keeper/CLI process identity (pid + OS start time), native session id, exit status and the
   CLI's own result. What cannot be proven is reported as `unknown`, never guessed.
3. **Results kept until the consumer ACKs them.** The terminal state and a stable event id are
   committed in one transaction. They are re-delivered until a named consumer ACKs, and are
   never expired or garbage-collected.
4. **A wake-up when a task ends (v0.2).** A local notifier wakes the consumer through an existing
   notifier script or webhook, for example a Grok Bot routine. It does this for `succeeded`,
   `failed`, `blocked`, `needs_input` and `unknown`.
   - Retries are bounded, and wake-ups are deduplicated per event.
   - After a restart it sends what is still due.
   - The transport result is never taken for the consumer's ACK.
   - See [`docs/notifier.md`](docs/notifier.md).

Everything else stays native: the CLIs do the work, own the sessions, apply their own
permission rules and use your existing subscription login. cli-relay is not a model proxy, an
agent harness, a networked relay, a terminal runtime or an orchestrator.
[`docs/decisions.md`](docs/decisions.md) maps what the CLIs and existing projects (Reason
Network, Agenvo, AGPair) already cover. It also explains why this is a small separate
ledger rather than a fork.

## Verification status (v0.2.0 branch)

The 156-test and real-CLI rows below describe candidate `d27b849`, before the
independent review fixes. Revised-code checks are described in `docs/acceptance.md` §2.5 and recorded on [PR #2](https://github.com/Yevanchen/cli-relay/pull/2);
passing the earlier candidate does not certify the revised code or a live deployment.

| Evidence | Status |
|---|---|
| Fault-injection suite + notifier scenarios (fixture engine, real OS processes, local fake webhook; macOS) | PASS, 156/156 in each of 2 consecutive rounds |
| Regression tests for the independent audit of v0.1.0 (each failed before its fix) | PASS |
| Real Claude Code loop with the notify step (local fake webhook) | PASS, 2/2 runs |
| Real Codex loop with the notify step (local fake webhook) | PASS, 2/2 runs (every turn read-only, no named permission profile) |
| CI on `ubuntu-latest`, Python 3.11 / 3.12 / 3.14 | runs on the pull request |
| Two-machine deployment with the real Bot | **NOT RUN** (pending deployment) |
| A real 30-minute task window with the real Bot | **NOT RUN** (pending deployment) |
| 25 h soak of this version | **Not completed. Does not block this controlled rollout** |
| 25 h soak of `b72618d` (v0.1 before both audits) | Historical run; current status not rechecked. It says nothing about this version |
| C4 real bot/SSH entry point, F5 real disk-full, J1 sleep/power loss/reboot | **NOT RUN** |

Not promised:
- exactly-once external side effects;
- that any notification reaches a chat, a phone or a person;
- that a process keeps running across sleep, power loss or reboot.

The matrix, numbers and limits are in [`docs/acceptance.md`](docs/acceptance.md).

## Requirements

- macOS or Linux, Python ≥ 3.11 (standard library only), a local filesystem for the ledger.
- For real runs: `claude` and/or `codex` installed and logged in **with a subscription**
  (`claude auth status` → `claude.ai`; `codex login status` → ChatGPT). API-key routes are refused.

## Minimal example

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
  permission denials ends as `needs_input`, even if the CLI also reports an error.
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
  checks. See [`docs/reliability.md`](docs/reliability.md) for the exact layers and limits.

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
Notifications carry the choice too; see [`docs/notifier.md`](docs/notifier.md).

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
already allowed by your settings is denied, so a research task that tries `WebSearch` or
`WebFetch` ends `needs_input` with that permission denial. To allow them for one task, name them:

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
  altogether. A tool that is still denied ends the task `needs_input`, as before.
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
  a Claude permission denial are never changed: the marker cannot turn a failure into a success.
- The relay never edits a prompt: ask for the line in the task prompt, and again in resume prompts.
  Each attempt is judged by its own final message, so a `needs_input` task can be continued with
  `resume`.
- The attempt's evidence records the check, for example `"completion_check": {"marker":
  "incomplete", "downgraded_from": "succeeded"}`. Receipts, task records, event bodies and
  notifications carry `completion_marker_required`; when it is `true`, event bodies and notifications
  also carry `completion_marker` (`complete`, `incomplete` or `null`), because the line is usually
  past the result excerpt. The option is part of the payload and its hash.
- It is the CLI's self-report, not a verification: the relay runs no command of its own and reads no
  other text of the result, so a CLI can still claim `complete` wrongly. Without the opt-in nothing
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
wake-up means, and does not mean, is in [`docs/notifier.md`](docs/notifier.md). Whether a chat,
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
  after spawn) cannot be seen after a crash; see [`docs/reliability.md`](docs/reliability.md).
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
[`docs/acceptance.md`](docs/acceptance.md) for the matrix and the latest recorded results. See
[`docs/reliability.md`](docs/reliability.md) for the state machine, crash windows and the
preconditions of every guarantee.

## Acknowledgements

The submit de-duplication, exit-code runner, process identity and explicit-retry patterns
follow [AGPair](https://github.com/logicrw/agpair) (MIT). The `unknown` execution outcome and
the no-automatic-write-replay rule follow [Agenvo](https://github.com/Xuanwo/agenvo).

## License

MIT

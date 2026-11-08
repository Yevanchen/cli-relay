# cli-relay

**A small local ledger for Claude Code and Codex on the Macs (or Linux boxes) where you already run them.**

When a chat bot or script hands work to those CLIs, it needs three things the CLIs do not give you: the same request must not start twice, a crash or reconnect must leave a truthful status, and a finished result must stay until something has actually taken it. cli-relay does that. Your subscription login, permissions, and the CLIs themselves stay in charge of the work.

It is not a model proxy, an agent framework, a networked relay, an orchestrator, or a second terminal. It is a ledger on each machine, plus an optional wake-up when a task ends.

[![Intro video: Grok Bot + cli-relay, dispatch and track Claude Code / Codex CLI jobs across Macs](docs/media/cli-relay-grokbot-intro.jpg)](docs/media/cli-relay-grokbot-intro.mp4)

[Watch the intro (2:22, mp4)](docs/media/cli-relay-grokbot-intro.mp4) — English narration, Chinese + English captions. How [Grok Bot](https://grok.x.ai/) dispatches and tracks Claude Code / Codex jobs on more than one Mac through cli-relay.

## What you get

| | |
|---|---|
| **Same key, same task** | Resubmit the same `(scope, key)` and you get the same task back. A different payload under that key is a conflict, not a silent second run. |
| **Honest status after a break** | Attempts record host, process identity, native session id, and exit evidence. What cannot be proven is `unknown`, never guessed. |
| **Hold until ACK** | Terminal events keep a stable id and are re-delivered until a named consumer ACKs them. |
| **Wake on finish** | Optional notifier (script or webhook) can wake a consumer such as a Grok Bot routine when a task ends. A wake is not an ACK. |
| **Results to the submitting bot** | A verified per-bot route wakes that bot directly. `brief` prints complete identities, Chinese message lines and one batch ACK command; `acks.message_id` records the visible delivery. |
| **Dependencies and queues** | `submit --after <task id or key>` requires business `ok` by default; `--queue NAME` runs tasks one at a time in FIFO order within a home and scope. [Waiting, cancellation and release rules](docs/usage.md#dependencies-and-queues). |

## Quick start

```sh
git clone https://github.com/Yevanchen/cli-relay && cd cli-relay
pip install .            # or run in place: PYTHONPATH=. python3 -m cli_relay …
cli-relay init           # ledger under $CLI_RELAY_HOME or ~/.cli-relay

cli-relay submit --key fix-tests-42 --engine claude --cwd ~/code/app --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --prompt "Run the unit tests and summarise failures."

cli-relay wait t-… --timeout 600    # a timeout only stops waiting; the task keeps running
cli-relay events --consumer grok    # waiting/result events this consumer has not ACKed yet
cli-relay ack --consumer grok --task t-… --attempt 1 --event e-…
```

Every command prints one JSON object on stdout (except `brief --text`, `--help` / `--version`). Exit codes, `status`, `resume`, `cancel` and more: [`docs/usage.md`](docs/usage.md#commands).

With the notifier (v0.2):

```sh
cli-relay migrate                   # upgrade an existing ledger; consistent backup first
cli-relay submit --key k --engine claude --cwd ~/code/app --prompt "…" --notify-route bot --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"'
cli-relay notify run --interval 60  # periodic pass: retries, reminders, catch-up after a restart
```

Routes are configured per machine in `<home>/notifier.json`. Configuration, route registration and verification: [`docs/notifier.md`](docs/notifier.md).
For an asynchronous webhook, `route verify NAME` prints an awaiting-confirmation verification id
(exit 6, still unproven). After the bot posts `cli-relay 接通测试，无需处理`, run
`route confirm NAME --verification-id V --message-id M` on the same Mac. A synchronous visible-message
receipt still verifies in one step. New managed routes cannot gain proof by editing the configuration.
`route set-fallback NAME friday` optionally sends one reminder after the original consumer misses
the 30-minute ACK timeout, or its notification fails; it keeps the task's original owner.

## Requirements

- macOS or Linux, Python ≥ 3.11 (stdlib only), local disk for the ledger
- For real runs: `claude` and/or `codex` installed and logged in with a **subscription** by default
  (Codex API billing requires an explicit per-task `--billing-mode api`)

## Engines

| Engine | Role |
|---|---|
| `claude` | Claude Code (`dontAsk`; tools follow your existing Claude settings) |
| `codex` | Codex (explicit sandbox required for new production work) |
| `fixture` | Local stand-in for tests |

New production work requires explicit task/resource class and tier-matched model/effort.
All scopes in one relay home share at most two active jobs and one computer-use job.
See [migration notes](docs/migration-20261106.md); deployment and cross-machine coordination remain operator work.
Model and effort are set per task with `--option`. More engine options are documented in
[`docs/usage.md`](docs/usage.md#engines), and their guards in [`docs/reliability.md`](docs/reliability.md).

Codex tasks default to subscription billing and remove inherited `OPENAI_API_KEY`, `CODEX_API_KEY`,
`OPENAI_BASE_URL` and `AZURE_OPENAI_API_KEY` before preflight and CLI startup, including resume.
Only variable names and the policy are recorded; the relay and machine environment stay unchanged.
Deliberate Codex API billing requires `submit --billing-mode api` (or `--option billing_mode=api`),
persisted for that task's resumes. Configuration, provider and permission guards still apply.
Claude retains its subscription guards. Before this fix is deployed, dispatchers should keep the
temporary `--unset-env OPENAI_BASE_URL` workaround (repeat it for other names in the billing error).
See [environment cleanup](docs/usage.md#per-task-environment-cleanup).

### Permission opt-ins

Defaults stay minimal: Claude pins `dontAsk` and adds no allow rules; its existing
`allowed_tools` option accepts only `WebFetch` and `WebSearch`. Existing Claude settings and
deny rules remain active. `bypassPermissions` is rejected in task options, argv and every
settings layer, including user/project defaults. Codex defaults to `read-only`.

Claude can opt into Edit and Write within specific task subdirectories and Bash command prefixes:

```sh
cli-relay submit --key reports --engine claude --cwd ~/code/app --task-class highest_quality --resource-class standard --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --prompt "Generate reports in out and run the Python checker." \
  --allow-write-dir './out/**' --allow-write-dir './reports' \
  --allow-bash python3 --allow-bash 'pip install --user'
```

Both flags are repeatable. Directories are relative to the task `--cwd`, with an optional
trailing `/**`; absolute paths inside that cwd are also accepted. They become rules such as
`Edit(./out/**)` and `Write(./out/**)`. The whole cwd, `..` components, paths outside cwd,
symlink escapes and other glob/rule syntax are rejected. Paths are checked again before each
launch, including resume. Command prefixes become `Bash(python3:*)` and
`Bash(pip install --user:*)`. Use literal words separated by single spaces; shell operators,
quotes within the value, substitutions and wildcard/rule syntax are rejected. Shell quoting
around the CLI argument, as above, is fine.

Claude Code (observed 2.1.312) denies `cp` and `mv` with flags under every scoped Bash rule, and
denies file commands such as `cp`, `ls` and `cat` whose path is outside the task directory, even
when the rule matches. An isolated reproduction showed that the relay's rules are correct and that
`.env` files are not special. Submit receipts warn with `bash_rule_not_honoured` and
`bash_rule_path_outside_cwd`; a matching structured Bash denial adds `allowed_rule_denied` to
status, terminal events and notifications. See the
[observed commands and workarounds](docs/usage.md#allowed_bash_commands).

These are Claude permission rules, not an OS sandbox. A permitted interpreter or installer
can write outside the Edit/Write directories; choose command prefixes accordingly. Existing
Claude allow rules can also grant additional permissions, and filesystem checks cannot prevent
symlinks changing after launch.

Codex network access requires an explicit `workspace-write` sandbox:

```sh
cli-relay submit --key network-tests --engine codex --cwd ~/code/app --task-class complex --resource-class standard --option 'model="gpt-6.1-sol"' --option 'effort="max"' \
  --prompt "Run tests that bind localhost." \
  --option sandbox=workspace-write --network-access
```

This pins `sandbox_workspace_write.network_access=true` on first runs and resumes. Without
the opt-in, workspace network access remains pinned false. Network opt-ins are rejected with
`read-only`, the default sandbox, and `danger-full-access`. The existing full-access gate is
unchanged: it still requires both `--option sandbox=danger-full-access` and
`--option allow_full_access=true`.

The equivalent JSON task `options` fields (also accepted through `--option`) are:

```sh
# Claude
--option 'allowed_write_dirs=["./out/**","./reports"]' \
--option 'allowed_bash_commands=["python3","pip install --user"]'

# Codex
--option sandbox=workspace-write --option network_access=true
```

`network_access` is a typed boolean alias for the existing `allow_network`; both normalize to
`allow_network` in stored options. Conflicting aliases or flag/JSON values are rejected.
Claude lists are normalized and retained for exact resume and fresh reruns. Submit JSON,
status and terminal events include `permissions`, containing the exact requested sandbox,
network and tool opt-ins, or `"default minimal"`. Notification messages report the same
permissions separately for every attempt, without truncating permission lists. These fields
describe task opt-ins, not proof that a permission was used or a replacement for inherited
Claude settings.

Codex automatically adds `--skip-git-repo-check` when the task directory is outside a Git work
tree. Use `--option skip_git_repo_check=true` to always skip the check, or `false` to enforce it.
The decision is recorded in attempt evidence and terminal events; sandbox defaults stay the same.

## How a consumer should use it

1. Submit with a key derived from the request. If the reply is lost, submit the same key again.
2. Wait or poll `status`. A wait timeout is not a task failure.
3. Read `events --consumer <you>`, deliver the result, then `ack`. `succeeded` means the CLI finished without an error. Check the separate [business status](docs/usage.md#business-status) (`ok`, `partial`, `failed`, `unreported`, with a separate stop reason) and denial count in status/events and notification summaries. Tasks can report a final `status=...` line or opt into `result_file=out/result.json`; `require_business_status=true` warns when no status is reported. The existing [completion marker](docs/usage.md#when-the-cli-finished-is-not-the-task-is-done-the-completion-marker) can still gate the process outcome.
4. If the state is `unknown`, a human chooses `resume` or an explicit fresh re-run.

A bot woken by its own route uses these four steps on the named Mac:

1. Run `cli-relay --home /path/to/ledger brief --consumer <you> --route <your-route> --text`.
2. Send the printed Chinese lines together as one visible message in the route's chat. Each line
   contains the tool-computed task/event suffixes, conclusion and absolute result path (or `无结果文件`).
3. Obtain that message's platform message id.
4. Run the printed batch `ack` command, replacing `<MESSAGE_ID>` with that id; `--chat` can record
   the delivery chat. The ledger's `acks.message_id` and `acks.chat` are the delivery record.

`brief` does not send, ACK, reconcile or increment `delivery_count`. An empty brief needs no message.
Submit with `--notify-route <your-route>` so the result reaches the submitting bot directly.

## Documentation

| Doc | Covers |
|---|---|
| [`docs/usage.md`](docs/usage.md) | Commands and exit codes, per-task routes, engine options (sandbox, network, tools, model/effort, completion marker), consumer workflow, limits, tests |
| [`docs/notifier.md`](docs/notifier.md) | Notifier model, `notifier.json`, route registration and verification, payload, install and rollback |
| [`docs/reliability.md`](docs/reliability.md) | State machine, crash windows, subscription and permission guards, failure model |
| [`docs/decisions.md`](docs/decisions.md) | Design decisions; comparison with the native CLIs, AGPair, Agenvo and Reason Network |
| [`docs/acceptance.md`](docs/acceptance.md) | Test matrix, how to reproduce, what has and has not been run |

## Status

Experimental. What has and has not been verified is in [`docs/acceptance.md`](docs/acceptance.md); known limits are in [`docs/usage.md`](docs/usage.md#limits). Not promised: exactly-once external side effects, that a notification reaches a chat or a person, or that a process keeps running across sleep, power loss or reboot.

## Acknowledgements

The submit de-duplication, exit-code runner, process identity and explicit-retry patterns follow [AGPair](https://github.com/logicrw/agpair) (MIT). The `unknown` execution outcome and the no-automatic-write-replay rule follow [Agenvo](https://github.com/Xuanwo/agenvo).

## License

[MIT](LICENSE)

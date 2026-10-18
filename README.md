# cli-relay

**Experimental (v0.1).** A thin local ledger for tasks run by the official coding CLIs
(Claude Code, Codex) on the machine where they are installed. It adds three things the
CLIs do not do by themselves:

1. **No blind re-run for a repeated request key.** The same `(scope, key)` returns the same
   task. The same key with a different payload is an explicit conflict.
2. **Reconciliation after a disconnect.** Every attempt durably records its host, cwd,
   keeper/CLI process identity (pid + OS start time), native session id, exit status and the
   CLI's own result. What cannot be proven is reported as `unknown`, never guessed.
3. **Results kept until the consumer ACKs them.** The terminal state and a stable event id are
   committed in one transaction. They are re-delivered until a named consumer ACKs, and are
   never expired or garbage-collected.

Everything else stays native: the CLIs do the work, own the sessions, apply their own
permission rules and use your existing subscription login. cli-relay is not a model proxy, an
agent harness, a networked relay, a terminal runtime or an orchestrator.
[`docs/decisions.md`](docs/decisions.md) maps what the CLIs and existing projects (Reason
Network, Agenvo, AGPair) already cover. It also explains why this is a small separate
ledger rather than a fork.

## Requirements

- macOS or Linux, Python ≥ 3.9 (standard library only), a local filesystem for the ledger.
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
```

Every command prints one JSON object. Exit codes: `0` ok, `2` usage, `3` key conflict, `4` not
found, `5` ledger or storage error, `6` refused (a precondition does not hold).

## Engines

| Engine | Runs | Resume |
|---|---|---|
| `claude` | `claude -p --output-format stream-json --verbose --session-id <uuid> --permission-prompts none` (prompt on stdin) | `--resume <exact uuid>` |
| `codex` | `codex exec --json -C <cwd> -o <file> [--sandbox read-only\|workspace-write] -` | `codex exec resume <exact id> -` |
| `fixture` | a deterministic local stand-in used by tests and the soak run | same, against its own state |

Never used: `--dangerously-*`, `bypassPermissions`, `--approve-for-me`, `--sandbox
danger-full-access`, `--continue`, `--last`, `--bare`.
- Permission prompts that nobody can answer are denied by the CLI, and the task ends as `needs_input`.
- The keeper refuses a run (`blocked`) if API-key or gateway variables are present (only their names are reported) or if the login is not a subscription.
- It also stops the run if the CLI itself reports an API-key source.

## How a consumer (e.g. a chat bot calling over SSH) should use it

1. Submit with a key derived from the request. If the reply is lost or uncertain, submit the
   same key again: you get the same task, and nothing runs twice.
2. Wait with a bounded `wait`, or poll `status`. A timeout is not a failure.
3. Read `events --consumer <you>`. After the result has reached its destination, `ack` the event.
   Until then the event is delivered again on every read, with the same id.
4. If a task is `unknown` (the relay could not prove what happened), a human decides:
   - `resume` continues the exact native session;
   - `resume --fresh --confirm-rerun` starts over and may repeat side effects.

The relay keeps the result and the event; it does **not** deliver notifications. Whether a
chat bot, a phone notification or a person ever sees the result depends on the consumer reading
again or on a separate wake-up mechanism.

## Limits (v0.1)

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

## Tests and acceptance

```sh
python3 -m unittest discover -s tests -t tests -v   # fixture engine, real OS processes, ~1 min
python3 scripts/soak.py run --dir /tmp/soak --hours 0.03                   # scaled-down soak
python3 scripts/soak.py start --dir ~/soak-cli-relay --hours 25            # the real 25 h soak
python3 scripts/real_cli_acceptance.py --engine claude --home DIR --workdir DIR --out result.json
```

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

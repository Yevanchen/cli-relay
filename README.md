# cli-relay

**Experimental (v0.1.0).** A thin local ledger for tasks run by the official coding CLIs
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

## Verification status (v0.1.0)

| Evidence | Code | Status |
|---|---|---|
| Fault-injection suite: 105 tests, fixture engine, real OS processes (macOS) | `a431845` | PASS, 105/105 in 2 consecutive rounds |
| Scaled-down soak (0.03 h) | `a431845` | PASS. A short test, **not** the 25 h result |
| Real Claude Code loop: success, failure parsing, exact resume, events → ACK | `a431845` | PASS, 3/3 runs |
| Real Codex loop: same steps | `a431845` | PASS, 3/3 runs |
| 25 h soak | `b72618d` (before the audit fixes) | **RUNNING** |
| 25 h soak | `a431845` (current code) | **NOT RUN** |
| C4: second entry point over a real bot/SSH channel | — | **NOT RUN** (simulated locally only) |
| F5: real disk-full on the ledger volume | — | **NOT RUN** (simulated only) |
| J1: execution across machine sleep, power loss or reboot | — | **NOT RUN** |
| J2: CI on `ubuntu-latest`, Python 3.11 / 3.12 / 3.14 | — | **NOT RUN** when this was written. Its first run starts when the repository is published |

Not promised:
- exactly-once external side effects;
- delivery of any notification (chat bot, phone or anything else).

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
| `claude` | `claude -p --output-format stream-json --verbose --session-id <uuid> --permission-prompts none` (prompt on stdin) | `--resume <exact uuid>` |
| `codex` | `codex exec --json -C <cwd> -o <file> --sandbox <s> -`, where `<s>` is `read-only` unless the task asks for `workspace-write` | `codex exec --sandbox <s> resume -c sandbox_mode="<s>" --json -o <file> <exact id> -` (same `<s>`) |
| `fixture` | a deterministic local stand-in used by tests and the soak run | same, against its own state |

Never used: `--dangerously-*`, `bypassPermissions`, `--approve-for-me`, `--sandbox
danger-full-access`, `--continue`, `--last`, `--bare`.
- The Codex sandbox is always passed explicitly, so neither the first run nor a resume takes it
  from the configuration of the moment.
- A Claude settings layer or init event reporting `bypassPermissions` blocks the run.
- Claude: permission prompts that nobody can answer are denied by the CLI. A result that lists
  permission denials ends as `needs_input`, even if the CLI also reports an error.
- Codex: the `codex exec --json` events that cli-relay reads carry no structured permission
  denial. A command refused by the sandbox reaches the model as a failed command, and the
  outcome follows Codex's own result, so `needs_input` is never reported for Codex. Read the
  result text.
- The keeper refuses a run (`blocked`) before the CLI starts if it finds any of these:
  - API-key or gateway variables (only their names are reported);
  - a configuration layer that applies to the task directory and routes away from the
    subscription (e.g. a Codex custom provider or base URL, a Claude `apiKeyHelper`);
  - a configuration layer it cannot read;
  - a login that is not a subscription.
- It also stops the run if the CLI itself reports an API-key source.
- After a Codex run, the thread's own record must name the built-in `openai` provider.
- These outcomes do not depend on whether the keeper survived: reconciliation applies the same
  checks. See [`docs/reliability.md`](docs/reliability.md) for the exact layers and limits.

## How a consumer (e.g. a chat bot calling over SSH) should use it

1. Submit with a key derived from the request. If the reply is lost or uncertain, submit the
   same key again: you get the same task, and nothing runs twice.
2. Wait with a bounded `wait`, or poll `status`. A timeout is not a failure. Polling is also
   the recovery path: a task whose keeper never started is started by `status`, `events` or
   `wait` once it has been pending for 5 s.
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
- The subscription check reads the CLIs' configuration files itself, not through their own
  resolvers. A routing setting it does not know about is not detected before the run.
- The run's own provider record is checked for Codex only. It is written when a thread is
  created, so a resumed Codex turn relies on the preflight check.

## Tests and acceptance

```sh
python3 -m unittest discover -s tests -t tests -v   # fixture engine, real OS processes, ~2 min
python3 scripts/soak.py run --dir /tmp/soak --hours 0.03                   # scaled-down soak
python3 scripts/soak.py start --dir ~/soak-cli-relay --hours 25            # the real 25 h soak
python3 scripts/soak.py verdict --dir ~/soak-cli-relay                     # re-check a finished soak
python3 scripts/real_cli_acceptance.py --engine claude --home DIR --workdir DIR --out result.json
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

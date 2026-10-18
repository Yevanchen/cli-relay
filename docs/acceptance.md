# Acceptance

Three kinds of evidence are reported separately and never stand in for one another:

1. **Argument**: the state machine and crash-window analysis in [`reliability.md`](reliability.md).
2. **Short tests**: automated fault injection with real OS processes, plus a few real CLI loops.
3. **Real 25-hour soak**: reported as RUNNING until it has actually elapsed.

Status words: **PASS**, **FAIL**, **NOT RUN** (with the reason), **RUNNING**.

## Environment of the recorded run

- Product code under test: `cli_relay/` at commit `b72618d9ad98afab48d47c3a2f9c7834d709c706`, unchanged through `f14f2bb39c537879523e4042aced64da3537445f`.
  - `f14f2bb` only widens two test timing margins, spaces the identity test for Linux tick resolution, and updates CI and docs.
  - The fault-injection suite was re-run on `f14f2bb`.
- macOS (Darwin 25.2.0, arm64), Python 3.14.3, SQLite 3.52.0.
- Claude Code 2.1.301 (subscription login).
- Codex CLI 0.163.0 (ChatGPT login).
- Linux and Python 3.10/3.12/3.14 are covered by CI ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml), free `ubuntu-latest`); results appear once the repository is published.

## How to reproduce

```sh
python3 -m unittest discover -s tests -t tests -v                       # ~1 min, fixture engine only
python3 scripts/real_cli_acceptance.py --engine claude --home H --workdir W --out claude.json
python3 scripts/real_cli_acceptance.py --engine codex  --home H --workdir W --out codex.json
python3 scripts/soak.py start --dir SOAK --hours 25 && python3 scripts/soak.py status --dir SOAK
```

The real-CLI script makes two tiny model calls per engine plus one request rejected for an
unknown model, all on the existing subscription. It refuses to run unless `doctor --engine`
confirms a subscription login.

## Matrix

| # | Requirement | Evidence | Result |
|---|---|---|---|
| A1 | Same key + same payload returns the same task; work runs once (sequential) | `test_duplicate_submit_returns_same_task_and_runs_once` | PASS |
| A2 | 8 concurrent submits of one key → 1 task, 1 attempt, 1 execution, 8 audited submits | `test_concurrent_duplicate_submits_converge_on_one_execution` | PASS |
| A3 | Same key + different payload (prompt, options, cwd) → explicit conflict, nothing runs, payload not echoed | `test_same_key_with_different_payload_is_a_conflict`, `test_cwd_and_options_are_part_of_the_payload` | PASS |
| A4 | Key scope isolates callers | `test_scope_isolates_keys` | PASS |
| A5 | Submit reply lost → resubmitting the same key recovers the same task | `test_lost_submit_response_is_recovered_by_resubmitting` | PASS |
| B1 | Crash after the submit commit, before a keeper (W1) | `test_submit_crash_after_commit_before_keeper` | PASS |
| B2 | Keeper SIGKILL after claim (W2): claim released, same attempt restarted, runs once | `test_keeper_crash_after_claim_is_released_and_restarted` | PASS |
| B3 | Keeper SIGKILL before spawn (W3): `unknown`, never auto re-run; exact resume reports the missing session; only `--fresh --confirm-rerun` runs again | `test_keeper_crash_before_spawn_is_unknown_and_not_rerun` | PASS |
| B4 | Keeper SIGKILL after spawn, before the pid record (W4): orphan observed, closed from the native result with `exit.known=false` | `test_keeper_crash_after_spawn_before_pid_record` | PASS |
| B5 | Keeper killed while the CLI keeps running (W5) | `test_keeper_killed_while_cli_keeps_running` | PASS |
| B6 | CLI SIGKILL (W6) → `failed`, signal 9 recorded, no re-run | `test_cli_killed_is_recorded_as_failed_not_rerun` | PASS |
| B7 | Keeper SIGKILL after `exit.json`, before the commit (W7) → real exit code recovered | `test_keeper_crash_after_exit_before_commit` | PASS |
| B8 | Keeper SIGKILL after the commit (W8) is harmless | `test_keeper_crash_after_commit_is_harmless` | PASS |
| B9 | Recycled pid is not mistaken for the keeper (pid + OS start time) | `test_recycled_pid_is_not_mistaken_for_the_keeper`, `test_processes_started_back_to_back_have_distinct_tokens` | PASS |
| C1 | Caller session teardown (SIGHUP + SIGKILL of an SSH-like session) does not stop the task | `test_caller_session_teardown_does_not_stop_the_task` | PASS |
| C2 | Killing a waiter does not stop the task; `wait` timeout only stops waiting | `test_killed_waiter_does_not_stop_the_task`, `test_wait_timeout_does_not_stop_the_task` | PASS |
| C3 | Second entry point: a separate process with a bare environment reads the same task by key | `test_second_entry_point_with_minimal_environment` | PASS |
| C4 | Second entry point over the existing bot/SSH channel | — | NOT RUN (needs the bot's command channel; only simulated by C1/C3) |
| D1 | Events re-delivered with stable ids and bodies until ACK; delivery count visible | `test_events_are_redelivered_with_stable_ids_until_ack` | PASS |
| D2 | Lost or repeated ACK is idempotent | `test_lost_or_repeated_ack_is_idempotent` | PASS |
| D3 | ACK with wrong task / attempt / unknown event is rejected | `test_mismatched_ack_is_rejected` | PASS |
| D4 | Consumers are independent; an offline consumer reads everything later | `test_consumers_are_independent`, `test_offline_consumer_reads_everything_later` | PASS |
| D5 | No code path deletes results (no TTL, no GC) | `test_ledger_code_never_deletes` | PASS |
| E1 | Cancel a running task → `cancelled` (SIGTERM recorded), no side effect | `test_cancel_running_task` | PASS |
| E2 | Cancel before any keeper → `cancelled_before_start`; resubmit does not run it | `test_cancel_pending_attempt_before_any_keeper` | PASS |
| E3 | Cancel vs. completion race → exactly one terminal event; a lost cancel is recorded | `test_cancel_racing_completion_has_one_consistent_outcome` | PASS |
| E4 | Resume continues the exact native session; refused while the attempt is active | `test_resume_continues_the_exact_native_session`, `test_resume_refused_while_attempt_active` | PASS |
| E5 | 4 concurrent resumes → exactly one new attempt | `test_concurrent_resumes_create_one_attempt` | PASS |
| E6 | Stale writers cannot change a closed or superseded attempt | `test_stale_writers_cannot_change_a_closed_or_superseded_attempt` | PASS |
| F1 | Uninitialised home, missing database, corrupt database → explicit errors; nothing recreated or replaced; corrupt ledger refuses new work | `test_uninitialized_home_is_an_explicit_error`, `test_missing_database_is_never_recreated_silently`, `test_corrupt_database_stops_new_work` | PASS |
| F2 | Low disk space refuses new work before acceptance and blocks at spawn time | `test_low_disk_space_refuses_new_work`, `test_low_disk_space_at_spawn_time_blocks_without_running` | PASS (simulated with a free-space threshold) |
| F3 | Read-only ledger: submit fails without reporting `accepted` | `test_read_only_ledger_fails_without_reporting_accepted` | PASS |
| F4 | Real disk-full on the ledger volume | — | NOT RUN (not induced on a real machine) |
| G1 | No permission-relaxing flags; `danger-full-access` and bypass options rejected | `test_claude_argv`, `test_codex_argv`, `test_forbidden_flags_and_full_access_are_rejected` | PASS |
| G2 | API-key/gateway variables block the run before spawn; values never printed | `test_billing_env_blocks_and_never_reports_values`, `test_billing_env_blocks_a_real_run_before_spawn` | PASS |
| G3 | Non-subscription login refused; init event reporting an API-key source stops the run | `test_auth_must_be_the_subscription_login`, `test_api_key_source_reported_by_the_cli_stops_the_run` | PASS |
| H1 | Claude Code real loop: success, failure parsing, exact resume of the pre-assigned session id, events → ack | `scripts/real_cli_acceptance.py --engine claude` | PASS (5/5 steps) |
| H2 | Codex real loop: success (`--sandbox read-only`), failure parsing, exact resume of the captured thread id, events → ack | `scripts/real_cli_acceptance.py --engine codex` | PASS (5/5 steps) |
| I1 | Scaled-down soak (0.03 h): every soak mechanism exercised end to end | `scripts/soak.py run --hours 0.03` | PASS (9/9 verdicts); a short test, **not** the 25 h result |
| I2 | Real 25-hour soak | `scripts/soak.py start --hours 25` | RUNNING (started 2026-10-19 03:28 UTC+8; planned end 2026-10-20 ≈04:30 UTC+8) |
| J1 | Execution across machine sleep, power loss or reboot | — | NOT RUN (out of scope by design; sleep settings are not changed and the machine is not restarted) |
| J2 | CI on `ubuntu-latest` with Python 3.10 / 3.12 / 3.14 | workflow in this repository | NOT RUN (runs after publishing) |

## Recorded results

- **Fault-injection suite.** 58 tests, 58 passed, in each of four consecutive runs on `b72618d`
  (61–64 s per run) and two runs on `f14f2bb` (63–67 s).
  - An earlier run exposed a real defect: on macOS, `ps -o lstart` has only one-second
    resolution, so a pid recycled within the same second looked like the original process.
  - The fix reads the kernel's microsecond start time. A regression test now checks that two
    processes started back to back have different identities.
- **Real Claude Code loop.** Success in 5.1 s. Failure in 4.1 s: an unknown model gave exit 1,
  and the result had `is_error: true` even though `subtype` was `success`. Exact resume took
  9.2 s, kept the same session id and the model repeated the earlier phrase. 3 events were
  ACKed and none were left.
- **Real Codex loop.** Success in 19.3 s. Failure in 8.2 s: an HTTP 400 stating the model is
  not supported "when using Codex with a ChatGPT account", which confirms the subscription
  path. Exact resume took 16.3 s and kept the same thread id. 3 events were ACKed and none
  were left.
- **Scaled-down soak (0.03 h).** All verdicts passed:
  - no lost or changed events;
  - no key executed more than expected;
  - the long task completed;
  - the keeper-killed task was reconciled once;
  - the unknown task was re-run only explicitly;
  - all events were ACKed;
  - the control restart was exercised;
  - the repeated ACK was reported as a duplicate.
- **25-hour soak.** RUNNING. Its checkpoints and final summary will replace this line only after
  the run has actually completed.

## What this does not show

- Exactly-once external side effects. They are not claimed.
- Delivery to any chat bot, phone or person. The relay stores results and re-delivers them;
  notification is out of scope.
- Behaviour across real machine sleep, power loss, reboot or a full disk.

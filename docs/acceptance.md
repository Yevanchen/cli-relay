# Acceptance

Three kinds of evidence are reported separately and never stand in for one another:

1. **Argument**: the state machine, crash windows and guards in [`reliability.md`](reliability.md).
2. **Short tests**: automated fault injection with real OS processes, a scaled-down soak, and a
   few real CLI loops. They show the mechanisms, not long-run behaviour.
3. **Real 25-hour soak**: reported as RUNNING or NOT RUN until a run on the code in question
   has actually elapsed and been evaluated.

Status words: **PASS**, **FAIL**, **NOT RUN** (with the reason), **RUNNING**.

## Code under test

- Product code, scripts and tests at commit `a431845`. Later commits change documentation only.
  - It includes the fixes for the external audit of `6c8d2f3` (product code `b72618d`), one commit
    per finding (see [`decisions.md`](decisions.md), D11).
- macOS (Darwin 25.2.0, arm64), Python 3.14.3, SQLite 3.52.0.
- Claude Code 2.1.301 (subscription login), Codex CLI 0.163.0 (ChatGPT login).
- Linux and Python 3.11 / 3.12 / 3.14 are covered by CI
  ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml), free `ubuntu-latest`). That CI has
  not run yet; results appear once the repository is published.

## How to reproduce

```sh
python3 -m unittest discover -s tests -t tests -v                       # ~2 min, fixture engine only
python3 scripts/soak.py run --dir SOAK --hours 0.03                     # scaled-down soak, exit 0 only on PASS
python3 scripts/real_cli_acceptance.py --engine claude --home H --workdir W --out claude.json
python3 scripts/real_cli_acceptance.py --engine codex  --home H --workdir W --out codex.json
python3 scripts/soak.py start --dir SOAK25 --hours 25                   # the real 25 h soak
python3 scripts/soak.py verdict --dir SOAK25                            # evaluate it when finished
```

Each real-CLI run makes two tiny model calls plus one request rejected for an unknown model,
all on the existing subscription. It refuses to run (NOT RUN, exit 3) unless `doctor --engine
--cwd` confirms a subscription login and configuration for the work directory. It exits 0
only on PASS.

## 1. Argument

[`reliability.md`](reliability.md) covers:
- the attempt state machine;
- the crash windows W0–W9 and what each leaves durable;
- process identity, and when the relay may signal a process;
- the subscription and permission guards;
- the safety and liveness conditions;
- the residual risks.

The tests below check that the code behaves as argued. They do not prove the argument for
every schedule.

## 2. Short tests

### 2.1 Matrix (fault-injection suite, fixture engine, real OS processes)

| # | Requirement | Evidence | Result |
|---|---|---|---|
| A1 | Same key + same payload returns the same task; work runs once (sequential) | `test_duplicate_submit_returns_same_task_and_runs_once` | PASS |
| A2 | 8 concurrent submits of one key → 1 task, 1 attempt, 1 execution, 8 audited submits | `test_concurrent_duplicate_submits_converge_on_one_execution` | PASS |
| A3 | Same key + different payload (prompt, options, cwd) → explicit conflict, nothing runs, payload not echoed | `test_same_key_with_different_payload_is_a_conflict`, `test_cwd_and_options_are_part_of_the_payload`, `test_same_key_with_a_different_cwd_is_a_conflict` | PASS |
| A4 | Key scope isolates callers | `test_scope_isolates_keys` | PASS |
| A5 | Submit reply lost → re-sending the same key recovers the same task, also after the task directory was removed or with the disk below the threshold; a new key still needs both | `test_lost_submit_response_is_recovered_by_resubmitting`, `test_duplicate_after_the_cwd_was_deleted`, `test_duplicate_when_disk_space_is_low`, `test_a_new_key_still_needs_an_existing_cwd_and_free_disk` | PASS |
| B1 | Crash after the submit commit, before a keeper (W1); with the default 5 s grace, plain `status` polling starts it | `test_submit_crash_after_commit_before_keeper`, `test_resubmit_within_grace_then_only_status_polling` | PASS |
| B2 | Keeper SIGKILL after claim (W2): claim released, same attempt restarted, runs once | `test_keeper_crash_after_claim_is_released_and_restarted` | PASS |
| B3 | Keeper SIGKILL before spawn (W3): `unknown`, never auto re-run; exact resume reports the missing session; only `--fresh --confirm-rerun` runs again | `test_keeper_crash_before_spawn_is_unknown_and_not_rerun` | PASS |
| B4 | Keeper SIGKILL after spawn, before the pid record (W4): orphan observed, closed from the native result with `exit.known=false` | `test_keeper_crash_after_spawn_before_pid_record` | PASS |
| B5 | Keeper killed while the CLI keeps running (W5) | `test_keeper_killed_while_cli_keeps_running` | PASS |
| B6 | CLI SIGKILL (W6) → `failed`, signal 9 recorded, no re-run | `test_cli_killed_is_recorded_as_failed_not_rerun` | PASS |
| B7 | Keeper SIGKILL after `exit.json`, before the commit (W7) → real exit code recovered | `test_keeper_crash_after_exit_before_commit` | PASS |
| B8 | Keeper SIGKILL after the commit (W8) is harmless | `test_keeper_crash_after_commit_is_harmless` | PASS |
| B9 | Process identity: a recycled pid is not the keeper; a failed kernel query or a token of another source is "cannot verify", never "gone"; a live keeper with an incomparable token keeps its claim | `test_recycled_pid_is_not_mistaken_for_the_keeper`, `test_processes_started_back_to_back_have_distinct_tokens`, `test_token_from_another_source_is_unverifiable`, `test_failed_kernel_query_does_not_fall_back_to_a_different_token`, `test_linux_tokens_compare_boot_and_start`, `test_live_keeper_with_an_incomparable_token_keeps_its_claim` | PASS |
| B10 | A relative `--home` or `CLI_RELAY_HOME` reaches the keeper as the same ledger | `test_relative_home_flag`, `test_relative_home_environment_variable` | PASS |
| C1 | Caller session teardown (SIGHUP + SIGKILL of an SSH-like session) does not stop the task | `test_caller_session_teardown_does_not_stop_the_task` | PASS (local session simulation) |
| C2 | Killing a waiter does not stop the task; `wait` timeout only stops waiting | `test_killed_waiter_does_not_stop_the_task`, `test_wait_timeout_does_not_stop_the_task` | PASS |
| C3 | Second entry point: a separate process with a bare environment reads the same task by key | `test_second_entry_point_with_minimal_environment` | PASS (separate local process) |
| C4 | Second entry point over the existing bot/SSH channel | — | NOT RUN (needs the bot's command channel; only simulated by C1/C3) |
| D1 | Events re-delivered with stable ids and bodies until ACK; delivery count visible | `test_events_are_redelivered_with_stable_ids_until_ack` | PASS |
| D2 | Lost or repeated ACK is idempotent | `test_lost_or_repeated_ack_is_idempotent` | PASS |
| D3 | ACK with wrong task / attempt / unknown event is rejected | `test_mismatched_ack_is_rejected` | PASS |
| D4 | Consumers are independent; an offline consumer reads everything later | `test_consumers_are_independent`, `test_offline_consumer_reads_everything_later` | PASS |
| D5 | No code path deletes results (no TTL, no GC) | `test_ledger_code_never_deletes` (a search for delete statements; the design argument is in `reliability.md`) | PASS |
| D6 | An empty poll does not read the acknowledged history (filtered in SQL) | `test_empty_poll_reads_no_acknowledged_rows` | PASS |
| E1 | Cancel a running task → `cancelled` (SIGTERM recorded) | `test_cancel_running_task` | PASS ("no side effect" only reflects the fixture's timing; a cancel cannot undo work a real CLI already did) |
| E2 | Cancel before any keeper → `cancelled_before_start`; resubmit does not run it | `test_cancel_pending_attempt_before_any_keeper` | PASS |
| E3 | Cancel vs. completion race → exactly one terminal event; a lost cancel is recorded, also when the cancel lands between the outcome decision and the commit | `test_cancel_racing_completion_has_one_consistent_outcome`, `test_cancel_between_outcome_decision_and_commit` | PASS |
| E4 | Resume continues the exact native session; refused while the attempt is active; Codex keeps the submitted sandbox (read-only by default) on resume | `test_resume_continues_the_exact_native_session`, `test_resume_refused_while_attempt_active`, `test_resume_passes_the_stored_sandbox`, `test_no_sandbox_option_means_read_only_on_both_runs`, `test_requested_sandbox_is_kept_on_both_runs` | PASS |
| E5 | 4 concurrent resumes → exactly one new attempt | `test_concurrent_resumes_create_one_attempt` | PASS |
| E6 | Stale writers cannot move a closed or superseded attempt's state (claim, spawn record, pid record, finish) | `test_stale_writers_cannot_change_a_closed_or_superseded_attempt` | PASS (metadata such as the native session id and evidence is not state-guarded) |
| E7 | Cancel never signals an unverified process: a recycled process group is left alone and the reply says why; a verified orphan CLI is still stopped | `test_cancel_does_not_signal_a_recycled_process_group`, `test_cancel_still_stops_a_verified_orphan_cli` | PASS |
| F1 | Uninitialised home, missing database, corrupt database → explicit errors; nothing recreated or replaced; corrupt ledger refuses new work | `test_uninitialized_home_is_an_explicit_error`, `test_missing_database_is_never_recreated_silently`, `test_corrupt_database_stops_new_work` | PASS |
| F2 | Low disk space refuses new work before acceptance and blocks at spawn time | `test_low_disk_space_refuses_new_work`, `test_low_disk_space_at_spawn_time_blocks_without_running` | PASS (simulated with a free-space threshold) |
| F3 | Read-only ledger: submit fails without reporting `accepted` | `test_read_only_ledger_fails_without_reporting_accepted` | PASS |
| F4 | Short writes are completed; a write without progress fails before rename; a prompt that cannot be stored blocks the attempt and the CLI never starts | `test_short_writes_are_completed`, `test_a_write_that_makes_no_progress_fails_before_rename`, `test_the_attempt_is_blocked_and_the_cli_never_starts` | PASS (simulated) |
| F5 | Real disk-full on the ledger volume | — | NOT RUN (not induced on a real machine) |
| G1 | No permission-relaxing flags; `danger-full-access` and bypass options rejected; Claude bypass mode in settings or the init event blocks the run | `test_claude_argv`, `test_codex_argv`, `test_forbidden_flags_and_full_access_are_rejected`, `test_user_settings`, `test_project_settings_of_the_task_directory`, `test_the_run_is_stopped_and_blocked`, `test_still_blocked_when_the_keeper_died_before_committing` | PASS |
| G2 | API-key/gateway variables block the run before spawn; values never printed | `test_billing_env_blocks_and_never_reports_values`, `test_billing_env_blocks_a_real_run_before_spawn` | PASS |
| G3 | Non-subscription login refused; init event reporting an API-key source stops the run | `test_auth_must_be_the_subscription_login`, `test_api_key_source_reported_by_the_cli_stops_the_run` | PASS |
| G4 | Configuration that routes away from the subscription blocks before the CLI starts (Codex custom provider + `env_key`, profile, project config of the task directory; Claude `apiKeyHelper`, settings `env`); the Codex thread record must name `openai` | `test_codex_custom_provider_with_its_own_key_is_blocked`, `test_codex_profile_selecting_a_custom_provider_is_blocked`, `test_codex_project_configuration_in_the_task_cwd_is_checked`, `test_claude_api_key_helper_in_settings_is_blocked`, `test_claude_gateway_in_settings_env_is_blocked`, `test_default_configuration_passes`, `test_custom_provider_blocks_before_the_cli_starts`, `test_provider_recorded_by_the_run_is_verified` | PASS (fake CLIs; known keys only, see `reliability.md`) |
| G5 | A billing violation stays `blocked` when the keeper dies while the CLI runs or after `exit.json` | `test_keeper_dies_while_the_cli_runs`, `test_keeper_dies_after_exit_record`, `test_codex_provider_record_is_checked_after_a_keeper_crash` | PASS |
| G6 | A Claude permission denial is `needs_input` even with an error result; a cancel still wins | `test_denial_with_error_result_is_needs_input`, `test_denial_with_success_result_is_needs_input`, `test_cancel_still_wins` | PASS (Codex reports no structured denial) |
| H0 | The real-CLI script itself: checks exactly its own three events, ACKs only those, leaves other events alone, exits non-zero on FAIL | `test_passes_and_acks_only_its_own_events`, `test_a_failed_step_gives_a_nonzero_exit_code` (fixture mode) | PASS |
| I0 | The soak verdict fails on each false-pass path: a short task that never executed, a long task cut short, a run shorter than planned; `soak.py verdict` re-evaluates read-only | `test_a_short_task_that_never_executed_fails`, `test_a_long_task_cut_short_fails`, `test_a_run_shorter_than_planned_fails`, `test_unexecuted_short_task_and_too_short_long_task_fail`, `test_consistent_run_passes`, `test_verdict_command_re_evaluates_a_finished_soak_without_writing` | PASS |
| J1 | Execution across machine sleep, power loss or reboot | — | NOT RUN (out of scope by design; sleep settings are not changed and the machine is not restarted) |
| J2 | CI on `ubuntu-latest` with Python 3.11 / 3.12 / 3.14 | workflow in this repository | PASS on `baff533`: [push run](https://github.com/Yevanchen/cli-relay/actions/runs/39197188143), [tag run](https://github.com/Yevanchen/cli-relay/actions/runs/39197203482) |

### 2.2 Recorded short-test results on `a431845`

All of these are short tests. None of them is a long-run result.

- **Fault-injection suite.** 105 tests: the 58 earlier tests (some extended) plus the audit
  regression tests and the permission-posture tests. **105/105 passed in each of two
  consecutive rounds** (107.1 s and 106.7 s).
  - Before any fix, the audit regression tests were run on the audited code: 33 tests,
    24 failures and 4 errors. Every must-fix item had a failing test. The 5 that passed were
    controls or already-correct paths.
  - Each test added later failed on the commit before its fix, unless it tests a helper or a
    command that did not exist yet.
  - The per-finding record is in [`decisions.md`](decisions.md) (D11).
- **Scaled-down soak (0.03 h).** **PASS, 12/12 verdicts**, under the corrected verdict rules.
  - It ran from a frozen copy of `a431845` for 0.03 h of the planned 0.03 h.
  - The long task ran 0.029 h of its configured 0.029 h.
  - 11 short tasks ran, each exactly once. No key was off, and no clock gaps occurred.
  - 15 events: none lost or changed, and all ACKed at the end.
  - The control restart was exercised, and repeated ACKs were reported as duplicates.
  - The unknown task had exactly 2 attempts.
  - `soak.py verdict` on the finished run also gave PASS (exit 0).
  - The same scaled-down soak on `df2f153`, after the must-fix items, also passed.
- **Real Claude Code loop, 3 runs.** **PASS 3/3**, every run 5/5 steps.
  - Preflight: `claude.ai` / `firstParty`.
  - Success in 6.1 s each.
  - Failure: an unknown model gave exit 1 in 3.1–4.1 s, with `is_error: true` while
    `subtype` was `success`.
  - Exact resume: 5.1–7.2 s, same pre-assigned session id, and the model repeated the
    earlier phrase.
  - Events: exactly this run's 3 terminal events were seen and ACKed, and no others.
  - The init event of every attempt reported `apiKeySource: none` and `permissionMode: default`.
- **Real Codex loop, 3 runs.** **PASS 3/3**, every run 5/5 steps.
  - Preflight: ChatGPT login, user configuration layer only.
  - Success in 9.2–21.2 s; the thread record named provider `openai`, sandbox `read-only`.
  - Failure: an unknown model gave exit 1 in 5.1–6.2 s, with an HTTP 400 that refers to
    "a ChatGPT account".
  - Exact resume: 8.2–10.2 s, same thread id. The resumed turn's sandbox was `read-only`, but
    that is also this machine's default, so it does not by itself show the fix for audit #2
    (the argv tests do).
  - Events: exactly this run's 3 terminal events were seen and ACKed.
- **Raw reports.** They are kept locally and not committed, because they contain local paths
  and session ids.

## 3. Real 25-hour soak

| Run | Code | Result |
|---|---|---|
| 25 h soak started 2026-10-19 03:28 UTC+8 (`scripts/soak.py start --hours 25`) | `b72618d`, before the audit fixes | **RUNNING** (planned end 2026-10-20 ≈04:30 UTC+8) |
| 25 h soak on the fixed code | `a431845` | **NOT RUN** (whether to start one is the maintainer's decision) |

The running soak exercises the old code. Its own summary uses the verdict rules from before
audit #9, which had false-pass paths. When it ends, evaluate it with the corrected rules:

```sh
python3 scripts/soak.py verdict --dir SOAK25   # reads only; exit 0 only on PASS
```

Even a PASS of that run says nothing directly about the fixed code. This section will only
show a result once a run has actually elapsed and been evaluated.

## What this does not show

- Exactly-once external side effects. They are not claimed.
- Delivery to any chat bot, phone or person. The relay stores results and re-delivers them;
  notification is out of scope and not guaranteed.
- Behaviour across real machine sleep, power loss, reboot or a full disk.
- That every possible CLI configuration is recognised by the subscription check.

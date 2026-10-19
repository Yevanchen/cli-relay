# Acceptance

Three kinds of evidence are reported separately and never stand in for one another:

1. **Argument**: the state machine, crash windows and guards in [`reliability.md`](reliability.md).
2. **Short tests**: automated fault injection with real OS processes, a scaled-down soak, and a
   few real CLI loops. They show the mechanisms, not long-run behaviour.
3. **Real 25-hour soak**: reported as RUNNING or NOT RUN until a run on the code in question
   has actually elapsed and been evaluated.

Status words: **PASS**, **FAIL**, **NOT RUN** (with the reason), **RUNNING**.

## Code under test

- Branch `v0.2.0`; original candidate evidence is in section 2.4; subsequent independent-review
  fixes and their separate verification are in section 2.5.
  - It adds the notifier ([`notifier.md`](notifier.md), [`decisions.md`](decisions.md) D12).
  - It fixes the findings of the independent audit of v0.1.0 (D13).
  - The independent-review fixes change code and tests; earlier real-CLI results are historical.
- macOS (Darwin 25.2.0, arm64), Python 3.14.3, SQLite 3.52.0.
- Claude Code 2.1.301 (subscription login), Codex CLI 0.163.0 (ChatGPT login).
- Linux and Python 3.11 / 3.12 / 3.14 are covered by CI
  ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml), free `ubuntu-latest`). It runs on
  the pull request.

## How to reproduce

```sh
python3 -m unittest discover -s tests -t tests -v                       # ~4 min, fixture engine and a local fake webhook
python3 scripts/soak.py run --dir SOAK --hours 0.03                     # scaled-down soak, exit 0 only on PASS
python3 scripts/real_cli_acceptance.py --engine claude --notify --home H --workdir W --out claude.json
python3 scripts/real_cli_acceptance.py --engine codex  --notify --home H --workdir W --out codex.json
python3 scripts/soak.py start --dir SOAK25 --hours 25                   # the real 25 h soak
python3 scripts/soak.py verdict --dir SOAK25                            # evaluate it when finished
```

Each real-CLI run makes two tiny model calls plus one request rejected for an unknown model,
all on the existing subscription.
- It refuses to run (NOT RUN, exit 3) unless `doctor --engine --cwd` confirms a subscription
  login and configuration for the work directory.
- With `--notify` it points the run's notifier at a webhook on 127.0.0.1 that the script itself
  starts. No real Bot is contacted.
- It exits 0 only on PASS.

## 1. Argument

[`reliability.md`](reliability.md) covers:
- the attempt state machine;
- the crash windows W0–W9 and what each leaves durable;
- the notifier's crash windows N1–N9 and its bounds;
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
| D6 | Acknowledged events are filtered in SQL: an empty poll fetches no acknowledged rows into Python. SQLite still scans the event table, so the cost grows with history; no performance claim is made | `test_empty_poll_reads_no_acknowledged_rows` | PASS |
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
| J2 | CI on `ubuntu-latest` with Python 3.11 / 3.12 / 3.14 | workflow in this repository | v0.1.0: PASS on `baff533` ([push run](https://github.com/Yevanchen/cli-relay/actions/runs/39197188143), [tag run](https://github.com/Yevanchen/cli-relay/actions/runs/39197203482)). v0.2.0: runs on the pull request; see section 2.4 |

### 2.2 Notifier (fixture engine, local fake webhook; no model, no real Bot)

| # | Requirement | Evidence | Result |
|---|---|---|---|
| N1 | One wake-up per terminal event, naming source, ledger, route and event/task/attempt; transport accepted is kept apart from the consumer ACK; an acknowledged event is never sent again | `test_one_wake_with_routing_identity_then_the_consumer_ack_closes_it` | PASS |
| N2 | Only configured outcomes wake the consumer; `cancelled` is opt-in; no route means no wake-up | `test_only_the_configured_outcomes_wake_the_consumer`, `test_cancelled_is_not_a_default_wake_and_no_route_means_no_wake` | PASS |
| N3 | Crash before the send: sent once by the next pass | `test_crash_before_send_is_sent_once_by_the_next_pass` | PASS |
| N4 | Crash after the send: never resent at once; one bounded reminder; then `unacknowledged` | `test_crash_after_send_is_never_resent_blindly` | PASS |
| N5 | Receiver down, then back: retried with backoff, sent once | `test_outage_then_recovery` | PASS |
| N6 | Response lost: `unconfirmed`, bounded reminder | `test_lost_response_is_unconfirmed_and_reminded_within_bounds` | PASS |
| N7 | Receiver restart between two events: only the event not yet accepted is sent again | `test_server_restart_between_two_events` | PASS |
| N8 | 429 with `Retry-After`: honoured, then accepted | `test_rate_limited_then_accepted` | PASS |
| N9 | Bounded retries: exactly `max_attempts` calls, then `failed` (kept) | `test_retries_are_bounded` | PASS |
| N10 | Concurrent passes and a duplicate submit: one send | `test_concurrent_passes_and_duplicate_submits_send_once` | PASS |
| N11 | A lost consumer ACK: one reminder; a repeated ACK is a duplicate; then nothing more | `test_a_lost_consumer_ack_gets_a_bounded_reminder` | PASS |
| N12 | An ACK during a send: the send stays on record | `test_an_ack_during_a_send_keeps_the_send_on_record` | PASS |
| N13 | Accepted but never acknowledged (e.g. the Bot run failed on its rate limit): never cleared; only an explicit retry sends again | `test_unacknowledged_events_are_never_cleared` | PASS |
| N14 | Two hosts: each names itself; an ACK never lands on the other ledger | `test_each_host_names_itself_and_acks_never_cross` | PASS |
| N15 | `notify.sh`-style command adapter: five arguments, one sanitized line, exit 0 recorded as `handed_off` (not accepted), bounded reminder | `test_exit_0_is_a_hand_off_not_an_acceptance` | PASS |
| N16 | The keeper starts a pass when the task ends | `test_the_keeper_starts_a_pass_when_the_task_ends` | PASS |
| N17 | The token never appears in output or logs | `test_the_token_never_appears_in_output_or_logs` | PASS |
| N18 | Invalid configuration is refused, nothing sent; an unknown route is refused at submit | `test_invalid_configuration_is_refused_and_nothing_is_sent`, `test_a_route_must_exist_in_the_configuration` | PASS |
| N19 | Migration: backup first, idempotent, earlier events not notified (unless `--backfill`); an unmigrated ledger refuses notifier commands | `test_migrate_backs_up_then_notifies_only_new_events`, `test_backfill_includes_earlier_events`, `test_unmigrated_ledger_refuses_notifier_commands` | PASS |
| N20 | Rollback: v0.1.0 (`baff533`) still reads a migrated ledger (status, events) | `test_v010_code_still_reads_a_migrated_ledger` (skipped where the checkout lacks that commit) | PASS |
| N21 | The real-CLI acceptance script's notify step (fixture mode) | `test_notify_step_sends_each_terminal_event_once` | PASS |

### 2.3 Independent audit of v0.1.0 (regression tests; each failed before its fix)

| # | Requirement | Evidence | Result |
|---|---|---|---|
| I1 | The soak verdict fails a run without events, and a run with one attempt missing its event | `test_zero_events_acks_and_deliveries_fail`, `test_one_attempt_without_its_event_fails`, `test_the_same_run_with_its_events_delivered_and_acked_passes` | PASS |
| I2 | Codex permissions: permission settings, requirements, unsafe or unreadable selected profiles block the run; after it, a wider sandbox, a named profile or a missing record blocks | `test_ind_02_codex_permissions` (10 tests) | PASS |
| I3 | Cancel racing reconciliation converges to `cancelled` | `test_cancel_after_the_snapshot_is_seen_by_the_closing_write`, `test_an_attempt_already_stuck_in_unknown_with_a_cancel_converges` | PASS |
| I4 | A SIGTERM-ignoring descendant in the group is stopped before the cancelled state is committed | `test_a_term_ignoring_descendant_is_stopped_before_the_commit`, `test_lists_live_members_of_a_group` | PASS |
| I5 | A missing identity part is unverifiable | `test_ind_05_linux_empty_boot_id` (4 tests) | PASS |
| I6 | Unexpected configuration structure converges to `blocked` with one event and no CLI start | `test_ind_06_config_anomalies` (3 tests) | PASS |
| I7 | Tests do not depend on the host environment | `test_base_env_carries_no_billing_variable_or_host_configuration`, `test_guard_tests_pass_with_billing_variables_and_hostile_user_settings_on_the_host` | PASS |
| I8 | The events-query efficiency statement matches the evidence | D6 above (wording corrected) | PASS (documentation) |

### 2.4 Historical short-test results (candidate `d27b849`)

All of these are short tests on the original candidate, before the fixes in D14. They do not
certify the revised code and none is a long-run result.

- **Code.** The product code (`cli_relay/`, `scripts/`, `pyproject.toml`, CI) is identical in
  every run below. Subsequent D14 repairs change code; the following results remain historical.
- **Fault-injection suite with the notifier scenarios.** 156 tests: the v0.1 suite, the
  regression tests for both audits, and 24 notifier scenarios against a local fake webhook.
  - **156/156 passed in each of two consecutive rounds** on the final tests (209.8 s and
    209.9 s), and in two earlier rounds (210.2 s and 209.6 s).
  - No test was skipped on macOS. On Linux, CI skips the macOS-only tests.
- **Regression tests for the independent audit, before the fixes.** On the unfixed code they gave
  25 tests (with subtests): 22 failures, 2 errors, and 5 passing controls. Every finding had a
  failing test.
- **Scaled-down soak (0.03 h), new verdict rules.** **PASS, 13/13 verdicts.**
  - 15 attempts, each with its event delivered and acknowledged.
  - The long task ran 0.029 h of its configured 0.029 h; 11 short tasks ran.
- **Real Claude Code loop with the notify step, 2 runs.** **PASS 2/2**, every run 6/6 steps.
  - Preflight: `claude.ai`.
  - Success, failure parsing (exit 1) and exact resume behaved as before.
  - Notify step: each of the 3 terminal events was sent exactly once to the script's own local
    webhook, naming this ledger, source and route. The transport was accepted, and every
    notification was `acked` after the consumer ACKs.
- **Real Codex loop with the notify step, 2 runs.** **PASS 2/2**, every run 6/6 steps.
  - Preflight: ChatGPT login.
  - The thread record named `openai`, and every turn ran `read-only` with no named permission
    profile, so the new post-run permission check (I2) passed on the real CLI (codex-cli
    0.163.0).
  - The notify step was as for Claude.
- **Raw reports.** They are kept locally and not committed, because they contain local paths
  and session ids.

### 2.5 Independent-review repair verification

D14 has focused regressions in `test_notifier_faults`, `test_notifier_routing`,
`test_ind_04_stop_evidence`, `test_resume_permission_checkpoint` and the existing isolation tests.
They cover adapter exceptions without secret exposure, abandoned claims with a still-live
sender, ACK-before-recovery, conservative wake bounds, server retry delays, fixed owners and
migration, incomplete cancellation cleanup, and old resume evidence that cannot verify a new turn.

The merge gate is the complete isolated suite and CI on the repaired code; run-specific results
are recorded in [PR #2](https://github.com/Yevanchen/cli-relay/pull/2) and its linked checks.
Section 2.4 is historical and is not substituted for those checks. Fixtures use temporary ledgers,
local fake HTTP and fake CLIs; they make no real model or Grok calls.

A stopped keeper can leave a live CLI briefly in `unknown` before it finishes as `blocked`.
The billing-guard regression waits for `attempt.state=finished` before asserting the final
outcome; it must not mistake this observable intermediate state for a completed cancellation.
Real two-host notification, model/Max settings and the 30-minute window remain **NOT RUN**.

## 3. Real 25-hour soak

| Run | Code | Status |
|---|---|---|
| 25 h soak started 2026-10-19 03:28 UTC+8 | `b72618d` (v0.1 before both audits) | Historical run; current status not rechecked (planned end 2026-10-20 ≈04:30 UTC+8). Its own summary uses the old verdict rules; evaluate it with `soak.py verdict` from this version. The result is about `b72618d` only |
| 25 h soak on v0.1.0 | `a431845` | NOT RUN |
| 25 h soak on v0.2.0 | this branch | **Not completed. Does not block this controlled rollout.** Nothing here is claimed to have run for 25 hours |

## 4. Deployment acceptance

| # | Requirement | Status |
|---|---|---|
| K1 | Mac host-a: notifier installed, old path retired, one low-risk native-CLI task wakes the right Bot, which reports in the right chat and ACKs; no follow-up question needed | **NOT RUN** (pending deployment, after the independent review) |
| K2 | Second machine: the same | **NOT RUN** (pending deployment) |
| K3 | A real 30-minute task window: ordinary progress does not wake the Bot, the end does, once | **NOT RUN** (pending deployment) |
| K4 | Real Grok run count and chat evidence per event (task, event, host, send time, run, chat, ACK) | **NOT RUN** (pending deployment) |

## What this does not show

- Exactly-once external side effects. They are not claimed.
- Delivery to any chat bot, phone or person. The relay stores results and re-delivers them;
  transport notification is implemented, but actual chat delivery still requires the deployment checks above.
- Behaviour across real machine sleep, power loss, reboot or a full disk.
- That every possible CLI configuration is recognised by the subscription check.

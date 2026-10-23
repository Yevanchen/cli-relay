# Reliability model

This document states what cli-relay guarantees, under which preconditions, and where the
guarantees stop. It is the **argument** (theory). The evidence comes in two other kinds, reported
separately in [`acceptance.md`](acceptance.md):
- **short tests:** fault injection with real OS processes, a local fake webhook, and a few real
  CLI loops;
- **the real 25-hour soak.**

None of the three stands in for another. Their current status is in "Evidence" at the end.

## Components and ownership

```
caller (shell / SSH / bot) ──short JSON commands──▶ ledger (SQLite, local disk)
                                                     ▲
           double-forked keeper (one per attempt) ───┘ claims, records, commits
                     │ spawns, waits
                     ▼
           official CLI (claude / codex) ── stdout/stderr ──▶ files in the attempt directory
```

- **Ledger.** Holds tasks, attempts, events and acks.
  - All writes use `BEGIN IMMEDIATE` with `synchronous=FULL` (WAL).
  - Every state change is a compare-and-set on the expected previous state.
  - Nothing is ever deleted.
- **Keeper.** One short-lived process per attempt, re-parented to init in its own session.
  - It is the only process that starts the CLI for that attempt.
  - It records facts and exits when the CLI exits.
  - There is no resident service.
- **CLI.**
  - Its stdout/stderr go to files, not pipes, so it survives the death of its keeper.
  - It stays in the keeper's process group, so an orphaned CLI is still detectable.
- **Reconciliation.**
  - Runs inside the short commands (`status`, `wait`, `events`, `submit` for duplicates, `reconcile`).
  - It only closes what evidence proves.
  - The only thing it restarts is a keeper for an attempt whose CLI provably never started.
    Any of these commands does that, including plain `status`/`events` polling, once the
    attempt has been pending for the 5 s grace.
  - A keeper started this way inherits the environment of the command that started it. The
    keeper's preflight runs again in that environment (billing variables, configuration, login).

## State machine

Attempt states:

```
pending ──claim (CAS)──▶ claimed ──record 'spawning'──▶ (spawn) ──record pid──▶ running ──exit──▶ finished
   │                         │                                                     │
   │ cancel                  │ keeper died, no 'spawning' record                   │ keeper died
   ▼                         ▼                                                     ▼
finished(cancelled)      pending (claim released; may get a new keeper)    reconcile: finished (exit.json or
                                                                           native result) | unknown | keep observing
unknown ──late native result──▶ finished           unknown / finished ──explicit resume──▶ new attempt (pending)
```

Task state is derived from the current attempt:
- `accepted` = pending;
- `running` = claimed or running;
- `unknown`;
- or the outcome: `succeeded | failed | cancelled | needs_input | blocked`.

A `terminal` event is written in the same transaction as `finished`. An `unknown` event is
written in the same transaction as `unknown`.

Three facts are stored separately and never substituted for one another:
- **CLI exit status** (`exit.code/signal/known`), taken from the keeper's `waitpid`.
- **The CLI's own result.** Claude's final `result` event, or Codex's `turn.completed` /
  `turn.failed` plus the last agent message. The real-CLI acceptance found Claude can report
  `subtype: "success"` with `is_error: true`, so both fields are checked.
- **Business acceptance.** Never inferred by the relay. It is the consumer's decision; ACK
  only means "the consumer has the event". A task may require the CLI's own completion marker
  (`require_completion_marker=true`): then `succeeded` also needs the exact last line
  `CLI-RELAY-STATUS: complete` in the CLI's final message, and a run without it is `needs_input`.
  That is still the CLI's self-report, not acceptance ([`decisions.md`](decisions.md) D15).

Git observations are separate attempt evidence, never an outcome gate or request-payload input.
For a detected work tree, `git` contains `head_before`, `head_after` and the final `dirty` flag;
unavailable observations are `null`. A dirty tree adds an `uncommitted_changes` warning,
including when those changes predate the attempt. The keeper persists the initial snapshot
before spawn and the final snapshot in `exit.json`. Reconciliation reuses the saved final
snapshot; if the keeper died before saving one, it observes the tree after the processes stop.
Git commands use argv, a two-second timeout per call, disabled optional index writes and
disabled fsmonitor hooks. No paths, diffs, Git errors or tool inputs are added to events.
Codex `workspace-write` leaves `.git` read-only and cannot commit; the caller must handle
committing outside that sandbox.

## Persistence boundaries and crash windows

The SQLite commit and the OS `spawn` cannot be atomic. The design makes the window between
them observable rather than pretending it does not exist.

| # | Crash point | What is durable | What reconciliation does | Test |
|---|---|---|---|---|
| W0 | before the submit commit | nothing | nothing to do; the caller retries with the same key and a new task is created | — |
| W1 | after the submit commit, before a keeper starts (reply lost) | task + pending attempt | after the 5 s grace, a duplicate submit (same key) or any `status`/`events`/`wait`/`reconcile` starts a keeper; the claim CAS allows exactly one | `test_submit_crash_after_commit_before_keeper`, `test_resubmit_within_grace_then_only_status_polling` |
| W2 | keeper dies after claim, before the `spawning` record | claimed, no spawn record | **proof** the CLI never started (the spawn record is committed before `Popen`): the claim is released and the attempt re-kicked | `test_keeper_crash_after_claim_is_released_and_restarted` |
| W3 | keeper dies after the `spawning` record, before `Popen` | spawn record, no pid | process group empty, no output, no exit record → `unknown`; never re-run | `test_keeper_crash_before_spawn_is_unknown_and_not_rerun` |
| W4 | keeper dies right after `Popen`, before the pid record | spawn record, no pid | the keeper's process group is alive → keep observing; when it empties, close from the CLI's native result with `exit.known=false`, else `unknown` | `test_keeper_crash_after_spawn_before_pid_record` |
| W5 | keeper killed while the CLI runs | pid + start time of keeper and CLI | CLI identity verified alive → observe; afterwards close from the native result (`exit.known=false`) or `unknown`; a billing violation in the run's own output still closes it as `blocked` | `test_keeper_killed_while_cli_keeps_running`, `test_keeper_dies_while_the_cli_runs` |
| W6 | CLI killed | keeper alive | the keeper records the signal → `failed`; no re-run | `test_cli_killed_is_recorded_as_failed_not_rerun` |
| W7 | keeper dies after writing `exit.json`, before the terminal commit | `exit.json` (fsynced, atomic rename; includes any billing guard the keeper tripped) | close with the real exit code from `exit.json`, through the same outcome decision as the keeper | `test_keeper_crash_after_exit_before_commit`, `test_keeper_dies_after_exit_record`, `test_codex_provider_record_is_checked_after_a_keeper_crash` |
| W8 | keeper dies after the terminal commit | terminal state + event | nothing | `test_keeper_crash_after_commit_is_harmless` |
| W9 | consumer loses the ACK reply / ACKs twice | ack row | ACK is idempotent per `(event, consumer)` and rejects mismatched task/attempt/event | `test_lost_or_repeated_ack_is_idempotent`, `test_mismatched_ack_is_rejected` |

Process identity is always pid + OS-reported start time, from the kernel only:
- Linux: `/proc/<pid>/stat` field 22 (clock ticks, typically 10 ms) plus the boot id;
- macOS: the kernel's `kinfo_proc.p_starttime` (microseconds), validated against the pid
  stored in the struct.

There is no fallback to another source. `ps -o lstart` was dropped for two reasons:
- It has one-second resolution, which was too coarse: the suite caught a pid pointed at a
  process started within the same second.
- A token from another source or precision cannot be compared with the recorded one. Doing
  so made a live keeper look recycled.

So these answer "cannot verify":
- a failed kernel query;
- a recorded token of another source;
- a token with any missing part, even when both sides are missing the same part. A Linux token
  whose boot id could not be read is an example.

On Linux a different (readable) boot id means "gone".
Tests: `test_failed_kernel_query_does_not_fall_back_to_a_different_token`,
`test_live_keeper_with_an_incomparable_token_keeps_its_claim`, `test_both_boot_ids_missing`.

A recycled pid is treated as "gone" (`test_recycled_pid_is_not_mistaken_for_the_keeper`). An
unverifiable answer is treated as "may be alive" and blocks closing and resuming. A lock, a pid
or a stale timestamp alone is never accepted as proof that something did not run.

Signals follow the same rule.
- `cancel` signals the keeper only if its identity is verified.
- If the keeper is gone, `cancel` signals the CLI only if the CLI's own identity is verified.
  The recorded process group is signalled only while that verified CLI is still a member: a
  group id cannot be reused while a member exists.
- Otherwise nothing is signalled. The request stays in the ledger and the reply says why
  (`test_cancel_does_not_signal_a_recycled_process_group`).
- After a cancel or a guard stop, the keeper stops its whole process group except itself: the
  CLI, and tool processes the CLI left in the group.
  - Every other member gets SIGTERM, then SIGKILL after the grace (10 s).
  - The terminal state is committed only once the group is empty, and the evidence records
    `terminated`, `killed` and `remaining`
    (`test_a_term_ignoring_descendant_is_stopped_before_the_commit`).
  - Members are listed from the kernel (`kinfo_proc` on macOS, `/proc` on Linux).
  - A group id cannot be reused while the keeper is in it, so every member is the attempt's own.
  - After a normal exit, lingering members are left alone but counted in the evidence.

Concurrency rules:
- **Concurrent submits** of one key converge on one task and one execution (`BEGIN IMMEDIATE` plus a unique `(scope, key)` index).
- **Concurrent resumes** create exactly one new attempt, via a CAS on `current_attempt`.
- **A stale keeper** can only move its own attempt's state, and only from the expected state.
  A superseded attempt cannot change the task's state.
  - The state-guarded writes are claim, spawn record, pid record, finish and unknown.
  - The native session id and added evidence are written without a state guard. They only
    add facts about that attempt, and the outcome never depends on them alone.
- **Cancel vs. completion** yields exactly one terminal event. If completion wins,
  `cancel_lost_race` is recorded. This is decided inside the transaction that closes the
  attempt, so a cancel committed just before that commit is not missed
  (`test_cancel_between_outcome_decision_and_commit`).
- **Cancel vs. reconciliation.** Reconciliation decides from a snapshot. The write that would
  mark the attempt `unknown` re-reads it in its own transaction: if a cancel was committed
  meanwhile, the attempt is closed as `cancelled` (stopped without a result after a cancel).
  - An attempt that an earlier version left `unknown` with a cancel recorded converges to
    `cancelled` once its processes are provably gone
    (`test_cancel_after_the_snapshot_is_seen_by_the_closing_write`,
    `test_an_attempt_already_stuck_in_unknown_with_a_cancel_converges`).
- **A preflight never crashes the keeper.**
  - A configuration of unexpected structure is itself a reason to block.
  - Any other exception in the preflight or in building argv closes the attempt as `blocked`.
  - Otherwise reconciliation would release the claim and re-kick a keeper that crashes the same
    way (`test_the_task_reaches_blocked_with_one_event_and_no_cli_start`).

## Subscription-only and permission guards

Permission posture does not depend on the CLIs' configuration of the moment:
- Codex always gets an explicit sandbox: `read-only` by default, `workspace-write` when requested,
  or `danger-full-access` only with the additional typed boolean `allow_full_access=true`.
  Full access is off by default and disables the Codex sandbox, allowing access outside the task
  directory and network use subject to host permissions. The boolean alone is harmless.
  The same sandbox is passed on resume, both as the exec-level option and
  as a `sandbox_mode` override, because `codex exec resume` has no sandbox option of its own.
- A Codex `workspace-write` run also gets an explicit `sandbox_workspace_write.network_access`,
  on the first run and on resume: `false`, which blocks every connection including loopback, unless
  the task opts in with the typed boolean `allow_network=true`. The option is a usage error with any
  other sandbox and never selects full access. Codex has no loopback-only setting, so the opt-in
  allows all network access from the sandbox; writes stay limited to the workspace.
- Claude always gets `--permission-mode dontAsk --permission-prompts none`, including resume.
  Preflight only accepts a user/project bypass default because that fixed CLI override replaces
  it. A conflicting managed mode, a stricter `plan` default, and malformed checked settings
  still block. User/project settings
  and their allow/ask/deny rules remain loaded; the relay does not isolate HOME, copy credentials,
  skip setting sources, or bypass approvals. Existing allowed tools can still run.
  Every Claude run must report `permissionMode: dontAsk` in its init event; a mismatch or missing
  init blocks, including when reconciliation closes the attempt after a keeper crash.
- A Claude task adds no tool unless it opts in with `allowed_tools`, a list of exact names from a
  fixed allowlist (`WebSearch`, `WebFetch`). That adds one `--allowedTools` value for the task,
  on every attempt, and changes neither the mode nor any settings layer. Deny and ask rules of
  every layer, and managed `allowManagedPermissionRulesOnly`, still win. Native success plus a
  known zero exit treats denied tools as `permission_denials` warnings (tool names only), with
  the completion marker gate still applied. Otherwise denials mean `needs_input`. Cancellation
  precedence and the allowlist are unchanged; `Monitor` is not allowed by the relay.
- Argv checks reject `danger-full-access` unless both task options opt in, a workspace network
  setting other than the one built from the task's options, and Claude `--allowedTools` unless it
  carries exactly the task's stored list. Every permission-bypass flag, including
  `--dangerously-bypass-approvals-and-sandbox`, `--yolo` and `--full-auto`, stays forbidden.
- `--sandbox` is not the only thing that decides a Codex run's permissions: named permission
  profiles and requirements can take precedence. Codex runs, including opted-in full-access runs,
  are checked on both sides:
  - before the run, a permission setting, a requirements or managed-preference layer, or an unsafe or
    unreadable config-profile selected by `profile` blocks the run; benign profiles are parsed;
  - after the run, every `turn_context` in the thread's rollout must show the requested sandbox
    (or a narrower one), no network access unless the task opted in (`network_access` missing or
    `false`), and no active named permission profile. Otherwise the attempt is
    `blocked`; a thread without that record is `permissions_unverified`. Before an exact resume,
    the keeper records the rollout path, file identity and byte boundary in attempt evidence.
    A resumed attempt also needs a new `turn_context` after that boundary. Missing evidence,
    replacement or truncation of that file is unverified; reconciliation uses the same saved
    boundary, so an earlier safe turn cannot certify a later attempt.

The relay refuses to run, or stops and closes as `blocked`, whatever it can see routing a run
away from the subscription login. It checks:

1. **Before the CLI starts (keeper preflight; also `doctor --engine E --cwd D`):**
   - billing or gateway variables in the environment (names only are reported);
   - the CLI's configuration layers that apply to the task directory:
     - Codex: system, user (`$CODEX_HOME` or `~/.codex`), the active profile and the
       config-profile file it selects, every `.codex/config.toml` from `/` down to the task
       directory, and managed. These block the run:
       - a non-default `model_provider`;
       - an override of the built-in `openai` provider;
       - any `*base_url` key;
       - an API login method;
       - a permission setting.
     - Claude: user (`$CLAUDE_CONFIG_DIR` or `~/.claude`), `.claude/settings.json` and
       `settings.local.json` of the task directory and its parents, and managed. A
       credential helper (`apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`), a
       non-claude.ai `forceLoginMethod`, or a billing variable in `env` blocks the run.
     - An unreadable layer or an invalid checked routing/permission value blocks. This is
       not a complete validator for every value accepted by the vendor configuration format.
   - the login (`claude auth status` → claude.ai / firstParty; `codex login status` → ChatGPT).
2. **While it runs:** a Claude init event that reports an API-key source or a different permission mode
   stops the CLI.
3. **After it ran:** the Codex thread's own record must name `openai`
   (`session_meta.model_provider` in its rollout) and show the requested sandbox and network
   access for every turn. A missing record also counts as unverified.

The keeper and reconciliation name the outcome through one function. The guard result is
therefore the same even when the keeper died before committing: the init event is re-read
from the saved output, the guard the keeper tripped is kept in `exit.json`, and the Codex
record is looked up where the keeper's run wrote it.

Task-local `model` and `effort` options are reused on exact resume. For Claude, the effort flag
is paired with a generated CLI settings layer containing only `env.CLAUDE_CODE_EFFORT_LEVEL`;
it takes precedence over user/project environment defaults while leaving credentials and rules
alone. A conflicting managed environment override or a lower `maxEffortLevel` from any checked
settings layer blocks at preflight: the lowest cap applies, even below managed scope.
Codex accepts only enumerated effort and safe sandbox config overrides.
These settings express the requested model/effort; provider availability and server-delivered
limits need real-run evidence. In particular, Claude may silently clamp effort under organization
policy in stream-json output. See the official [settings precedence](https://code.claude.com/docs/en/settings),
[effort environment variable](https://code.claude.com/docs/en/env-vars), and
[effort limits](https://code.claude.com/docs/en/model-config#organization-effort-limits).

Limits:
- cli-relay reads the configuration files itself; it does not run the CLIs' own config
  resolvers. A future routing setting it does not know about is not detected before the run.
- The Codex record names the provider when the thread is created. A resumed turn therefore
  relies on the preflight of that resume.

## Failure model

1. **Caller or SSH session disconnects.**
   - The task belongs to the execution machine. The keeper runs in its own session, so
     SIGHUP/SIGKILL to the caller's session does not reach it
     (`test_caller_session_teardown_does_not_stop_the_task`).
   - Killing a `wait` does not affect the task.
   - Any later process can query the same task by id or key. This was tested with a bare
     environment as a second entry point.
2. **Control layer restarts.**
   - There is no resident control process: every command is short-lived and re-reads the
     ledger, so a restart is just the next command.
   - Business commands are never re-submitted by the relay.
   - The soak driver re-executes itself mid-run to exercise this.
3. **Keeper or CLI crash.**
   - Known facts are kept. Outcomes that cannot be proven become `unknown`, and the relay never re-runs them.
   - `resume` requires proof that the old run stopped: the keeper, the CLI and the process group are all gone. It then continues the exact native session id, never "latest".
   - It cannot promise that the last step of the previous run is not repeated.
4. **Machine sleep, power loss or reboot.**
   - Execution is not guaranteed to continue.
   - After the machine and disk come back, the ledger is readable and open attempts are
     reconciled (recycled pids are recognised by their start time).
   - The work can be resumed explicitly in the native session.
   - There is no process-memory checkpointing and no cross-machine or cross-engine migration.
   - The soak run records wall-clock gaps (sleep) instead of hiding them.
5. **Consumer offline or notification failure.**
   - Terminal results stay retrievable with stable event ids and are re-delivered until ACKed.
   - Progress requires the consumer to read again, or an existing wake-up mechanism. Without
     that, nothing guarantees the consumer is ever notified.
   - HTTP 200, "written to the receiver's disk", "bot replied", "push notification" and
     "user read it" are different levels. The relay covers none of them beyond storage and
     re-delivery.

## Safety and liveness

**Safety.** These hold on any schedule of crashes:
- no fabricated success (an outcome needs the CLI's own result; with `require_completion_marker`,
  `succeeded` also needs the CLI's own complete marker);
- no loss of a committed terminal state or event (no delete paths: `test_ledger_code_never_deletes`);
- no automatic re-run;
- at most one CLI per attempt;
- ACKs never move to another task or attempt;
- no signal to a process whose identity is not verified;
- the CLI only starts on a prompt that was stored in full.

**Liveness.** A submitted task reaches a terminal state only if all of the following hold:
- the machine stays up and its disk is writable;
- the CLI can make progress (login valid, quota available, network);
- the consumer eventually reads again (or something wakes it).

Storage faults are explicit:
- low disk space refuses new work before acceptance and again before spawn;
- a re-sent known key still gets its task back, even when the disk is low or the task
  directory is gone (these are preconditions for new work only);
- atomic file writes repeat short writes until every byte is written, and only rename a
  complete temp file;
- a prompt that cannot be stored in full closes the attempt as `blocked` before the CLI starts;
- a corrupt database stops new executions without being replaced;
- a missing database next to an existing marker is never silently recreated;
- a read-only ledger fails the submit without reporting `accepted`;
- the ledger home is made absolute at the command entry, so a relative `--home` or
  `CLI_RELAY_HOME` reaches the keeper as the same ledger.

Without a backup, physical corruption cannot be repaired.

## Why the 24-hour argument does not depend on a short window

- The relay has no timer that kills, expires or deletes anything:
  - the keeper waits on its child without a deadline;
  - `wait --timeout` only bounds the caller's waiting;
  - there is no TTL on results;
  - the only time constant with an effect is a 5 s grace before re-kicking a *pending* attempt that provably never started.
- A long task's progress depends on the CLI and the machine, not on the relay.
- What this does **not** prove is a 24 h SLA. The 25-hour soak (`scripts/soak.py`) exercises the
  argument with a real clock:
  - a fixture task that runs ~98% of 25 h;
  - a keeper killed mid-run;
  - an unknown task re-run only explicitly;
  - duplicate submits every half hour;
  - a consumer offline for 12.5 h;
  - a control-process restart.

  Its result is reported as RUNNING until it has actually elapsed.
- The verdict counts what actually ran:
  - every key must reach `succeeded` with exactly its expected number of executions;
  - the run must last its planned time;
  - the long task must run at least 95% of its configured duration;
  - every attempt must have its event (terminal or unknown), delivered to and acknowledged by
    the soak consumer. A run that produced no events cannot pass.

  A soak started before these rules existed must be re-evaluated with `soak.py verdict --dir
  DIR`, which only reads its files.

## Notifier (v0.2)

The notifier ([`notifier.md`](notifier.md)) is a separate, deterministic program that turns
committed events into wake-ups of the consumer. It cannot change a task: it reads tasks and
events, and writes only its own `notifications` rows.

| # | Crash or fault | What is durable | What happens | Test |
|---|---|---|---|---|
| N1 | the task ends, no pass runs (machine off, keeper died after the commit) | the event | the next pass enqueues and sends it | `test_migrate_backs_up_then_notifies_only_new_events` |
| N2 | a pass dies after claiming, before marking `sending` | `claimed` + a dead claimer | provably not sent: back to `pending`, sent once by the next pass | `test_crash_before_send_is_sent_once_by_the_next_pass` |
| N3 | a pass dies after `sending` (possibly after the POST) | `sending` + a dead claimer | may have woken the consumer: `unconfirmed`, no immediate resend, at most a bounded reminder | `test_crash_after_send_is_never_resent_blindly` |
| N4 | the receiver is unreachable or restarts | `pending` with `next_at` | exponential backoff up to `max_attempts`, then `failed` (kept) | `test_outage_then_recovery`, `test_server_restart_between_two_events`, `test_retries_are_bounded` |
| N5 | the request may have arrived but the response is lost or times out | `unconfirmed` | as N3 | `test_lost_response_is_unconfirmed_and_reminded_within_bounds` |
| N6 | 429 / 5xx | `pending` with `next_at` (≥ `Retry-After`), or `failed` if a bound is reached | counts as a possible wake; retried within both bounds | `test_rate_limited_then_accepted` |
| N7 | accepted, but the consumer never ACKs (its run failed, its ACK was lost) | `accepted` / `handed_off` | one reminder per `ack_timeout_s` up to `max_wakes`, then `unacknowledged`; never cleared | `test_a_lost_consumer_ack_gets_a_bounded_reminder`, `test_unacknowledged_events_are_never_cleared` |
| N8 | concurrent passes, repeated passes, duplicate submits | one row per (event, route) | compare-and-set claims: one send | `test_concurrent_passes_and_duplicate_submits_send_once` |
| N9 | an ACK arrives during a send | `sending` | the sender records its send; the next pass records the ACK | `test_an_ack_during_a_send_keeps_the_send_on_record` |

Guarantees, given that the machine runs, the ledger is writable and a pass runs from time to
time (the keeper's own pass plus `notify run`):
- an event is offered to its stored owner while that route is valid; missing or changed owners,
  permanent transport rejection or exhausted bounds leave a visible failure for explicit repair;
- no event is offered more than `max_attempts` times, and no more than `max_wakes` of those
  calls may have reached the consumer; this includes HTTP error responses and started commands
  that exit non-zero, and it does not measure how many model runs the receiver actually starts;
- a notification never runs, resumes or re-runs a task;
- the transport result and the consumer ACK are stored separately; neither is inferred from
  the other or from pull reads.

Not guaranteed:
- that a wake-up leads to a chat message, or that anyone reads it;
- that the Bot is woken only once per event (bounded duplicates are possible, see N3, N5, N7);
- anything while the machine is asleep or off.

Two machines are two independent ledgers and notifiers. Every payload names `source_id` and
`ledger_id`, and `ack --ledger-id` refuses an event of another ledger
(`test_each_host_names_itself_and_acks_never_cross`).

## Residual risks

- If the CLI moved itself to a new process group *and* the keeper died in the microseconds
  between `Popen` and recording the pid (W4), the orphan would be invisible:
  - the attempt would become `unknown` while the CLI still runs;
  - a later explicit resume could then run concurrently with it.

  Neither official CLI is known to change its own process group.
- Cancellation sends SIGTERM to the keeper's process group. Tool subprocesses that the CLI
  started in their own process groups are left to the CLI's own shutdown handling.
- A cancel verifies the identity of a process and then signals its pid. macOS has no pidfd,
  so a pid that exits and is reused in the microseconds between those two steps would be hit.
- Cancellation cannot undo work the CLI has already done. "No side effect" in the cancel test
  only reflects the fixture's timing.
- A same-UID local user can edit the ledger. The ledger is not a security boundary.
- Persisted timestamps use the wall clock. Clock jumps can affect the re-kick grace, retry
  deadlines, ACK reminder timing and reporting; they do not prove whether a process stopped.


## Evidence

The three kinds are kept apart:

1. **Argument**: this document.
2. **Short tests**: the fault-injection suite (fixture engine, real OS processes), the notifier
   scenarios against a local fake webhook, a scaled-down soak, and a few real Claude Code and
   Codex loops. The results are in [`acceptance.md`](acceptance.md); they show the mechanisms,
   not long-run behaviour.
3. **The real 25-hour soak**: not completed for this version. It does not block this controlled
   rollout, but nothing here is claimed to have run for 25 hours.

Two-machine deployment, and a real 30-minute window with the real Bot, are **NOT RUN** (pending
deployment).

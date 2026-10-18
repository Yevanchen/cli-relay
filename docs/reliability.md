# Reliability model

This document states what cli-relay guarantees, under which preconditions, and where the
guarantees stop. Mechanism evidence (this document plus the fault-injection tests) and the real
25-hour soak are reported separately in [`acceptance.md`](acceptance.md). Neither stands in
for the other.

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
  only means "the consumer has the event".

## Persistence boundaries and crash windows

The SQLite commit and the OS `spawn` cannot be atomic. The design makes the window between
them observable rather than pretending it does not exist.

| # | Crash point | What is durable | What reconciliation does | Test |
|---|---|---|---|---|
| W0 | before the submit commit | nothing | nothing to do; the caller retries with the same key and a new task is created | — |
| W1 | after the submit commit, before a keeper starts (reply lost) | task + pending attempt | a duplicate submit (same key) or `reconcile` starts a keeper; the claim CAS allows exactly one | `test_submit_crash_after_commit_before_keeper` |
| W2 | keeper dies after claim, before the `spawning` record | claimed, no spawn record | **proof** the CLI never started (the spawn record is committed before `Popen`): the claim is released and the attempt re-kicked | `test_keeper_crash_after_claim_is_released_and_restarted` |
| W3 | keeper dies after the `spawning` record, before `Popen` | spawn record, no pid | process group empty, no output, no exit record → `unknown`; never re-run | `test_keeper_crash_before_spawn_is_unknown_and_not_rerun` |
| W4 | keeper dies right after `Popen`, before the pid record | spawn record, no pid | the keeper's process group is alive → keep observing; when it empties, close from the CLI's native result with `exit.known=false`, else `unknown` | `test_keeper_crash_after_spawn_before_pid_record` |
| W5 | keeper killed while the CLI runs | pid + start time of keeper and CLI | CLI identity verified alive → observe; afterwards close from the native result (`exit.known=false`) or `unknown` | `test_keeper_killed_while_cli_keeps_running` |
| W6 | CLI killed | keeper alive | the keeper records the signal → `failed`; no re-run | `test_cli_killed_is_recorded_as_failed_not_rerun` |
| W7 | keeper dies after writing `exit.json`, before the terminal commit | `exit.json` (fsynced, atomic rename) | close with the real exit code from `exit.json` | `test_keeper_crash_after_exit_before_commit` |
| W8 | keeper dies after the terminal commit | terminal state + event | nothing | `test_keeper_crash_after_commit_is_harmless` |
| W9 | consumer loses the ACK reply / ACKs twice | ack row | ACK is idempotent per `(event, consumer)` and rejects mismatched task/attempt/event | `test_lost_or_repeated_ack_is_idempotent`, `test_mismatched_ack_is_rejected` |

Process identity is always pid + OS-reported start time:
- Linux: `/proc/<pid>/stat` field 22 (clock ticks, typically 10 ms) plus the boot id;
- macOS: the kernel's `kinfo_proc.p_starttime` (microseconds), validated against the pid stored in the struct, with `ps -o lstart` only as a fallback.

`ps -o lstart` has one-second resolution, which was too coarse: the suite caught a pid
pointed at a process started within the same second.

A recycled pid is treated as "gone" (`test_recycled_pid_is_not_mistaken_for_the_keeper`). An
unverifiable answer is treated as "may be alive" and blocks closing and resuming. A lock, a pid
or a stale timestamp alone is never accepted as proof that something did not run.

Concurrency rules:
- **Concurrent submits** of one key converge on one task and one execution (`BEGIN IMMEDIATE` plus a unique `(scope, key)` index).
- **Concurrent resumes** create exactly one new attempt, via a CAS on `current_attempt`.
- **A stale keeper** can only write to its own attempt row, and only from the expected state. A superseded attempt cannot change the task's state.
- **Cancel vs. completion** yields exactly one terminal event. If completion wins, `cancel_lost_race` is recorded.

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
- no fabricated success (an outcome needs the CLI's own result);
- no loss of a committed terminal state or event (no delete paths: `test_ledger_code_never_deletes`);
- no automatic re-run;
- at most one CLI per attempt;
- ACKs never move to another task or attempt.

**Liveness.** A submitted task reaches a terminal state only if all of the following hold:
- the machine stays up and its disk is writable;
- the CLI can make progress (login valid, quota available, network);
- the consumer eventually reads again (or something wakes it).

Storage faults are explicit:
- low disk space refuses new work before acceptance and again before spawn;
- a corrupt database stops new executions without being replaced;
- a missing database next to an existing marker is never silently recreated;
- a read-only ledger fails the submit without reporting `accepted`.

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

## Residual risks

- If the CLI moved itself to a new process group *and* the keeper died in the microseconds
  between `Popen` and recording the pid (W4), the orphan would be invisible:
  - the attempt would become `unknown` while the CLI still runs;
  - a later explicit resume could then run concurrently with it.

  Neither official CLI is known to change its own process group.
- Cancellation sends SIGTERM to the keeper's process group. Tool subprocesses that the CLI
  started in their own process groups are left to the CLI's own shutdown handling.
- A same-UID local user can edit the ledger. The ledger is not a security boundary.
- Timestamps use the wall clock. Clock jumps affect only the 5 s re-kick grace period and reporting.

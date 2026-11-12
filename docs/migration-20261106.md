# Results, delivery deadlines and production admission

This describes source behavior after this change. It does not confirm deployment, receiver
routine contents, visible chat delivery, provider model availability or either Mac's settings.
The relay schema/version stays compatible; no historical ledger rewrite or new migration is needed.

## New production requests

Every new Codex/Claude submit needs `--task-class` and `--resource-class`, plus explicit model
and effort. Codex also needs an explicit sandbox. The relay validates; it does not classify prose.

| task class | engine | model | effort |
|---|---|---|---|
| simple | codex | gpt-6.1-terra | high |
| complex | codex | gpt-6.1-sol | max |
| highest_quality | claude | claude-opus-5-5 | max |
| highest_quality | claude | claude-fable-5-2 | xhigh |

The `highest_quality` class accepts either listed combination; Opus 5.5/max remains valid.
Simple and complex Codex tiers are unchanged.

Resources are `standard` or `computer_use`. Computer use requires the complex Codex tier and
has no extra sandbox grant. Choose the minimum task permissions; file work normally uses
`workspace-write`. No CLI defaults, auth, global environment or host configuration are changed.
Unknown/deprecated models (including Astra), missing settings and effort mismatches are usage errors.
The operator must verify installed provider support; the allowlist implements routing §1.7.

`effective_settings` records the explicit engine/model/effort/task/resource values passed to the
adapter, with `verification=invocation_only`. It is not proof of the provider's served model or
internal effort. Legacy incomplete settings are reported with `production_settings_unverified`
on receipts/new terminal observations. Existing stored records are not backfilled.

`engine=fixture` is the only tier exemption: it cannot call a provider. It records a reason and
`verification=no_provider`. An optional `--test-only-reason` is accepted only for this engine.
Fake provider CLI tests use real tier arguments; there is no production exemption flag.

Known keys preserve their original identity and hashes, including legacy payloads. A matching
legacy duplicate is accepted; a changed payload still conflicts. New keys use the new policy.
Resume retains the stored request and permissions; old requests are not silently upgraded.

## Shared local admission

Use one canonical relay home for all bots/engines on a Mac. The ledger's write transaction
uses a [configurable, memory-aware total budget](usage.md#local-concurrency-and-available-memory):
by default four jobs with at least 1536 MiB available, one below that threshold, and two if
memory cannot be read. Preflight/claimed work counts, and the separate limit of at most one
declared computer-use job is unchanged. It covers all scopes and queues in that home. Separate relay homes are separate
budgets and must not be used to split a machine's production workload.

Pending and dependency-waiting jobs do not use running slots. Among eligible jobs, older attempts
reserve the next compatible slot; a CU wait can leave a free standard slot usable. Scope FIFO
queues and dependency gates still apply. No running job is preempted or killed for capacity.
Claims and spawning use compare-and-set transitions. A restart does not erase reservations;
unknown process ownership keeps its slot until existing recovery proves termination. A keeper
lost while waiting ends blocked without starting an engine, as before. A claim lost before a
spawning record can be safely released by existing reconciliation. No time lease expires a job.

Historical resource classes are unknown; unclassified provider work conservatively reserves a CU
budget without claiming it is CU. No class is inferred from prompt text. Quiesce old writers
and inventory old active/CU work before rollout. Old installed keepers do not implement the new
gate. A single-home ledger is local coordination, not a cross-machine subscription broker.
Friday must still enforce the combined per-provider cap of three across the two Macs, select a
machine and serialize same-repository writers. There is no central automatic machine selection.

## Result contract

Process `outcome`/exit codes, `business_status` and `artifact_verification` are independent.
Canonical producer JSON uses `status=ok|partial|failed` and
`stop_reason=null|blocked|needs_user|stopped_for_approval`. End final stdout with the same
`status=<value>` on its own line. Submit the exact cwd-relative `result_file`, and have the
producer write there. `required_artifacts` can also be submitted as an option.

| Legacy status | Business status | Separate stop reason |
|---|---|---|
| done / ok | ok | null |
| partial | partial | null |
| failed / fail | failed | null |
| blocked | partial | blocked |
| needs_user / needs_input / login_required | partial | needs_user |
| stopped_for_approval / needs_approval | partial | stopped_for_approval |
| unknown, absent, malformed | unreported | no success inferred |

Raw status/stop tokens are retained with a 128-character limit. Prose is never parsed for success
or stops. Legacy uppercase `*_DONE status=... [ok=N fail=N skip=N]` final markers are accepted.
The report sources use severity `failed > partial > ok`; ties prefer structured JSON. Conflicts
warn. An invalid/missing declared file or unknown report blocks promotion to `ok`, even if another
source says ok. A stop can never become ok. A process blocked/needs_input maps to partial with a
separate stop; a failed/cancelled/unknown process defaults to business failed unless a stop report
preserves partial. An explicit failed business report remains failed even with a stop reason.

Structured Claude denial entries downgrade ok/unreported to partial with source `denials`.
They do not prove every artifact is invalid. Codex denial counts remain unavailable. Dependency
success gates never accept a stop, including `ok_or_partial`; `any_terminal` remains explicit.

JSON reads are limited to 1 MiB, regular UTF-8 files, no traversal or symlink escape. Duplicate
JSON keys are invalid. Missing/unsafe/malformed/unknown files produce safe diagnostics. A root
file with the same basename is only a path-mismatch clue; it is never read as an alternate result.

`artifacts` and `required_artifacts` are lists of at most 128 task-local paths. Absolute paths
inside the cwd are accepted. Submitted required paths are checked even without a valid report.
Missing files or invalid paths downgrade an ok report to partial. `artifact_verification` records
existence only, not correctness, quality or reproducibility of file contents. No declarations
means `not_declared`, not verified output. Producer `artifact_checks` is not trusted as proof.

Each terminal attempt stores `result_evidence`: exact bounded-file SHA-256, byte count, declared
path/existence, allowlisted status/stop snapshot and artifact checks. Summaries, secrets, arbitrary
JSON fields, file contents and denial command arguments are not copied. Evidence lives in the
attempt and its immutable event; old events are not reread from a reused result path. Keeper
exit.json also freezes business evidence before the terminal commit, covering crash recovery.
If both the keeper and exit snapshot were lost, first recovered observation is all that is known.
Old receipts without snapshots stay historical/unverified; no retrospective file check is implied.
A producer must replace its result for every attempt; this does not certify result freshness.

## Transport and operator boundary

HTTP adapters run in a disposable, owned process, bounded by the smaller remaining wall-clock
and monotonic budget (`timeout_s`, including worker startup/DNS/TCP/TLS/headers/body). The parent
checks every 50 ms and allows bounded worker cleanup (two 200 ms joins). It stops that transport
worker only. A pre-request shared flag prevents a lagging phase message from turning a possibly
sent request into a safe retry. Response loss stays ambiguous; event identities and wake/retry
budgets are unchanged. A timeout before send is retryable only after stopping the worker. If worker termination cannot
be verified, the result remains ambiguous with `transport_worker_stop_unverified`.
No process can enforce elapsed time while the whole OS is suspended or unscheduled; the deadline
is checked when execution resumes. Worker startup itself is an OS operation, not a real-time SLA.

Send records include phase, configured timeout, wall/monotonic elapsed seconds and clock divergence.
DNS, TCP, TLS, request, response headers/body and worker loss are distinguished where observed.
Clock divergence can indicate suspend or clock adjustment; `suspend_verified=false` makes no cause
claim. Exception text, URL, tokens and raw responses are not logged. Command adapters retain their
subprocess timeout and gain safe elapsed timing; they are not covered by HTTP worker isolation.

Fallback considers unacked `terminal` events only, once, including after send failure. Waiting,
unknown informational events and skipped sends do not qualify. Fallback never ACKs for the owner.
HTTP accepted, visible delivery and consumer ACK remain separate facts. Fallback scheduling still
needs a due notifier pass; this change does not install a service or routine.

Friday must inspect the existing server-side routine and its execution traces. In one authorized
wake on the fixed source/ledger/machine: read `brief`, send its lines to the original bot chat,
obtain the actual message id, then run its batch ACK. On delivery failure, leave events unacked.
Fallback alerts the owner once and does not proxy the original ACK. Check receive/start/finish,
visible-message id, ACK time, transport timing, and host suspend/resume evidence separately.
A conflicting archived routine prompt is a hypothesis, not proof of the historical delay's cause.

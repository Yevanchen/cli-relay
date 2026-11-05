# Notifier (v0.2)

The notifier wakes the consumer of a task when the task ends, for example the webhook routine of
a Grok Bot. It is a small local program that reads the ledger:
- It never calls a model.
- It never runs, resumes or re-runs a task.
- A notification can only cause the consumer to look again.

What it adds to v0.1:
- results were kept until a consumer ACKed them;
- now a terminal event also triggers a wake-up of that consumer, with bounded retries;
- after a restart, unsent wake-ups are sent and unacknowledged ones get bounded reminders.

What it does not promise:
- Exactly-once delivery.
- That any message reaches a chat, a phone or a person.
- That anything keeps running when the machine sleeps or loses power. A notification that is
  due while the machine is off is sent by the next pass after it comes back.

## Model

```
task ends ─▶ terminal/unknown event (same transaction as the state)          ← the pending fact
              │
              ▼
notifier pass ─▶ notifications row per (event, route) ─▶ adapter ─▶ webhook / notify.sh
              │           states: pending → claimed → sending → accepted | handed_off | unconfirmed
              │                                               → acked (consumer ACK seen)
              │                                               → failed | unacknowledged (bounds reached)
              ▼
consumer (Bot) reads the task by id, reports in its chat, then `cli-relay ack` ─▶ acks row
```

- **Pending fact.** It is the event itself, committed with the task's terminal (or `unknown`)
  state. There is no second record to forget: a crash anywhere leaves the event, and the next
  pass finds it.
- **Two acknowledgements, kept apart.**

  | Fact | Where | Means |
  |---|---|---|
  | `transport_accepted` | `notifications.accepted_at` (HTTP adapter, 2xx) | the receiver accepted the request. For a Grok routine this means a run was started, **not** that a chat message exists |
  | `handed_off` | `notifications.state` (command adapter) | the notify program exited 0. `notify.sh` always exits 0, so this says nothing about the HTTP result |
  | consumer ACK | `acks` (`cli-relay ack --consumer … --message-id M [--chat C]`) | the consumer says it delivered the result; message id/chat are the ledger's delivery record. Older ACKs without a message id only say processed |

  Pull reads (`events`) are counted in `deliveries` and are never counted as sends. `brief`
  reads unacknowledged terminal events without incrementing that count or changing state.
- **Bounds.** Every number is configurable.
  - At most `retry.max_attempts` attempts per event (default 6). An attempt either calls the
    adapter or records a failed readiness check for an unproven route, without sending.
  - Backoff doubles from `retry.base_delay_s` (default 30 s) up to `retry.max_delay_s` (default
    30 min). `Retry-After` is a lower bound applied after that cap, so it may require a longer wait.
  - At most `reminder.max_wakes` calls that may have reached the consumer (default 2). HTTP
    error responses and a command that starts but exits non-zero also count: failure is not
    proof that the receiver did no work. Connection failures before sending do not count as a
    possible wake, but still consume an attempt. After an accepted, handed-off or unconfirmed
    call, a reminder follows only within the remaining budget and after
    `reminder.ack_timeout_s` (default 30 min). These are transport bounds, not measured Bot run counts.
  - Then the row stays `failed` or `unacknowledged`. Nothing is deleted, and the event is still
    listed by `events`. Only `notify retry --event E` gives it a new budget.
- **Crashes.** A pass that dies after claiming but before marking `sending` sent nothing, and the
  next pass sends it. A pass that dies after marking `sending`, a timeout and a lost response may
  all have woken the consumer. Such a row becomes `unconfirmed` and is only resent as a bounded
  reminder.
- **Duplicates.** One row per (event, route) is enforced by the primary key. Concurrent passes
  claim rows with compare-and-set. The Grok routine has no idempotency key, so a lost response or
  a reminder can wake the Bot twice for one event. That is bounded by `max_wakes`. A duplicate
  wake-up never re-runs a task, and the second `ack` is reported as `duplicate`.
- **Batching.** Due notifications of one route go out together as one wake-up.
- **Default wake-ups.** `succeeded`, `failed`, `blocked`, `needs_input`, `unknown`. Logs, tokens
  and heartbeats never produce events, so they never wake anyone; `cancelled` is opt-in.
  The informational `waiting` event is available through `events` and never causes a wake-up.

### Fallback reminders

Use `route set-fallback bot friday` (or `route add … --fallback-route friday`) to configure one
fallback route. It must already exist, be proven and differ from the original route. Use
`route set-fallback bot --clear` to remove it. A referenced fallback cannot be removed until
its references are cleared. Neither command changes `default_route` or Friday's proof/identity.

On the next notifier pass after an original notification has no owner ACK for
`reminder.ack_timeout_s` (default 1800 seconds), one fallback reminder is attempted. It also
triggers immediately in a pass that records the original notification as `failed`. Timeout age
starts at the first send, so an original-route reminder does not delay the fallback. An original
owner ACK before the fallback send suppresses it. Routes without a fallback behave as before.

The payload has `status: "fallback_reminder"`, `cli_relay.fallback: true`, `original_route`,
and, per event, `original_consumer`, `original_chat`, `notification_age_s` and `fallback_reason`
(`ack_timeout` or `send_failed`), in addition to the full event/task ids. Its summary explicitly
says `FALLBACK REMINDER` and identifies those original owners, ids and ages, including through
the command adapter. Friday should remind Evan, leaving delivery and ACK to the original bot.
Fallback never changes task ownership or writes an ACK for anyone.

`notification_fallbacks`, keyed by `(event_id, original_route)`, records the destination, claim,
state, send time and result. `notify status` exposes it under each notification's `fallback`.
Concurrent passes claim it once. A crash before `sending` can recover the claim; a crash after
`sending` becomes final `unconfirmed` and is never resent. There is **at most one fallback
adapter call**, with no retry even on connection failure, ambiguous response or `notify retry`
of the original notification. This bounds reminders rather than promising delivery. A target
that loses proof is not sent to; verification must be restored before a fallback can be claimed.
No fallback invokes another fallback.

## Configuration: `<ledger home>/notifier.json`

Private to each machine. Never commit it. Keep it readable only by its owner (`chmod 600`).

```json
{
  "source_id": "host-a",
  "routes": {
    "bot": {
      "consumer": "bot",
      "chat": "Bot chat",
      "adapter": {"type": "command", "argv": ["/ABSOLUTE/PATH/TO/notify.sh"], "job": "CLI_RELAY"}
    }
  },
  "notify_outcomes": ["succeeded", "failed", "blocked", "needs_input", "unknown"],
  "retry": {"max_attempts": 6, "base_delay_s": 30, "max_delay_s": 1800},
  "reminder": {"ack_timeout_s": 1800, "max_wakes": 2}
}
```

- `source_id` names the machine in every payload, e.g. `host-a` and `host-b`. It must differ between
  machines.
- `routes`: name → the consumer name used for its ACKs, a chat label, and the adapter.
  - A task chooses its route with `submit --notify-route NAME`.
  - Without one it uses `default_route`.
  - With neither it is not notified.
  - The effective route, consumer and chat are stored once; changing the default cannot move
    an existing task. If no configuration existed at submit, the first configured pass binds
    it once. Historical explicit route reservations can wait for their configuration; new
    submissions with `--notify-route` require a registered, proven route.
  - Credentials may rotate without changing the owner. If a route name is reused with a new
    consumer or chat, old tasks fail with `route_owner_changed`; their owner is not rewritten.
    Restore the original route identity before an explicit `notify retry`.
- `adapter.type = "command"` runs `argv + [job, stage, status, summary, artifact]`. That is the
  interface of `notify.sh <job> <stage> <status> <summary> [artifact]`.
  - `exit_status` defaults to `"unverified"`: exit 0 is recorded as `handed_off`, and a non-zero
    exit is ambiguous and uses the bounded reminder budget.
  - Set `"verified"` only for a program whose exit status really reflects delivery.
  - The program gets the notifier's environment. It reads its own credentials; cli-relay never
    sees them.
- `adapter.type = "http"` POSTs JSON directly, with `Authorization: Bearer <token>`. URL and
  token come from exactly one of `url_env` / `url_file` and one of `token_env` / `token_file`
  (absolute paths). They are read at send time and never printed or logged.
- An invalid file is refused as a whole (exit 2, key names only) and nothing is sent.

The example above shows the legacy layout: `migrate` records existing routes with **no
`verification` key** and no managed-route record in the ledger's `legacy_routes` table. Only those
names with matching source, consumer, chat and adapter fingerprints remain usable without a receipt
for explicit/default submissions, task delivery and bounded reminders. This preserves the deployed
installer's `friday` route without changing notifier.json's format, its file bytes or the installer
template. Loading, listing and diagnosing the configuration do not rewrite it. Leave the live
route configuration unchanged on upgrade; do not add a receipt or re-register it as an upgrade step.
`doctor` reports `proven: true, proof: legacy_config`, which denotes compatibility rather than a
new visible-message receipt. An explicit `verification: null` or any unfinished verification is
**not** legacy and cannot bypass proof requirements. Adding a new name, renaming a route, or changing
its recorded identity requires verification; removing `verification` cannot grant an exemption.

Use `route add` for new routes. It writes `verification: null`, so they remain **unproven** until
`route verify` (plus `route confirm` for an async receiver) succeeds. New routes need Evan's approval and a separate acceptance step before
task use or default selection. A command route using this verification flow must implement the
response below; ordinary `notify.sh` exit 0 cannot verify it. Legacy command adapters keep working
with their original five-field task interface. Transport acceptance remains separate from the
task consumer's ACK.

### Registration and visible-message verification

`route add NAME --consumer C --chat LABEL --url-env URL_VARIABLE --token-env TOKEN_VARIABLE`
registers an HTTP route in `<home>/notifier.json`. The first registration additionally requires
`--source-id SOURCE`. Each reference can instead be `--url-file /absolute/private/file` or
`--token-file /absolute/private/file`. File references must already be owner-only regular files
owned by the current user. Registration checks the reference, not its contents, and makes no
network call. `--timeout` sets the adapter timeout (1–600 seconds, default 20).

- Identical managed registrations do not rewrite the configuration or reset verification/retry
  state. Re-registering a legacy route, even with identical settings, writes `verification: null`
  and drops its exemption; it then needs verification. Changes to an existing registration are
  refused; remove and re-add deliberately, which also requires verification. Changes to other
  routes preserve the untouched legacy route's missing verification key and exemption. Removing the
  last route leaves an empty route map, and removing a default clears that default. Task ownership
  is never rewritten. In-flight notifier passes may still finish using their previous snapshot.
- All managed writes are atomic (file and directory fsync) with mode `0600`. A home-local
  `.notifier.lock` serializes registration, removal, fallback/default changes and verification; a symlink
  `notifier.json` is refused for these mutations. Verification holds this lock through the send.
- Only non-secret settings, credential reference names/paths and the latest verification receipt
  are stored. URL/token values and raw receiver responses never enter the configuration, receipt,
  log or CLI output. `route list` shows references and proof state without resolving secrets.
  `doctor` shows each name, `proven`, `proof` (`legacy_config`, `receipt` or `unproven`), and
  `secret_resolved: {"url": true, "token": false}` for HTTP routes, with booleans only for secret
  resolution. Command adapters own their credentials, so that map is empty.
  Unproven routes are informational unless configured as a default; an unusable default or an
  unresolved HTTP credential reference makes doctor exit 6.
- `route set-default NAME` and new `submit --notify-route NAME` require a proven route; new
  submissions also refuse a manually configured unproven default before committing any task.
  No `--allow-unproven` escape hatch is provided. `--origin` never bypasses the routing gate.

`route verify NAME` is an **explicit visible send**. Approve the receiver/chat first. It performs
at most one due attempt, prints a JSON receipt and exits 0 only for proven, otherwise 6. It does
not create a task, run a CLI, poll a Bot or alter task ACKs. The five fields use `status=route_verify`
and a visible `summary` saying `cli-relay 接通测试，无需处理` and naming a fresh verification id.
The additional request object is:

```json
{"schema":"cli-relay.route-verify.v1","verification_id":"v-<32 hex digits>",
 "source_id":"mac-a","ledger_id":"l-...","route":"github",
 "consumer":"github-bot","chat":"github-original-chat","test_message":"cli-relay 接通测试，无需处理"}
```

Two verification paths are supported:

1. **Synchronous:** the receiver sends the test message to the specified chat, obtains its
   platform message id, then returns HTTP 2xx with this JSON body, copying the six identity
   fields exactly. This existing protocol proves the route immediately:

   ```json
   {"verification_id":"v-<32 hex digits>","source_id":"mac-a","ledger_id":"l-...",
    "route":"github","consumer":"github-bot","chat":"github-original-chat",
    "visible":true,"message_id":"platform-message-id"}
   ```

2. **Asynchronous (Grok routine):** a 2xx run-start acknowledgement without a message receipt
   records `state: "awaiting_confirmation"`, still `proven: false`. `route verify` prints
   `verification_id`, `next_at` and `expires_at` and exits 6. Have the bot post exactly
   `cli-relay 接通测试，无需处理` as a visible test message in the specified chat, obtain its message
   id, then run on the same Mac:

   ```sh
   cli-relay --home /path/to/ledger route confirm github --verification-id v-… --message-id platform-message-id
   ```

   Confirmation validates the current probe, ledger and the unchanged route/source/consumer/chat
   and credential references. Optional `--source-id S --ledger-id L --consumer C --chat LABEL`
   add explicit identity assertions. Wrong, stale, expired or mismatched ids are refused;
   repeating the same successful confirmation is harmless. Confirmation does not send anything
   or ACK any task event. A run id is never used as a visible message id.

Message ids must be strings of 1–256 characters from `[A-Za-z0-9._:@/+-]`. Responses are read up
to 65536 bytes; other fields are ignored. The HTTP adapter rejects a credential echoed as the
message id. Command adapters receive the same full JSON request on stdin **only during verify**
and must return the same receipt on stdout with exit 0; they must keep credentials out of their
output. Their `exit_status=verified` setting alone is not sufficient. A zero exit without a
receipt can await explicit confirmation too. An explicit but invalid synchronous receipt
(wrong identity, invalid message id or `visible` flag) stays `unconfirmed`, and cannot be
confirmed as an async acceptance. Raw responses and run ids are not stored.

After validating the receipt, the relay records `state=acked`, `message_id` and `acked_at`, then
the route becomes proven. This is the probe's ACK, not an ACK of any task event. The receipt is
in `routes.NAME.verification` in notifier.json, the ledger's `route_verifications` table, and
the command output. For managed routes, `proven` requires the matching ledger record, its ledger
id, and a SHA-256 of the source, route name, owner and adapter references/settings. Registration
also persists the name in `managed_routes`, so removing its verification key cannot acquire the
legacy exemption. That ledger row also owns the current verification id: retry replaces it,
and removal/re-registration clears it. Restoring an older config receipt cannot revive a stale
proof or reset the current budget. No CLI operation can grant proof by simply editing notifier.json.
Editing any of those invalidates the proof. Rotating secret contents behind unchanged references
does not invalidate it. A receipt copied to another ledger is unproven. Use distinct `source_id`s
per machine. These files are operator-owned state, not a security boundary against that user.

Repeated verify calls keep the same verification id and budget, and never send before `next_at`.
The receiver must deduplicate by `(source_id, ledger_id, verification_id)` and return the same
message id after a lost response. Connection failures back off by 30, 60, 120, 240, 480 seconds
with the default six-attempt budget (the configurable cap is 1800 seconds). A request that may
have arrived, including an async 2xx, consumes a possible wake. Default ACK timeout
is 1800 seconds and the possible-wake budget is 2. The same `retry`/`reminder` settings govern
probes and task notifications, but their counters are separate. A crash after the persisted
`sending` state consumes one attempt and one possible wake; no immediate resend is allowed.
After `failed`/`unacknowledged`, `route verify NAME --retry` explicitly starts a new id and budget.
An unfinished probe expires 24 hours after creation. Expiry refuses confirmation and allows an
explicit `verify --retry`; an id from an older retry cannot confirm its replacement. An accepted
async probe can still be confirmed after its send budget is exhausted, before that expiry.
`notify run` does not drive verification probes: invoke verify again when the receipt is due.
Already-proven verification, including a legacy route, is a no-send, idempotent read; it does
not write a receipt into a legacy configuration.

For task events whose route loses proof, readiness checks consume the same exponential-backoff
attempt budget without invoking the adapter or recording a transport send. Events remain in
the ledger, including after the budget ends as `failed/route_unproven`. If verification finishes
before that point, the next due check can send. After exhaustion, use `notify retry --event E`.
An ACK timeout still ends as `unacknowledged` after bounded reminders; no route operation or
notification retry creates a new task attempt.

### Payload

The five `notify.sh` fields, so an existing routine keeps working:

| Field | Value |
|---|---|
| `job` | the adapter's `job` (default `CLI_RELAY`) |
| `stage` | `<source_id>/<route>` |
| `status` | the outcome (`succeeded`, `failed`, `blocked`, `needs_input`, `unknown`), or `multiple` for a batch |
| `summary` | one line: a header and result excerpt bounded to 400 characters, followed by business status and denial count when available, then complete permission opt-ins (or `default minimal`) for every task attempt. Business fields and permission lists are never truncated. Control characters, `"` and `\` in the excerpt are replaced |
| `artifact` | `cli-relay:<source_id>:<ledger_id>` |

The HTTP adapter also sends `cli_relay`, a structured copy:

```json
{"schema": "cli-relay.notify.v1", "source_id": "host-a", "ledger_id": "l-…", "route": "bot",
 "chat": "Bot chat", "consumer": "bot", "reminder": false, "count": 1,
 "events": [{"event_id": "e-…", "task_id": "t-…", "attempt_no": 1, "kind": "terminal",
             "outcome": "succeeded", "key": "…", "result_excerpt": "…",
             "business_status": "partial", "business_status_source": "status_line",
             "business_status_counts": {"ok": 9, "fail": 4, "skip": 4},
             "denials": {"count": null, "tools": [], "available": false},
             "sandbox": "danger-full-access", "full_access": true, "network_access": true,
             "completion_marker_required": false}]}
```

Every new terminal/unknown event entry includes `business_status`, `business_status_source` and
`denials`, copied from the event without changing `outcome` or the five-field `status`. Optional
`business_status_counts` contains integer counters from the final status line. Values and
precedence follow the [business-status rules](usage.md#business-status): reports can be `ok`,
`partial`, `stopped_for_approval`, `failed`, or `unreported`. Sources are `status_line`,
`result_file`, `outcome`, `denials`, or `none`. A succeeded Claude run with structured permission
denials becomes business `partial` when its report is `ok` or absent; its process outcome remains
`succeeded`. Denial counts count entries, including repeated tool names, with a unique tool list.
Codex has `available: false` and count `null`; Claude also has no denial data without a final result.

The summary appends `<task_id>#<attempt_no> business_status=partial denials=2` for each new event,
including every event in a batch. An unavailable denial count is rendered as `denials=null`.
These fields survive header/excerpt truncation, so a command-adapter consumer need not inspect
`result_excerpt` to discover a partial result. Historical events can omit the new fields and
remain deliverable; no past event is reinterpreted or backfilled. Warnings such as
`business_status_unknown`, `business_status_conflict`, `business_status_result_file_invalid` and
`business_status_missing` follow the existing structured-warning and summary-code convention.

Tasks opting into `result_file` also add `result_file: {"path": "/absolute/task/cwd/out/result.json",
"exists": true}` to terminal/unknown bodies and `cli_relay.events[]`. The absolute path is
resolved against the task cwd at closing, even for a missing file (`exists: false`). The summary
includes it when there is room; `artifact` keeps its existing `cli-relay:<source>:<ledger>` form.
No file contents are exported. Without the option, the new field is omitted and request hashes
are unchanged. `brief` displays the path and refreshes existence with a filesystem metadata read.

Dependency-blocked terminal events and HTTP event entries also carry `reason_code`, `details`
and a one-line `summary`, for example `dependency t-… business_status=partial outcome=succeeded
does not satisfy require=ok`. This explanation is appended for every blocked task in a batch,
outside header/excerpt truncation. See [dependency and queue rules](usage.md#dependencies-and-queues).

New Codex event bodies and each corresponding `cli_relay.events` entry include `sandbox` and
`full_access`. Full access is **off by default** and requires both
`--option 'sandbox="danger-full-access"' --option allow_full_access=true` at submit time.
The typed boolean alone is harmless and does not change the sandbox. Full access disables the
Codex sandbox: commands can access files outside the task directory and use the network,
subject to host permissions. Config-layer permission and subscription/billing checks still apply.

`full_access` describes the task's requested permissions, including when a run was blocked
before it started; it does not certify that commands ran. The default is `sandbox: "read-only"`
and `full_access: false`. Historical events and other engines may omit these fields; use
`status` to inspect the stored task options. A summary contains `full_access=true` if any
event in the batch opted in, so the choice is also visible through the command adapter's
five fields. For a batch, inspect the individual events or task records to identify those tasks.
This metadata does not change delivery, retries, routing, or ACK semantics.

New Claude event bodies and each corresponding `cli_relay.events` entry include
`allowed_tools`: the extra tools the task opted in to with
`--option 'allowed_tools=["WebSearch","WebFetch"]'`, `[]` by default. Like `full_access`, it
describes requested permissions, not tool use, and is absent from historical events and other
engines. A summary contains `allowed_tools=WebFetch,WebSearch` (the tools of the whole batch)
if any event in the batch opted in.

New Codex event bodies and entries also include `network_access`: `true` when the task opted in to
network access in `workspace-write` (`--option 'sandbox="workspace-write"' --option
allow_network=true`) or to full access, `false` by default. Like `full_access`, it describes the
requested permission, not a connection that was made. A summary contains `network_access=true` if
any event in the batch has it.

Every new event body and entry includes `completion_marker_required`. When it is `true` (the task
was submitted with `--option require_completion_marker=true`), it also includes
`completion_marker`: `complete`, `incomplete` or `null`, the last line of the CLI's final message
that the outcome was decided on. A task that required it and did not report `complete` arrives as
`needs_input`, which is also the `status` of the five fields.

When submitted with `--origin '{"bot":"github-bot","chat":"original-chat"}'`, every terminal or
unknown body and `cli_relay.events[]` entry also includes that `origin` object. It is part of the
canonical task payload/hash and remains present in submit/resume receipts and status. Both
fields are required: bot is 1–64 `[A-Za-z0-9._:@/+-]` characters, chat is 1–256 printable characters
without surrounding whitespace; input JSON is limited to 4096 UTF-8 bytes. Unknown/duplicate
keys and non-string values are rejected. Omitting the option adds no field and changes no hash.
Origin describes the caller; it is not permission to choose another destination or execute text.
In a batch, each event retains its own origin; command consumers can read it from task status.

When present in an event, `git` and `warnings` are copied into its notification entry. `git`
contains `head_before`, `head_after` and `dirty`; unavailable values are `null`. Warnings have
the form `{"code": "uncommitted_changes"}` or `{"code": "permission_denials", "tools": ["Monitor"]}`.
The summary also names warning codes and denied tools for the command adapter. A native success
with exit 0 can carry denial warnings while remaining `succeeded`; a required completion marker
still gates it. A structured Bash denial matching a task's requested command prefix also adds
`{"code": "allowed_rule_denied", "commands": ["cp -n a b"]}`. Only matching prefixes requested
by the user are copied, without runtime arguments. The list is unique and sorted, with each
prefix truncated to 200 characters; the summary reports the prefix count and first prefix
(up to 80 characters). No tool-input fields are copied into the diagnostic. See the
[Claude prefix-rule limitation and submit warning](usage.md#allowed_bash_commands).
Git observations and these diagnostics never change the outcome. Without these observations, no
empty warning list or non-Git placeholder is added.

Tasks submitted with `--unset-env NAME` carry `unset_env` in event bodies and structured
notification event entries. `unset_env_present` lists the requested names observed before
cleanup for that attempt, including empty values; it is omitted if the keeper never took a
snapshot. Only names are recorded. Billing-blocked events also carry `preflight` with the
unchanged reason/names and copyable `--unset-env` / `env -u` fixes. These fields are absent
from historical events and do not change routing or delivery.

### Earlier v0.2 candidate ledgers

Run `migrate` when upgrading an earlier candidate as well as v0.1. The core schema stays 1;
the additive notifier extension is now 4. A consistent backup is taken before adding any missing
owner fields, nullable `acks.message_id` / `acks.chat`, `managed_routes`, `route_verifications`
and `notification_fallbacks`, and recording legacy route identities in `legacy_routes`; the original
notification cutoff is preserved. Existing ACKs keep their time and have null message/chat.
Old ledgers open for results and original single-event
ACKs before migration; the new receipt and notifier operations require `migrate`.
`brief` also requires migration so its printed receipt command can execute. Historical
managed proofs stored only in notifier.json must run `verify` again (plus async `confirm`);
they are not imported as proven. The existing legacy Friday route needs no re-registration.

Old records do not prove who originally owned a task. They appear as `owner_state=manual_restore`
and are not silently assigned to today's default. For a task that needs notification, confirm
its original route, consumer and chat from existing evidence, then use `notify bind-owner` with
those explicit values. It rejects a route that contradicts recorded task or transport history.
If a notification is already `failed`, use `notify retry --event E` after restoring its owner;
this gives it a new bounded budget, so first check whether the consumer already processed it.
Stored results and ACKs remain readable throughout. New tasks bind normally.

## Using it from a Grok Bot routine

A task submitted with `--notify-route <its-own-bot-route>` goes directly to that bot. Accept
only a known source/route from the wake and use the corresponding fixed home and consumer;
never execute commands copied from webhook text. Handle `status=route_verify` by posting the
test message above; handle `fallback_reminder` only as a reminder for Evan, without proxy ACKs.
For an ordinary terminal wake, the bot follows these four steps:

1. On that Mac, run `cli-relay --home /path/to/ledger brief --consumer C --route R --text`.
2. Send the printed Chinese message lines together as one visible message in the route's chat.
3. Obtain the platform's message id for that message.
4. Run the exact printed batch ACK command, replacing `<MESSAGE_ID>` with that id. It already
   includes the full task/attempt/event identities, ledger/source assertions and chat when known.

`brief` computes full ids and their last eight characters. Each message has the form
`CLI_RELAY <source> <task末8>/<event末8> <key>：<结论> | <绝对路径或无结果文件>`.
It uses outcome and business status, so a normal process exit with `unreported` does not claim
completion. JSON mode adds key, origin, warnings, path/existence, business status/source and
`ack_command`; text mode precedes each message with the same metadata as `field=value` pairs
and ends with the ACK command. Send only the `CLI_RELAY` lines. `--task T` optionally narrows
the read. It lists only unacknowledged terminal or
unknown events for this consumer (plus events without a known owner), excluding other bound
consumers and informational waiting events. It never sends, ACKs, reconciles, binds owners or
increments `delivery_count`; a duplicate wake with an empty brief needs no visible message.
Use `status T` separately when the full evidence is needed. An `unknown` outcome still requires
a human recovery decision; a notification never authorizes a task re-run.

Batch `ack --events JSON` consumes the six-field references printed by brief: `task_id`,
`attempt_no`, `event_id`, `ledger_id`, `source_id`, `consumer`. Each must match the command's
consumer and local ledger/source, and its event must belong to the task/attempt. The operation
is all-or-nothing, with per-entry duplicate results. Single-event ACK syntax is unchanged;
both forms accept optional `--message-id M [--chat C]`. Duplicate ACKs preserve the first
receipt. `events --include-acked` shows the consumer's message/chat; `status` adds an `acks[]`
list while retaining `acked_by`. The ledger's `acks.message_id` is the delivery record.

Each wake-up is one routine run and uses the Bot's quota. The notifier only sends for real
outcomes, batches what is due, and stops after `max_wakes`. It never polls the Bot.

## Commands

| Command | Does |
|---|---|
| `cli-relay migrate [--backfill]` | add the notifier tables to an existing ledger, after a consistent SQLite backup in `<home>/backups/`; events from before the migration are not notified unless `--backfill` |
| `cli-relay notify flush` | one pass: enqueue events, recover abandoned passes, record ACKs, schedule reminders, send what is due |
| `cli-relay notify run [--interval 60]` | a pass every interval until SIGTERM (for launchd or systemd); one log line per pass in `<home>/notifier.log` |
| `cli-relay notify status [--task T] [--event E]` | per notification: state, attempts, wake-ups, `transport_accepted`, `consumer_acked`, last result |
| `cli-relay notify retry --event E` | operator action: a failed or unacknowledged notification gets a new budget |
| `cli-relay route add NAME --source-id S --consumer C --chat LABEL --url-env VAR --token-env VAR` | register an unproven HTTP route without reading or copying credentials; `--source-id` needed on first registration |
| `cli-relay route list` / `route remove NAME` | inspect references/receipt or idempotently remove a route, preserving task history |
| `cli-relay route verify NAME [--retry]` | one due visible-message probe, persisted receipt; explicit new budget after failure only |
| `cli-relay route confirm NAME --verification-id V --message-id M` | finish async verification after its visible test message; optional identity assertions |
| `cli-relay route set-default NAME` | select a proven route for future submissions |
| `cli-relay route set-fallback NAME FALLBACK` / `… NAME --clear` | set a proven alternate reminder route or remove the setting; also accepted by `route add --fallback-route` |
| `cli-relay submit … --notify-route NAME` | fixed route and owner for this task (routing metadata, not part of the payload) |
| `cli-relay notify bind-owner --task T --route R --consumer C --chat LABEL` | explicitly restore a migrated task's missing original owner; never change a bound owner or contradict a recorded route |
| `cli-relay ack … --ledger-id L [--source-id S]` | refuse unless the event belongs to this ledger or machine |
| `cli-relay brief --consumer C [--task T] [--route R] [--text]` | read-only terminal report, message lines and one complete batch ACK command |
| `cli-relay ack --consumer C --events JSON --message-id M [--chat LABEL]` | atomic batch ACK with a delivery receipt; same receipt flags also work with single-event syntax |

When a keeper commits a terminal event, and when a reconciling command closes an attempt, a
detached `notify flush` starts at once. `notify run` is still needed: it performs the retries and
reminders, and it catches up after a restart.

## Install (per machine)

1. **Back up.** Save the current notifier script, service definition and private configuration.
   Take a consistent SQLite backup, using `cli-relay migrate` or
   `sqlite3 <home>/ledger.db ".backup <file>"`; do not copy a live DB/WAL pair.
2. **Stop the old notification path first.** Disable its periodic watcher and any completion
   hooks that send the same task endings. Wait for already-started sends to finish. Do not stop
   coding tasks. Results can accumulate in the ledger during this switch.
3. **Install the reviewed version**, from a fixed commit or tag. Run `cli-relay migrate` on each
   existing ledger, including a ledger made by an earlier v0.2 candidate. A new ledger already
   has the current additive extension. Keep the existing ledger and its ACKs.
4. **Configure only after the old sender has stopped.** Write `<home>/notifier.json` for this
   machine, with its own `source_id` and destination, and make it owner-readable only. Once the
   file exists, a completing keeper can start an immediate pass even before the periodic service
   starts. Prefer `route add` for a new HTTP route; preserve existing credential references and
   do not publish the configuration. On upgrade, keep the existing installer-managed `friday`
   configuration byte-for-byte unchanged: its absent `verification` key preserves current use.
   New routes need Evan's approval and a separate acceptance step: their receiver must implement
   the synchronous or async protocol above and `route verify` (+ `confirm` when async) must pass
   with an approved visible test. Only then select
   a new route with `route set-default` or submit tasks using `--notify-route`.
5. **Start the periodic pass**, for example a launchd agent running
   `cli-relay --home <home> notify run --interval 60` with `KeepAlive`.
6. **Check one real task per machine.** Confirm the correct original chat receives its result,
   then the consumer ACK appears in `notify status`. Record source, ledger, task, event, transport
   result and chat evidence separately. A 2xx response alone does not pass this check.

## Rollback

The normal rollback changes code and notification configuration, **not the ledger**.

1. Disable new immediate sends by moving `<home>/notifier.json` to a private backup name outside
   that path. Stop the periodic `notify run` service and any scheduler that can restart it.
2. A pass that already read the configuration may still send. Wait for all in-flight passes for
   this ledger to exit before enabling another sender. Stopping `notify run` alone does not stop
   the detached passes started by keepers or reconciliation. Do not stop unrelated processes or
   running coding tasks.
3. Switch back to the previous code and notifier configuration, then enable only that sender.
   v0.1.0 can open the additive migrated ledger: core `schema_version` remains 1, and it ignores
   the new columns and tables. Keep results, events, ACKs and unsent notification rows intact.
4. Verify one sender is active and reconcile its backlog. A response lost before rollback may
   already have caused a Bot run; preserving ACKs and checking existing events avoids blindly
   sending all history again. Exactly-once notification is not guaranteed.

**Restoring an old database backup is a separate manual recovery, not normal rollback.** It
loses every task transition and ACK recorded since that backup. In particular a task that is
pending in the backup may have completed since: `status`, `events` or `wait` can start it again.
Before restoring, stop new submissions, recovery commands and senders; preserve the current DB,
WAL and attempt artifacts, and account for every post-backup task and its external effects. Do
not resume normal commands until a reconciled ledger is available. An old backup and an exit
file alone do not prevent duplicate execution.

## Limits

- The command adapter cannot know whether `notify.sh` delivered anything (it always exits 0 and
  logs HTTP results on its own). Use the HTTP adapter if acceptance matters, or rely on the
  consumer ACK.
- A Grok routine answers 200 when it starts a run. A run that fails afterwards (for example on the
  shared rate limit) is not retried by the platform. Here it shows up as a missing ACK and gets
  the bounded reminder.
- DNS resolution is not covered by the HTTP timeout.
- Notifications follow the ledger of one machine. Two machines are two ledgers, two notifiers and
  two `source_id`s. Nothing is shared or merged between them.

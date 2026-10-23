"""Notifier: wakes the consumer (e.g. a Grok Bot routine) when a task ends, from the ledger alone.

A local, deterministic program; it never calls a model and never runs a task.

* The pending fact is the event itself. It is committed with the terminal (or unknown) state,
  so a crash anywhere leaves the event, and the next pass finds it.
* ``notifications`` records the transport only: claims, sends, results. ``acks`` stays the
  consumer's own record. A transport acceptance (HTTP 2xx) and a consumer ACK are different
  facts. Neither is taken for the other, and pull ``deliveries`` are never counted as sends.
* Sends are bounded:
  - at most ``max_attempts`` adapter calls per event, with exponential backoff;
  - at most ``max_wakes`` calls that may have reached the consumer;
  - after an accepted, handed-off or unconfirmed send, at most one reminder per ``ack_timeout_s``
    without an ACK, until ``max_wakes``.
  Nothing is ever deleted: an event the consumer never ACKed stays visible as
  ``unacknowledged`` or ``failed``.
* A crash after claiming but before marking ``sending`` is safe to retry. A crash after
  ``sending`` may have woken the consumer, so the row becomes ``unconfirmed`` and is only resent
  as a bounded reminder.

Adapters:
* ``command``: runs ``argv + [job, stage, status, summary, artifact]``, the interface of
  ``notify.sh``. Its exit status says nothing about the HTTP result unless the config says it
  does (``"exit_status": "verified"``), so a zero exit is ``handed_off``, not ``accepted``.
* ``http``: POSTs the same five fields plus a structured ``cli_relay`` object, with a Bearer
  token. The URL and token come only from environment variables or files named in the config;
  they are never printed or logged.
"""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import procs
from .errors import Refused, UsageError
from .keeper import _hook
from .ledger import Ledger, canonical_json, now_iso

CONFIG_NAME = "notifier.json"
LOG_NAME = "notifier.log"
PAYLOAD_SCHEMA = "cli-relay.notify.v1"
DEFAULT_OUTCOMES = ("succeeded", "failed", "blocked", "needs_input", "unknown")
KNOWN_OUTCOMES = ("succeeded", "failed", "blocked", "needs_input", "unknown", "cancelled")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
ROUTE_RE = _ID_RE
ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9._:@/+-]{1,256}")
_NAME_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,64}$")
# states in which the row waits for something other than its own next send
WAITING = ("accepted", "handed_off", "unconfirmed")
FINAL = ("acked", "skipped", "failed", "unacknowledged")
SUMMARY_MAX = 400
EXCERPT_MAX = 120
# A live process can abandon a pass after an exception. PID liveness alone cannot own its claims.
_ACTIVE_CLAIMS: set[str] = set()
_CLAIMS_LOCK = threading.Lock()


# --------------------------------------------------------------------------------- config
def _bad(message: str, **detail: Any) -> UsageError:
    return UsageError(f"notifier config: {message}", **detail)


def load_config(home: Path) -> Dict[str, Any]:
    """Read and strictly validate ``<home>/notifier.json``. Raises UsageError with key names only."""
    path = Path(home) / CONFIG_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Refused("notifier_not_configured", "no notifier.json in the ledger home", path=str(path)) from exc
    except (OSError, ValueError) as exc:
        raise _bad("unreadable or not JSON", error=type(exc).__name__) from exc
    cfg = validate_config(raw)
    verified_routes = [route for route in cfg["routes"].values() if route["proven"] and route.get("verification")]
    if verified_routes:
        ledger = Ledger(home)
        try:
            for route in verified_routes:
                route["proven"] = route["verification"]["ledger_id"] == ledger.ledger_id
        finally:
            ledger.close()
    return cfg


def validate_config(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise _bad("top level must be an object")
    allowed = {"source_id", "default_route", "routes", "notify_outcomes", "retry", "reminder"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _bad("unknown keys", keys=unknown)
    source_id = raw.get("source_id")
    if not isinstance(source_id, str) or not _ID_RE.fullmatch(source_id):
        raise _bad("source_id must be 1-32 chars of [A-Za-z0-9._-]")
    routes = raw.get("routes")
    if not isinstance(routes, dict):
        raise _bad("routes must be an object")
    clean_routes = {name: _route(name, spec) for name, spec in routes.items()}
    default_route = raw.get("default_route")
    if default_route is not None and (not isinstance(default_route, str) or default_route not in clean_routes):
        raise _bad("default_route is not one of the routes")
    for route in clean_routes.values():
        verification = route.get("verification")
        # Installer-managed legacy routes have no verification key. Preserve their existing
        # behavior without migrating the file; an explicit null or pending receipt is not legacy.
        route["proven"] = "verification" not in route or bool(
            verification and verification["state"] == "acked"
            and verification["route_sha256"] == route_fingerprint(source_id, route))
    outcomes = raw.get("notify_outcomes", list(DEFAULT_OUTCOMES))
    if (not isinstance(outcomes, list) or not outcomes
            or any(o not in KNOWN_OUTCOMES for o in outcomes)):
        raise _bad("notify_outcomes must be a non-empty list of outcomes", allowed=list(KNOWN_OUTCOMES))
    retry = _numbers(raw.get("retry", {}), {"max_attempts": (6, 1, 50), "base_delay_s": (30.0, 0.01, 3600.0),
                                            "max_delay_s": (1800.0, 0.01, 86400.0)}, "retry")
    reminder = _numbers(raw.get("reminder", {}), {"ack_timeout_s": (1800.0, 0.01, 7 * 86400.0),
                                                  "max_wakes": (2, 1, 10)}, "reminder")
    return {"source_id": source_id, "default_route": default_route, "routes": clean_routes,
            "notify_outcomes": list(outcomes), "retry": retry, "reminder": reminder}


def _numbers(spec: Any, fields: Dict[str, Tuple[Any, Any, Any]], where: str) -> Dict[str, Any]:
    if not isinstance(spec, dict) or set(spec) - set(fields):
        raise _bad(f"{where} must be an object with keys {sorted(fields)}")
    out = {}
    for key, (default, low, high) in fields.items():
        value = spec.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise _bad(f"{where}.{key} must be a number in [{low}, {high}]")
        out[key] = int(value) if isinstance(default, int) else float(value)
    return out


def _route(name: str, spec: Any) -> Dict[str, Any]:
    if not isinstance(name, str) or not _ID_RE.fullmatch(name):
        raise _bad("route names must be 1-32 chars of [A-Za-z0-9._-]")
    if not isinstance(spec, dict) or set(spec) - {"consumer", "chat", "adapter", "verification"}:
        raise _bad(f"route {name} must have consumer, chat and adapter")
    consumer = spec.get("consumer")
    if not isinstance(consumer, str) or not _NAME_RE.fullmatch(consumer):
        raise _bad(f"route {name}: consumer must be a valid consumer name")
    chat = spec.get("chat", "")
    if not isinstance(chat, str) or len(chat) > 80 or any(not ch.isprintable() for ch in chat):
        raise _bad(f"route {name}: chat must be a short label")
    route = {"name": name, "consumer": consumer, "chat": chat, "adapter": _adapter(name, spec.get("adapter"))}
    if "verification" in spec:
        route["verification"] = _verification(spec["verification"])
    return route


def route_fingerprint(source_id: str, route: Dict[str, Any]) -> str:
    """Bind a proof to non-secret configuration, including credential *references*, not values."""
    identity = {k: route[k] for k in ("name", "consumer", "chat", "adapter")}
    return hashlib.sha256(canonical_json({"source_id": source_id, **identity}).encode("utf-8")).hexdigest()


def _verification(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    fields = {"verification_id", "route_sha256", "ledger_id", "state", "attempts", "wakes", "next_at", "created_at",
              "message_id", "acked_at", "last_result"}
    if not isinstance(value, dict) or set(value) != fields:
        raise _bad("invalid route verification receipt")
    if (not isinstance(value["verification_id"], str)
            or not re.fullmatch(r"v-[0-9a-f]{32}", value["verification_id"])
            or not isinstance(value["route_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["route_sha256"])
            or not isinstance(value["ledger_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value["ledger_id"])
            or value["state"] not in ("pending", "sending", "unconfirmed", "failed", "unacknowledged", "acked")):
        raise _bad("invalid route verification identity or state")
    for key in ("attempts", "wakes"):
        if type(value[key]) is not int or not 0 <= value[key] <= 50:
            raise _bad("invalid route verification budget")
    for key in ("next_at", "created_at", "acked_at"):
        if value[key] is not None and (not isinstance(value[key], str)
                                      or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value[key])):
            raise _bad("invalid route verification timestamp")
    if value["message_id"] is not None and (not isinstance(value["message_id"], str)
                                            or not MESSAGE_ID_RE.fullmatch(value["message_id"])):
        raise _bad("invalid route verification message id")
    if value["state"] == "acked" and (not value["message_id"] or not value["acked_at"] or not value["attempts"]):
        raise _bad("an ACKed verification needs a message id, timestamp and send")
    if value["last_result"] is not None and (not isinstance(value["last_result"], str)
                                            or not re.fullmatch(r"[a-z0-9_]{1,64}", value["last_result"])):
        raise _bad("invalid route verification result")
    return dict(value)


def _adapter(route: str, spec: Any) -> Dict[str, Any]:
    if not isinstance(spec, dict) or spec.get("type") not in ("command", "http"):
        raise _bad(f"route {route}: adapter.type must be command or http")
    timeout = spec.get("timeout_s", 60 if spec["type"] == "command" else 20)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600:
        raise _bad(f"route {route}: adapter.timeout_s must be in [1, 600]")
    if spec["type"] == "command":
        allowed = {"type", "argv", "job", "exit_status", "timeout_s"}
        argv = spec.get("argv")
        if (set(spec) - allowed or not isinstance(argv, list) or not argv
                or not all(isinstance(a, str) and a for a in argv) or not os.path.isabs(argv[0])):
            raise _bad(f"route {route}: command adapter needs argv (absolute program path first)")
        exit_status = spec.get("exit_status", "unverified")
        if exit_status not in ("unverified", "verified"):
            raise _bad(f"route {route}: exit_status must be unverified or verified")
        job = spec.get("job", "CLI_RELAY")
        if not isinstance(job, str) or not _NAME_RE.fullmatch(job):
            raise _bad(f"route {route}: job must be a short name")
        return {"type": "command", "argv": list(argv), "job": job, "exit_status": exit_status,
                "timeout_s": float(timeout)}
    allowed = {"type", "url_env", "url_file", "token_env", "token_file", "job", "timeout_s"}
    if set(spec) - allowed:
        raise _bad(f"route {route}: unknown http adapter keys")
    for kind in ("url", "token"):
        sources = [k for k in (f"{kind}_env", f"{kind}_file") if k in spec]
        if len(sources) != 1:
            raise _bad(f"route {route}: give exactly one of {kind}_env or {kind}_file")
        value = spec[sources[0]]
        if not isinstance(value, str) or not value or (sources[0].endswith("_file") and not os.path.isabs(value)):
            raise _bad(f"route {route}: {sources[0]} must be a variable name or an absolute path")
        if sources[0].endswith("_env") and not ENV_RE.fullmatch(value):
            raise _bad(f"route {route}: invalid environment variable name")
    job = spec.get("job", "CLI_RELAY")
    if not isinstance(job, str) or not _NAME_RE.fullmatch(job):
        raise _bad(f"route {route}: job must be a short name")
    return {"type": "http", **{k: spec[k] for k in spec if k.endswith(("_env", "_file"))}, "job": job,
            "timeout_s": float(timeout)}


# ------------------------------------------------------------------------------- payload
def _clean(text: Any, limit: int) -> str:
    """One line, no characters that a naive shell or JSON builder could trip over."""
    s = "" if text is None else str(text)
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s).replace('"', "'").replace("\\", "/")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _event_ref(row: Dict[str, Any]) -> Dict[str, Any]:
    body = json.loads(row["body_json"])
    return {"event_id": row["event_id"], "task_id": row["task_id"], "attempt_no": row["attempt_no"],
            "kind": row["kind"], "outcome": body.get("outcome") or row["kind"], "key": row.get("request_key"),
            **{key: body[key] for key in ("sandbox", "full_access", "network_access", "allowed_tools",
                                          "completion_marker_required", "completion_marker", "origin") if key in body},
            "result_excerpt": _clean(body.get("result_excerpt"), EXCERPT_MAX)}


def build_payload(cfg: Dict[str, Any], route: Dict[str, Any], ledger_id: str, refs: List[Dict[str, Any]],
                  reminder: bool) -> Dict[str, Any]:
    """The five notify.sh fields plus the structured record. Identifiers, requested permissions and
    a short excerpt are sent: the consumer reads everything else from the ledger by task id."""
    single = refs[0] if len(refs) == 1 else None
    status = single["outcome"] if single else "multiple"
    head = (f"cli-relay {'reminder ' if reminder else ''}source={cfg['source_id']} ledger={ledger_id} "
            f"route={route['name']} events={len(refs)}")
    if any(r.get("full_access") is True for r in refs):
        head += " full_access=true"  # also visible through the five-field command adapter
    if any(r.get("network_access") is True for r in refs):
        head += " network_access=true"
    tools = sorted({tool for r in refs for tool in r.get("allowed_tools") or []})
    if tools:
        head += " allowed_tools=" + ",".join(tools)
    parts = [f"{r['event_id']} {r['task_id']}#{r['attempt_no']} {r['outcome']}" for r in refs[:3]]
    if len(refs) > 3:
        parts.append(f"+{len(refs) - 3} more")
    summary = head + " | " + " ; ".join(parts)
    if single and single["result_excerpt"]:
        summary += " | " + single["result_excerpt"]
    record = {"schema": PAYLOAD_SCHEMA, "source_id": cfg["source_id"], "ledger_id": ledger_id,
              "route": route["name"], "chat": route["chat"], "consumer": route["consumer"],
              "reminder": reminder, "count": len(refs), "events": refs}
    return {"job": route["adapter"]["job"], "stage": f"{cfg['source_id']}/{route['name']}", "status": status,
            "summary": _clean(summary, SUMMARY_MAX), "artifact": f"cli-relay:{cfg['source_id']}:{ledger_id}",
            "cli_relay": record}


# ------------------------------------------------------------------------------ adapters
def _secret(spec: Dict[str, Any], kind: str, env: Dict[str, str]) -> Optional[str]:
    if f"{kind}_env" in spec:
        value = env.get(spec[f"{kind}_env"])
    else:
        try:
            value = Path(spec[f"{kind}_file"]).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            value = None
    value = (value or "").strip()
    return value or None


def send_command(adapter: Dict[str, Any], payload: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
    argv = adapter["argv"] + [payload["job"], payload["stage"], payload["status"], payload["summary"],
                              payload["artifact"]]
    try:
        verifying = payload.get("cli_relay", {}).get("schema") == "cli-relay.route-verify.v1"
        if verifying:
            p = subprocess.run(argv, input=canonical_json(payload).encode("utf-8"), stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env, timeout=adapter["timeout_s"])
        else:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=env, timeout=adapter["timeout_s"])
    except subprocess.TimeoutExpired:
        return {"outcome": "ambiguous", "why": "command_timeout"}  # it may already have posted
    except OSError as exc:
        return {"outcome": "retry", "why": "command_not_runnable", "error": type(exc).__name__}
    if p.returncode == 0:
        if verifying:
            # Existing command adapters can implement the same receipt protocol over stdin/stdout.
            message_id = _proof_message_id(p.stdout[:65536], payload["cli_relay"], ())
            if message_id is None:
                return {"outcome": "ambiguous", "why": "verification_receipt_invalid"}
            return {"outcome": "accepted", "why": "visible_message_confirmed", "message_id": message_id}
        if adapter["exit_status"] == "verified":
            return {"outcome": "accepted", "why": "command_exit_0_verified"}
        return {"outcome": "handed_off", "why": "command_exit_0_unverified"}
    return {"outcome": "ambiguous", "why": "command_failed", "exit_code": p.returncode}


def send_http(adapter: Dict[str, Any], payload: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
    url, token = _secret(adapter, "url", env), _secret(adapter, "token", env)
    if not url or not token:
        return {"outcome": "permanent", "why": "credentials_unavailable"}
    if not token.isascii() or any(ord(ch) <= 32 or ord(ch) == 127 for ch in token):
        return {"outcome": "permanent", "why": "token_invalid"}
    try:
        parts = urlsplit(url)
        port = parts.port
        valid = parts.scheme in ("http", "https") and parts.hostname and parts.username is None
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        return {"outcome": "permanent", "why": "url_invalid"}
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, port, timeout=adapter["timeout_s"])
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as exc:
            return {"outcome": "retry", "why": "connect_failed", "error": type(exc).__name__}  # never sent
        try:
            conn.request("POST", path, body=body, headers={"Content-Type": "application/json",
                                                           "Authorization": f"Bearer {token}"})
            resp = conn.getresponse()
            status = resp.status
            retry_after = resp.getheader("Retry-After")
            response_body = resp.read(65536)
        except (OSError, http.client.HTTPException) as exc:
            # The request may have reached the receiver (which may already have started a run).
            return {"outcome": "ambiguous", "why": "response_lost", "error": type(exc).__name__}
    finally:
        conn.close()
    if 200 <= status < 300:
        if payload.get("cli_relay", {}).get("schema") == "cli-relay.route-verify.v1":
            message_id = _proof_message_id(response_body, payload["cli_relay"], (url, token))
            if message_id is None:
                return {"outcome": "ambiguous", "why": "verification_receipt_invalid", "status": status}
            return {"outcome": "accepted", "why": "visible_message_confirmed", "status": status,
                    "message_id": message_id}
        return {"outcome": "accepted", "why": "http_2xx", "status": status}
    if status == 429 or status == 408 or status >= 500:
        out: Dict[str, Any] = {"outcome": "retry", "why": "http_retryable", "status": status,
                               "may_have_woken": True}
        delay = _retry_after(retry_after)
        if delay is not None:
            out["retry_after_s"] = delay
        return out
    return {"outcome": "permanent", "why": "http_rejected", "status": status, "may_have_woken": True}


def _proof_message_id(body: bytes, request: Dict[str, Any], secrets: Tuple[str, ...]) -> Optional[str]:
    """Never persist a receiver's raw body, error text or an echoed credential."""
    try:
        receipt = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(receipt, dict) or receipt.get("visible") is not True:
        return None
    for key in ("verification_id", "source_id", "ledger_id", "route", "consumer", "chat"):
        if receipt.get(key) != request[key]:
            return None
    message_id = receipt.get("message_id")
    if (not isinstance(message_id, str) or not MESSAGE_ID_RE.fullmatch(message_id)
            or any(secret in message_id for secret in secrets if secret)):
        return None
    return message_id


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return float(value.strip())
        until = parsedate_to_datetime(value)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def send(adapter: Dict[str, Any], payload: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
    try:
        if adapter["type"] == "command":
            return send_command(adapter, payload, env)
        return send_http(adapter, payload, env)
    except Exception as exc:
        # Third-party exception text can contain the URL or Authorization header.
        return {"outcome": "ambiguous", "why": "adapter_error", "error": type(exc).__name__}


# ------------------------------------------------------------------------------- the pass
def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _later(seconds: float) -> str:
    try:
        return _iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))
    except OverflowError:
        return _iso(datetime.max.replace(tzinfo=timezone.utc))


def enqueue(ledger: Ledger, cfg: Dict[str, Any]) -> int:
    """Create the transport record for every event since the cutoff that has none yet."""
    ledger.bind_notification_owners(cfg)
    cutoff = ledger.notifier_cutoff()
    now = now_iso()
    created = 0
    with ledger.tx() as c:
        rows = c.execute(
            "SELECT e.event_id, e.kind, e.body_json, t.notify_route, t.notify_bound FROM events e "
            "JOIN tasks t ON t.task_id=e.task_id "
            "WHERE e.created_at >= ? AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.event_id=e.event_id) "
            "ORDER BY e.created_at, e.event_id", (cutoff,)).fetchall()
        for r in rows:
            outcome = json.loads(r["body_json"]).get("outcome") or r["kind"]
            route = r["notify_route"]
            state, why = "pending", None
            if r["notify_bound"] != 1:
                state, why, route = "failed", "owner_unresolved", route or "-"
            elif route is None:
                state, why, route = "skipped", "no_route", "-"
            elif route not in cfg["routes"]:
                state, why = "failed", "route_not_configured"
            elif outcome not in cfg["notify_outcomes"]:
                state, why = "skipped", "outcome_not_notified"
            c.execute("INSERT OR IGNORE INTO notifications (event_id, route, state, next_at, last_result_json, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                      (r["event_id"], route, state, now if state == "pending" else None,
                       canonical_json({"why": why}) if why else None, now, now))
            created += 1
        names = tuple(cfg["routes"])
        c.execute("UPDATE notifications SET state='failed', next_at=NULL, last_result_json=?, updated_at=? "
                  f"WHERE state='pending' AND route NOT IN ({','.join('?' for _ in names)})",
                  (canonical_json({"why": "route_not_configured"}), now, *names))
    return created


def sync_acks(ledger: Ledger, cfg: Dict[str, Any]) -> int:
    """A consumer ACK (in ``acks``) closes the transport record of its route. Rows a sender holds
    ('claimed', 'sending') are left to it, so its send is recorded; the next pass closes them."""
    changed = 0
    with ledger.tx() as c:
        owner_ack = ("SELECT a.acked_at FROM acks a JOIN events e ON e.event_id=a.event_id "
                     "JOIN tasks t ON t.task_id=e.task_id WHERE a.event_id=notifications.event_id "
                     "AND t.notify_bound=1 AND a.consumer=t.notify_consumer")
        cur = c.execute(
            f"UPDATE notifications SET state='acked', next_at=NULL, updated_at=?, acked_at=({owner_ack}) "
            "WHERE state NOT IN ('acked','skipped','claimed','sending') "
            f"AND EXISTS ({owner_ack})", (now_iso(),))
        changed += cur.rowcount
    return changed


def recover(ledger: Ledger, cfg: Dict[str, Any]) -> Dict[str, int]:
    """Rows held by a notifier that died: 'claimed' was never sent, 'sending' may have been."""
    out = {"reclaimed": 0, "unconfirmed": 0}
    with ledger.tx() as c:
        rows = c.execute("SELECT * FROM notifications WHERE state IN ('claimed','sending')").fetchall()
        for r in rows:
            with _CLAIMS_LOCK:
                abandoned_here = r["claimed_by"] == os.getpid() and r["claim_id"] not in _ACTIVE_CLAIMS
            if not abandoned_here and procs.same_process_alive(r["claimed_by"], r["claimed_token"]) is not False:
                continue  # its sender may still be working on it
            if r["state"] == "claimed":
                c.execute("UPDATE notifications SET state='pending', next_at=?, claimed_by=NULL, claimed_token=NULL, "
                          "claim_id=NULL, updated_at=? WHERE event_id=? AND route=? AND state='claimed'",
                          (now_iso(), now_iso(), r["event_id"], r["route"]))
                out["reclaimed"] += 1
            else:
                c.execute("UPDATE notifications SET state='unconfirmed', wakes=wakes+1, attempts=attempts+1, next_at=?, "
                          "last_result_json=?, claimed_by=NULL, claimed_token=NULL, claim_id=NULL, updated_at=? "
                          "WHERE event_id=? AND route=? AND state='sending'",
                          (_later(cfg["reminder"]["ack_timeout_s"]),
                           canonical_json({"outcome": "ambiguous", "why": "sender_died_while_sending"}), now_iso(),
                           r["event_id"], r["route"]))
                out["unconfirmed"] += 1
    return out


def remind(ledger: Ledger, cfg: Dict[str, Any]) -> Dict[str, int]:
    """No consumer ACK within ack_timeout_s after a send that may have reached it: one more wake,
    up to max_wakes; then the row stays visible as 'unacknowledged' (needs a human look)."""
    out = {"reminders": 0, "unacknowledged": 0}
    now = now_iso()
    with ledger.tx() as c:
        rows = c.execute("SELECT * FROM notifications WHERE state IN ('accepted','handed_off','unconfirmed') "
                         "AND next_at IS NOT NULL AND next_at <= ?", (now,)).fetchall()
        for r in rows:
            if r["wakes"] < cfg["reminder"]["max_wakes"] and r["attempts"] < cfg["retry"]["max_attempts"]:
                c.execute("UPDATE notifications SET state='pending', next_at=?, reminder=1, updated_at=? "
                          "WHERE event_id=? AND route=? AND state=?", (now, now, r["event_id"], r["route"], r["state"]))
                out["reminders"] += 1
            else:
                c.execute("UPDATE notifications SET state='unacknowledged', next_at=NULL, updated_at=? "
                          "WHERE event_id=? AND route=? AND state=?", (now, r["event_id"], r["route"], r["state"]))
                out["unacknowledged"] += 1
    return out


def _claim(ledger: Ledger, route: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    claim_id = uuid.uuid4().hex
    me = os.getpid()
    now = now_iso()
    with _CLAIMS_LOCK:
        _ACTIVE_CLAIMS.add(claim_id)
    try:
        with ledger.tx() as c:
            c.execute("UPDATE notifications SET state='failed', next_at=NULL, last_result_json=?, updated_at=? "
                      "WHERE route=? AND state='pending' AND (attempts >= ? OR wakes >= ?)",
                      (canonical_json({"why": "budget_exhausted"}), now, route["name"],
                       cfg["retry"]["max_attempts"], cfg["reminder"]["max_wakes"]))
            owner_matches = ("SELECT 1 FROM events e JOIN tasks t ON t.task_id=e.task_id "
                             "WHERE e.event_id=notifications.event_id AND t.notify_bound=1 "
                             "AND t.notify_consumer=? AND t.notify_chat=?")
            c.execute("UPDATE notifications SET state='failed', next_at=NULL, last_result_json=?, updated_at=? "
                      f"WHERE route=? AND state='pending' AND NOT EXISTS ({owner_matches})",
                      (canonical_json({"why": "route_owner_changed"}), now, route["name"],
                       route["consumer"], route["chat"]))
            c.execute("UPDATE notifications SET state='claimed', claimed_by=?, claimed_token=?, claim_id=?, claimed_at=?, "
                      "updated_at=? WHERE route=? AND state='pending' AND next_at <= ? "
                      "AND NOT EXISTS (SELECT 1 FROM acks a WHERE a.event_id=notifications.event_id AND a.consumer=?)",
                      (me, procs.start_token(me), claim_id, now, now, route["name"], now, route["consumer"]))
            rows = c.execute(
                "SELECT n.*, e.task_id, e.attempt_no, e.kind, e.body_json, t.request_key FROM notifications n "
                "JOIN events e ON e.event_id=n.event_id JOIN tasks t ON t.task_id=e.task_id "
                "WHERE n.claim_id=? ORDER BY e.created_at, e.event_id", (claim_id,)).fetchall()
    except BaseException:
        with _CLAIMS_LOCK:
            _ACTIVE_CLAIMS.discard(claim_id)
        raise
    if not rows:
        with _CLAIMS_LOCK:
            _ACTIVE_CLAIMS.discard(claim_id)
    return claim_id, [dict(r) for r in rows]


def delivery_result(cfg: Dict[str, Any], result: Dict[str, Any], attempts: int, wakes: int) -> Dict[str, Any]:
    """Shared bounded transition for task notifications and route verification probes."""
    outcome = result["outcome"]
    attempts += 1
    wakes += int(outcome in ("accepted", "handed_off", "ambiguous") or result.get("may_have_woken", False))
    if outcome in ("accepted", "handed_off", "ambiguous"):
        state = {"accepted": "accepted", "handed_off": "handed_off", "ambiguous": "unconfirmed"}[outcome]
        next_at = _later(cfg["reminder"]["ack_timeout_s"])
    elif (outcome == "retry" and attempts < cfg["retry"]["max_attempts"]
          and wakes < cfg["reminder"]["max_wakes"]):
        delay = min(cfg["retry"]["max_delay_s"], cfg["retry"]["base_delay_s"] * 2 ** (attempts - 1))
        if "retry_after_s" in result:
            delay = max(delay, float(result["retry_after_s"]))
        state, next_at = "pending", _later(delay)
    else:
        state, next_at = "failed", None
    return {"state": state, "attempts": attempts, "wakes": wakes, "next_at": next_at}


def _record(ledger: Ledger, cfg: Dict[str, Any], claim_id: str, result: Dict[str, Any], started: str,
            *, sent: bool = True) -> None:
    now = now_iso()
    outcome = result["outcome"]
    with ledger.tx() as c:
        rows = c.execute("SELECT * FROM notifications WHERE claim_id=? AND state='sending'", (claim_id,)).fetchall()
        for r in rows:
            transition = delivery_result(cfg, result, r["attempts"], r["wakes"])
            c.execute("UPDATE notifications SET state=?, attempts=?, wakes=?, next_at=?, last_result_json=?, "
                      "first_sent_at=COALESCE(first_sent_at, ?), last_sent_at=COALESCE(?, last_sent_at), accepted_at=CASE WHEN ?='accepted' "
                      "THEN COALESCE(accepted_at, ?) ELSE accepted_at END, claimed_by=NULL, claimed_token=NULL, "
                      "claim_id=NULL, reminder=0, updated_at=? WHERE event_id=? AND route=? AND claim_id=?",
                      (transition["state"], transition["attempts"], transition["wakes"], transition["next_at"],
                       canonical_json(result), started if sent else None, now if sent else None, outcome, now, now,
                       r["event_id"], r["route"], claim_id))
            if sent:
                c.execute("INSERT INTO notification_sends (event_id, route, claim_id, started_at, ended_at, outcome, "
                          "detail_json) VALUES (?,?,?,?,?,?,?)",
                          (r["event_id"], r["route"], claim_id, started, now, outcome, canonical_json(result)))


def flush(home: Path, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """One pass: enqueue new events, close ACKed ones, recover dead claims, schedule reminders, and
    send what is due, one batch per route. Safe to run concurrently with other passes."""
    cfg = load_config(home)
    env = dict(os.environ if env is None else env)
    ledger = Ledger(home)
    try:
        ledger.require_notifier()
        summary: Dict[str, Any] = {"enqueued": enqueue(ledger, cfg)}
        summary.update(recover(ledger, cfg))
        summary["acked"] = sync_acks(ledger, cfg)
        summary.update(remind(ledger, cfg))
        sends, blocked = [], []
        for name, route in cfg["routes"].items():
            claim_id, rows = _claim(ledger, route, cfg)
            if not rows:
                continue
            try:
                _hook("notify_after_claim")
                refs = [_event_ref(r) for r in rows]
                payload = build_payload(cfg, route, ledger.ledger_id, refs, reminder=any(r["reminder"] for r in rows))
                started = now_iso()
                with ledger.tx() as c:  # from here on, a crash may leave the consumer woken
                    c.execute("UPDATE notifications SET state='sending', updated_at=? WHERE claim_id=? AND state='claimed'",
                              (started, claim_id))
                _hook("notify_before_send")
                result = (send(route["adapter"], payload, env) if route["proven"] else
                          {"outcome": "retry", "why": "route_unproven"})
                _hook("notify_after_send")
                _record(ledger, cfg, claim_id, result, started, sent=route["proven"])
                (sends if route["proven"] else blocked).append({
                    "route": name, "events": [r["event_id"] for r in refs], "outcome": result["outcome"],
                    "why": result.get("why"), "status": result.get("status")})
            finally:
                with _CLAIMS_LOCK:
                    _ACTIVE_CLAIMS.discard(claim_id)
        summary["sends"] = sends
        summary["blocked"] = blocked
        return summary
    finally:
        ledger.close()


def status(home: Path, *, task_id: Optional[str] = None, event_id: Optional[str] = None) -> List[Dict[str, Any]]:
    ledger = Ledger(home)
    try:
        ledger.require_notifier()
        rows = ledger.conn.execute(
            "SELECT n.*, e.task_id, e.attempt_no, e.kind FROM notifications n JOIN events e ON e.event_id=n.event_id "
            "WHERE (? IS NULL OR e.task_id=?) AND (? IS NULL OR n.event_id=?) ORDER BY n.created_at",
            (task_id, task_id, event_id, event_id)).fetchall()
    finally:
        ledger.close()
    out = []
    for r in rows:
        d = {k: r[k] for k in ("event_id", "route", "state", "attempts", "wakes", "next_at", "task_id", "attempt_no",
                               "kind", "first_sent_at", "last_sent_at", "accepted_at", "acked_at")}
        d["last_result"] = json.loads(r["last_result_json"]) if r["last_result_json"] else None
        d["transport_accepted"] = r["accepted_at"] is not None
        d["consumer_acked"] = r["acked_at"] is not None
        out.append(d)
    return out


def retry(home: Path, event_id: str) -> int:
    """An explicit operator action: give a failed or unacknowledged notification one new budget."""
    ledger = Ledger(home)
    try:
        ledger.require_notifier()
        with ledger.tx() as c:
            cur = c.execute("UPDATE notifications SET state='pending', attempts=0, wakes=0, next_at=?, reminder=0, "
                            "last_result_json=?, updated_at=? WHERE event_id=? AND state IN ('failed','unacknowledged')",
                            (now_iso(), canonical_json({"manual_retry_at": now_iso()}), now_iso(), event_id))
            return cur.rowcount
    finally:
        ledger.close()


def configured(home: Path) -> bool:
    return (Path(home) / CONFIG_NAME).is_file()


def run_forever(home: Path, interval: float, max_passes: Optional[int] = None) -> Dict[str, Any]:
    """`notify run`: one pass every `interval` seconds until SIGTERM/SIGINT (or max_passes)."""
    log_path = Path(home) / LOG_NAME
    passes = errors = 0
    stop = {"set": False}

    import signal as _signal

    def _stop(signum: int, frame: Any) -> None:
        stop["set"] = True

    _signal.signal(_signal.SIGTERM, _stop)
    _signal.signal(_signal.SIGINT, _stop)
    while not stop["set"] and (max_passes is None or passes < max_passes):
        try:
            result = flush(home)
            line = {"at": now_iso(), "ok": True, **{k: v for k, v in result.items() if k != "sends"},
                    "sends": [{k: s[k] for k in ("route", "outcome", "why", "status")} | {"events": len(s["events"])}
                              for s in result["sends"]]}
        except Exception as exc:  # one bad pass must not end the loop; the reason is logged
            errors += 1
            line = {"at": now_iso(), "ok": False, "error": type(exc).__name__,
                    "code": getattr(exc, "code", None)}
        passes += 1
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError:
            pass
        deadline = time.monotonic() + interval
        while not stop["set"] and time.monotonic() < deadline and (max_passes is None or passes < max_passes):
            time.sleep(min(0.5, interval))
    return {"passes": passes, "errors": errors, "log": str(log_path)}


def host_label() -> str:
    return socket.gethostname()

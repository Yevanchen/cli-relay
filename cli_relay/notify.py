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
import ssl
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import business_status, concurrency, engines, procs, transport
from .errors import Refused, UsageError
from .keeper import _hook
from .ledger import Ledger, canonical_json, iso_age_seconds, now_iso

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
    if cfg["routes"]:
        ledger = Ledger(home)
        try:
            managed = dict(ledger.conn.execute("SELECT route, verification_id FROM managed_routes").fetchall()) \
                if ledger.has_notifier() else {}
            legacy = dict(ledger.conn.execute("SELECT route, route_sha256 FROM legacy_routes").fetchall()) \
                if ledger.has_notifier() else {}
            for route in cfg["routes"].values():
                if not route.get("verification"):
                    route["proven"] = bool("verification" not in route and route["name"] not in managed
                                           and legacy.get(route["name"]) == route_fingerprint(cfg["source_id"], route))
                    continue
                proof = route["verification"]
                stored = ledger.conn.execute(
                    "SELECT receipt_json FROM route_verifications WHERE verification_id=? AND route=?",
                    (proof["verification_id"], route["name"])).fetchone() if ledger.has_notifier() else None
                # Configuration alone cannot grant a managed route proof. Only verify/confirm
                # write this ledger record. Legacy exemptions also require a stored identity.
                route["proven"] = bool(managed.get(route["name"]) == proof["verification_id"]
                                       and stored and stored[0] == canonical_json(proof)
                                       and proof["state"] == "acked" and proof["ledger_id"] == ledger.ledger_id
                                       and proof["route_sha256"] == route_fingerprint(cfg["source_id"], route))
        finally:
            ledger.close()
    return cfg


def validate_config(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise _bad("top level must be an object")
    allowed = {"source_id", "default_route", "routes", "notify_outcomes", "retry", "reminder", "local_concurrency"}
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
        # Validation alone cannot grant proof or a legacy exemption; load_config checks the ledger.
        route["proven"] = False
        fallback = route.get("fallback_route")
        if fallback is not None and (fallback == route["name"] or fallback not in clean_routes):
            raise _bad("fallback_route must be another existing route")
    outcomes = raw.get("notify_outcomes", list(DEFAULT_OUTCOMES))
    if (not isinstance(outcomes, list) or not outcomes
            or any(o not in KNOWN_OUTCOMES for o in outcomes)):
        raise _bad("notify_outcomes must be a non-empty list of outcomes", allowed=list(KNOWN_OUTCOMES))
    retry = _numbers(raw.get("retry", {}), {"max_attempts": (6, 1, 50), "base_delay_s": (30.0, 0.01, 3600.0),
                                            "max_delay_s": (1800.0, 0.01, 86400.0)}, "retry")
    reminder = _numbers(raw.get("reminder", {}), {"ack_timeout_s": (1800.0, 0.01, 7 * 86400.0),
                                                  "max_wakes": (2, 1, 10)}, "reminder")
    return {"source_id": source_id, "default_route": default_route, "routes": clean_routes,
            "notify_outcomes": list(outcomes), "retry": retry, "reminder": reminder,
            **({"local_concurrency": concurrency.validate_config(raw["local_concurrency"])}
               if "local_concurrency" in raw else {})}


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
    if not isinstance(spec, dict) or set(spec) - {"consumer", "chat", "adapter", "verification", "fallback_route"}:
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
    if "fallback_route" in spec:
        fallback = spec["fallback_route"]
        if not isinstance(fallback, str) or not ROUTE_RE.fullmatch(fallback):
            raise _bad(f"route {name}: invalid fallback_route")
        route["fallback_route"] = fallback
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
    if not isinstance(value, dict) or set(value) - {"expires_at", "async_accepted"} != fields:
        raise _bad("invalid route verification receipt")
    if "async_accepted" in value and type(value["async_accepted"]) is not bool:
        raise _bad("invalid route verification async acceptance")
    if (not isinstance(value["verification_id"], str)
            or not re.fullmatch(r"v-[0-9a-f]{32}", value["verification_id"])
            or not isinstance(value["route_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["route_sha256"])
            or not isinstance(value["ledger_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value["ledger_id"])
            or value["state"] not in ("pending", "sending", "awaiting_confirmation", "unconfirmed", "failed",
                                       "unacknowledged", "acked")):
        raise _bad("invalid route verification identity or state")
    for key in ("attempts", "wakes"):
        if type(value[key]) is not int or not 0 <= value[key] <= 50:
            raise _bad("invalid route verification budget")
    for key in ("next_at", "created_at", "acked_at", "expires_at"):
        if value.get(key) is not None and (not isinstance(value[key], str)
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
            **{key: body[key] for key in ("sandbox", "full_access", "network_access", "allowed_tools", "permissions",
                                          "completion_marker_required", "completion_marker", "origin", "git", "warnings",
                                          "codex_git_repo_check", "after", "after_require", "queue",
                                          "reason_code", "details", "summary", "unset_env", "unset_env_present", "preflight",
                                          "billing_mode", "billing_env_policy", "billing_env_present",
                                          "billing_env_removed", "billing_env_preserved",
                                          "result_file", "effective_settings", "target_session_id",
                                          *business_status.FIELDS)
               if key in body},
            "completion_report": body.get("completion_report", {"state": "没说做完没"}),
            "result_excerpt": _clean(body.get("result_excerpt"), EXCERPT_MAX)}


def build_payload(cfg: Dict[str, Any], route: Dict[str, Any], ledger_id: str, refs: List[Dict[str, Any]],
                  reminder: bool) -> Dict[str, Any]:
    """The five notify.sh fields plus the structured record. Identifiers, requested permissions and
    a short excerpt are sent: the consumer reads everything else from the ledger by task id."""
    single = refs[0] if len(refs) == 1 else None
    status = single["outcome"] if single else "multiple"
    head = (f"cli-relay {'reminder ' if reminder else ''}source={cfg['source_id']} ledger={ledger_id} "
            f"route={route['name']} events={len(refs)}")
    warnings = [warning for r in refs for warning in r.get("warnings", [])]
    if warnings:
        head += " warnings=" + ",".join(sorted({w["code"] for w in warnings}))
        denied_tools = sorted({tool for w in warnings if w["code"] == "permission_denials"
                               for tool in w["tools"]})
        if denied_tools:
            head += " denied_tools=" + ",".join(denied_tools)
        denied_commands = sorted({command for w in warnings if w["code"] == "allowed_rule_denied"
                                  for command in w["commands"]})
        if denied_commands:
            head += f" allowed_rule_denied={len(denied_commands)} first_command=" + _clean(denied_commands[0], 80)
        if any(w["code"] == "codex_trusted_directory" for w in warnings):
            head += " hint=skip_git_repo_check=true"
    parts = [f"{r['event_id']} {r['task_id']}#{r['attempt_no']} {r['outcome']}" for r in refs[:3]]
    if len(refs) > 3:
        parts.append(f"+{len(refs) - 3} more")
    summary = head + " | " + " ; ".join(parts)
    for ref in refs:
        if ref.get("result_file"):
            addition = " | result_file=" + ref["result_file"]["path"]
            if len(summary + addition) <= SUMMARY_MAX:
                summary += addition
    if single and single["result_excerpt"]:
        summary += " | " + single["result_excerpt"]
    # Excerpts/identifiers remain bounded, but permissions must never be truncated or merged
    # across runs: even a batched notification says exactly what each attempt requested.
    permissions = " | ".join(f"{r['task_id']}#{r['attempt_no']} permissions: {engines.permission_summary(r)}"
                             for r in refs)
    # Business status, like permissions, must survive excerpt/header truncation for every run.
    business = " | ".join(
        f"{r['task_id']}#{r['attempt_no']} business_status={r['business_status']} "
        f"stop_reason={r.get('business_stop_reason')} artifacts={r.get('artifact_verification', {}).get('state', 'unverified')} "
        f"denials={json.dumps(r.get('denials', {}).get('count'))}"
        for r in refs if "business_status" in r)
    completion = " | ".join(
        f"{r['task_id']}#{r['attempt_no']} 做完没=" +
        _clean(r.get("completion_report", {}).get("done", "没说做完没"), 512) +
        (" 还差=" + _clean(r["completion_report"]["remaining"], 512) +
         " 下一步谁动=" + _clean(r["completion_report"]["next_owner"], 512)
         if r.get("completion_report", {}).get("state") == "已报告" else "") for r in refs)
    reasons = " | ".join(f"{r['task_id']}#{r['attempt_no']} {_clean(r['summary'], 300)}"
                         for r in refs if r.get("summary"))
    # The copy-pasteable billing fix is the only actionable part of that block; command adapters
    # receive no structured record, so it is never truncated (names come from BILLING_ENV only).
    fixes = " | ".join(
        f"{r['task_id']}#{r['attempt_no']} billing_env_present fix: submit with a new key and "
        f"{_clean(r['preflight']['fix'].get('submit_flags'), 1000)}, or prefix the command with "
        f"{_clean(r['preflight']['fix'].get('env_prefix'), 1000)}"
        for r in refs if isinstance(r.get("preflight"), dict) and r["preflight"].get("reason") == "billing_env_present"
        and isinstance(r["preflight"].get("fix"), dict))
    record = {"schema": PAYLOAD_SCHEMA, "source_id": cfg["source_id"], "ledger_id": ledger_id,
              "route": route["name"], "chat": route["chat"], "consumer": route["consumer"],
              "reminder": reminder, "count": len(refs), "events": refs}
    return {"job": route["adapter"]["job"], "stage": f"{cfg['source_id']}/{route['name']}", "status": status,
            "summary": _clean(summary, SUMMARY_MAX) + (" | " + business if business else "")
                       + (" | " + reasons if reasons else "") + (" | " + fixes if fixes else "")
                       + " | " + completion + " | " + permissions,
            "artifact": f"cli-relay:{cfg['source_id']}:{ledger_id}",
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
                if _async_receipt(p.stdout[:65536]):
                    return {"outcome": "accepted", "why": "verification_awaiting_confirmation"}
                return {"outcome": "ambiguous", "why": "verification_receipt_invalid"}
            return {"outcome": "accepted", "why": "visible_message_confirmed", "message_id": message_id}
        if adapter["exit_status"] == "verified":
            return {"outcome": "accepted", "why": "command_exit_0_verified"}
        return {"outcome": "handed_off", "why": "command_exit_0_unverified"}
    return {"outcome": "ambiguous", "why": "command_failed", "exit_code": p.returncode}


def send_http(adapter: Dict[str, Any], payload: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
    return transport.run_http(adapter, payload, env)


def _http_exchange(adapter: Dict[str, Any], payload: Dict[str, Any], env: Dict[str, str],
                   phase) -> Dict[str, Any]:
    phase("credentials")
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
            phase("dns")
            addresses = socket.getaddrinfo(parts.hostname, port or (443 if parts.scheme == "https" else 80),
                                           0, socket.SOCK_STREAM)

            def connect_resolved(address, timeout, source_address=None):
                last_error = None
                for family, kind, proto, _, sockaddr in addresses:
                    sock = socket.socket(family, kind, proto)
                    try:
                        phase("tcp")
                        sock.settimeout(timeout)
                        if source_address:
                            sock.bind(source_address)
                        sock.connect(sockaddr)
                        if parts.scheme == "https":
                            phase("tls")
                        return sock
                    except OSError as exc:
                        last_error = exc
                        sock.close()
                if last_error is not None:
                    raise last_error
                raise OSError("no_addresses")

            conn._create_connection = connect_resolved
            conn.connect()
        except (OSError, http.client.HTTPException) as exc:
            error_phase = "dns" if isinstance(exc, socket.gaierror) else "tls" if isinstance(exc, ssl.SSLError) else None
            if error_phase:
                phase(error_phase)
            return {"outcome": "retry", "why": "connect_failed", "error": type(exc).__name__}
        try:
            phase("request")
            conn.request("POST", path, body=body, headers={"Content-Type": "application/json",
                                                           "Authorization": f"Bearer {token}"})
            phase("response_headers")
            resp = conn.getresponse()
            status = resp.status
            retry_after = resp.getheader("Retry-After")
            phase("response_body")
            response_body = resp.read(65536)
            phase("complete")
        except (OSError, http.client.HTTPException) as exc:
            # The request may have reached the receiver (which may already have started a run).
            return {"outcome": "ambiguous", "why": "response_lost", "error": type(exc).__name__}
    finally:
        conn.close()
    if 200 <= status < 300:
        if payload.get("cli_relay", {}).get("schema") == "cli-relay.route-verify.v1":
            message_id = _proof_message_id(response_body, payload["cli_relay"], (url, token))
            if message_id is None:
                if _async_receipt(response_body):
                    return {"outcome": "accepted", "why": "verification_awaiting_confirmation", "status": status}
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


def _async_receipt(body: bytes) -> bool:
    """A run-start acknowledgement is not a failed visible-message receipt.

    An explicit but invalid proof must not silently become confirmable. Raw responses and
    run ids are never persisted; the later confirm is bound to our own verification identity.
    """
    try:
        receipt = json.loads(body)
    except (ValueError, UnicodeError):
        return True
    return isinstance(receipt, dict) and not set(receipt).intersection(
        {"visible", "message_id", "verification_id", "source_id", "ledger_id", "route", "consumer", "chat"})


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
    start_wall, start_mono = time.time(), time.monotonic()
    try:
        if adapter["type"] == "command":
            result = {**send_command(adapter, payload, env), "phase": "command"}
        else:
            result = send_http(adapter, payload, env)
    except Exception as exc:
        # Third-party exception text can contain the URL or Authorization header.
        result = {"outcome": "ambiguous", "why": "adapter_error", "error": type(exc).__name__, "phase": "adapter"}
    result.setdefault("timing", transport.timing(start_wall, start_mono, adapter["timeout_s"]))
    return result


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
                          "first_sent_at=COALESCE(first_sent_at, updated_at), "
                          "last_sent_at=COALESCE(last_sent_at, updated_at), "
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


def _recover_fallbacks(ledger: Ledger) -> None:
    """A fallback may be attempted only once. A lost sender after 'sending' is final."""
    with ledger.tx() as c:
        for row in c.execute("SELECT * FROM notification_fallbacks WHERE state IN ('claimed','sending')").fetchall():
            with _CLAIMS_LOCK:
                abandoned_here = row["claimed_by"] == os.getpid() and row["claim_id"] not in _ACTIVE_CLAIMS
            if not abandoned_here and procs.same_process_alive(row["claimed_by"], row["claimed_token"]) is not False:
                continue
            sending = row["state"] == "sending"
            c.execute("UPDATE notification_fallbacks SET state=?, claim_id=NULL, claimed_by=NULL, claimed_token=NULL, "
                      "ended_at=?, last_result_json=? WHERE event_id=? AND original_route=?",
                      ("unconfirmed" if sending else "pending", now_iso() if sending else None,
                       canonical_json({"why": "fallback_sender_died"}) if sending else None,
                       row["event_id"], row["original_route"]))
        c.execute("UPDATE notification_fallbacks SET state='skipped', ended_at=?, last_result_json=? "
                  "WHERE state='pending' AND EXISTS (SELECT 1 FROM acks a JOIN events e ON e.event_id=a.event_id "
                  "JOIN tasks t ON t.task_id=e.task_id WHERE a.event_id=notification_fallbacks.event_id "
                  "AND a.consumer=t.notify_consumer)",
                  (now_iso(), canonical_json({"why": "original_consumer_acked"})))


def _claim_fallbacks(ledger: Ledger, cfg: Dict[str, Any], original: str,
                     target: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    claim_id = uuid.uuid4().hex
    with _CLAIMS_LOCK:
        _ACTIVE_CLAIMS.add(claim_id)
    try:
        with ledger.tx() as c:
            rows = c.execute(
                "SELECT n.*, e.task_id, e.attempt_no, e.kind, e.body_json, t.request_key, "
                "t.notify_consumer AS original_consumer, t.notify_chat AS original_chat "
                "FROM notifications n JOIN events e ON e.event_id=n.event_id JOIN tasks t ON t.task_id=e.task_id "
                "LEFT JOIN notification_fallbacks f ON f.event_id=n.event_id AND f.original_route=n.route "
                "WHERE n.route=? AND e.kind='terminal' AND t.notify_bound=1 AND t.notify_consumer IS NOT NULL "
                "AND n.state NOT IN ('acked','skipped','claimed','sending') "
                "AND (n.state='failed' OR (n.first_sent_at IS NOT NULL AND n.first_sent_at <= ?)) "
                "AND (f.event_id IS NULL OR f.state='pending') "
                "AND NOT EXISTS (SELECT 1 FROM acks a WHERE a.event_id=n.event_id AND a.consumer=t.notify_consumer) "
                "ORDER BY e.created_at, e.event_id",
                (original, _later(-cfg["reminder"]["ack_timeout_s"]))).fetchall()
            claimed = []
            for row in rows:
                existing = c.execute("SELECT * FROM notification_fallbacks WHERE event_id=? AND original_route=?",
                                     (row["event_id"], original)).fetchone()
                if existing:
                    if (existing["route"], existing["consumer"], existing["chat"]) != (
                            target["name"], target["consumer"], target["chat"]):
                        c.execute("UPDATE notification_fallbacks SET state='failed', ended_at=?, last_result_json=? "
                                  "WHERE event_id=? AND original_route=?",
                                  (now_iso(), canonical_json({"why": "fallback_owner_changed"}), row["event_id"], original))
                        continue
                    c.execute("UPDATE notification_fallbacks SET state='claimed', claim_id=?, claimed_by=?, "
                              "claimed_token=? WHERE event_id=? AND original_route=? AND state='pending'",
                              (claim_id, os.getpid(), procs.start_token(os.getpid()), row["event_id"], original))
                else:
                    c.execute("INSERT INTO notification_fallbacks (event_id, original_route, route, consumer, chat, "
                              "state, claim_id, claimed_by, claimed_token, created_at) VALUES (?,?,?,?,?,'claimed',?,?,?,?)",
                              (row["event_id"], original, target["name"], target["consumer"], target["chat"],
                               claim_id, os.getpid(), procs.start_token(os.getpid()), now_iso()))
                claimed.append(dict(row))
    except BaseException:
        with _CLAIMS_LOCK:
            _ACTIVE_CLAIMS.discard(claim_id)
        raise
    if not claimed:
        with _CLAIMS_LOCK:
            _ACTIVE_CLAIMS.discard(claim_id)
    return claim_id, claimed


def _send_fallbacks(ledger: Ledger, cfg: Dict[str, Any], env: Dict[str, str]) -> List[Dict[str, Any]]:
    from .routes import _locked

    _recover_fallbacks(ledger)
    sends = []
    for original, route in cfg["routes"].items():
        if not route.get("fallback_route"):
            continue
        claim_id = None
        try:
            # Normal sends may have outlived a route edit. Recheck proof and the target
            # under the management lock, holding it until the send is committed.
            with _locked(ledger.home):
                current_cfg = load_config(ledger.home)
                route = current_cfg["routes"].get(original, {})
                target = current_cfg["routes"].get(route.get("fallback_route"))
                if target is None or target["name"] == original or not target["proven"]:
                    continue
                claim_id, rows = _claim_fallbacks(ledger, current_cfg, original, target)
                if not rows:
                    continue
                _hook("fallback_after_claim")
                started = now_iso()
                with ledger.tx() as c:
                    # Recheck the original owner's ACK immediately before committing the send.
                    c.execute("UPDATE notification_fallbacks SET state='skipped', ended_at=?, last_result_json=?, "
                              "claim_id=NULL, claimed_by=NULL, claimed_token=NULL WHERE claim_id=? "
                              "AND EXISTS (SELECT 1 FROM acks a JOIN events e ON e.event_id=a.event_id "
                              "JOIN tasks t ON t.task_id=e.task_id WHERE a.event_id=notification_fallbacks.event_id "
                              "AND a.consumer=t.notify_consumer)",
                              (started, canonical_json({"why": "original_consumer_acked"}), claim_id))
                    c.execute("UPDATE notification_fallbacks SET state='sending', started_at=? "
                              "WHERE claim_id=? AND state='claimed'", (started, claim_id))
                    ids = {row[0] for row in c.execute("SELECT event_id FROM notification_fallbacks "
                                                     "WHERE claim_id=? AND state='sending'", (claim_id,))}
            rows = [row for row in rows if row["event_id"] in ids]
            if not rows:
                continue
            refs = []
            for row in rows:
                refs.append({**_event_ref(row), "original_route": original,
                             "original_consumer": row["original_consumer"], "original_chat": row["original_chat"],
                             "notification_age_s": round(max(0, iso_age_seconds(row["first_sent_at"] or row["created_at"]) or 0)),
                             "fallback_reason": "send_failed" if row["state"] == "failed" else "ack_timeout"})
            payload = build_payload(current_cfg, target, ledger.ledger_id, refs, reminder=True)
            payload["status"] = "fallback_reminder"
            payload["cli_relay"].update(fallback=True, original_route=original)
            origins = " ; ".join(
                f"{ref['event_id']} original_consumer={ref['original_consumer']} "
                f"original_chat={_clean(ref['original_chat'], 80)} age_s={ref['notification_age_s']} "
                f"reason={ref['fallback_reason']}" for ref in refs)
            payload["summary"] = (f"cli-relay FALLBACK REMINDER original_route={original} | {origins} | "
                                  "仅提醒，不代原 consumer ACK | " + payload["summary"])
            _hook("fallback_before_send")
            result = send(target["adapter"], payload, env)
            _hook("fallback_after_send")
            state = {"accepted": "accepted", "handed_off": "handed_off", "ambiguous": "unconfirmed"}.get(
                result["outcome"], "failed")
            with ledger.tx() as c:
                c.execute("UPDATE notification_fallbacks SET state=?, ended_at=?, last_result_json=?, "
                          "claim_id=NULL, claimed_by=NULL, claimed_token=NULL WHERE claim_id=? AND state='sending'",
                          (state, now_iso(), canonical_json(result), claim_id))
            sends.append({"original_route": original, "route": target["name"], "events": [r["event_id"] for r in refs],
                          "outcome": result["outcome"], "why": result.get("why"), "status": result.get("status")})
        finally:
            if claim_id is not None:
                with _CLAIMS_LOCK:
                    _ACTIVE_CLAIMS.discard(claim_id)
    return sends


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
        summary["fallback_sends"] = _send_fallbacks(ledger, cfg, env)
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
        fallbacks = {(r["event_id"], r["original_route"]): dict(r)
                     for r in ledger.conn.execute("SELECT * FROM notification_fallbacks").fetchall()}
    finally:
        ledger.close()
    out = []
    for r in rows:
        d = {k: r[k] for k in ("event_id", "route", "state", "attempts", "wakes", "next_at", "task_id", "attempt_no",
                               "kind", "first_sent_at", "last_sent_at", "accepted_at", "acked_at")}
        d["last_result"] = json.loads(r["last_result_json"]) if r["last_result_json"] else None
        d["transport_accepted"] = r["accepted_at"] is not None
        d["consumer_acked"] = r["acked_at"] is not None
        fallback = fallbacks.get((r["event_id"], r["route"]))
        d["fallback"] = ({key: fallback[key] for key in ("route", "consumer", "chat", "state", "started_at", "ended_at")}
                         | {"last_result": json.loads(fallback["last_result_json"]) if fallback["last_result_json"] else None}
                         if fallback else None)
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

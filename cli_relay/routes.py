"""Local route registration and bounded visible-message verification.

Only reference names/paths and non-secret receipts are persisted in notifier.json. A probe
has its own identity and budget; it is never a task, attempt, or task completion event.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from . import notify
from .errors import Refused, UsageError
from .ledger import Ledger, atomic_write_text, canonical_json, now_iso

VERIFICATION_TTL_S = 86400
TEST_MESSAGE = "cli-relay 接通测试，无需处理"


@contextlib.contextmanager
def _locked(home: Path) -> Iterator[None]:
    # Serialize the read/modify/rename sequence across both processes and threads. Keep the
    # inode in place: unlinking a lock could let a third writer lock a different inode.
    ledger = Ledger(home)
    ledger.close()
    fd = os.open(Path(home) / ".notifier.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if (Path(home) / notify.CONFIG_NAME).is_symlink():
            raise Refused("notifier_symlink", "route management requires a regular notifier.json inside home")
        yield
    finally:
        os.close(fd)


def _save(home: Path, cfg: Dict[str, Any]) -> None:
    raw = {**cfg, "routes": {name: {k: v for k, v in route.items() if k not in ("name", "proven")}
                              for name, route in cfg["routes"].items()}}
    notify.validate_config(raw)
    atomic_write_text(Path(home) / notify.CONFIG_NAME, canonical_json(raw) + "\n", mode=0o600)


def _get(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    if not notify.ROUTE_RE.fullmatch(name):
        raise UsageError("route name must be 1-32 chars of [A-Za-z0-9._-]")
    if name not in cfg["routes"]:
        raise Refused("route_not_configured", "route is not registered")
    return cfg["routes"][name]


def _view(route: Dict[str, Any]) -> Dict[str, Any]:
    return {k: route[k] for k in ("name", "consumer", "chat", "proven")} | {
        "verification": route.get("verification"), **({"fallback_route": route["fallback_route"]}
                                                       if "fallback_route" in route else {})}


def _mark_managed(home: Path, name: str) -> None:
    ledger = Ledger(home)
    try:
        ledger.require_notifier()
        with ledger.tx() as c:
            c.execute("INSERT INTO managed_routes (route, verification_id) VALUES (?,NULL) "
                      "ON CONFLICT(route) DO UPDATE SET verification_id=NULL", (name,))
    finally:
        ledger.close()


def add(home: Path, name: str, *, consumer: str, chat: str, adapter: Dict[str, Any],
        source_id: Optional[str] = None, fallback_route: Optional[str] = None) -> Dict[str, Any]:
    # Validation does not resolve secrets, or read the files they reference.
    candidate = notify._route(name, {"consumer": consumer, "chat": chat, "adapter": adapter, "verification": None})
    with _locked(home):
        if notify.configured(home):
            cfg = notify.load_config(home)
            if source_id is not None and source_id != cfg["source_id"]:
                raise Refused("source_id_conflict", "source_id cannot change during route registration")
        else:
            if source_id is None:
                raise UsageError("first route registration requires --source-id")
            cfg = notify.validate_config({"source_id": source_id, "routes": {}})
        if fallback_route is not None:
            _require_fallback(cfg, name, fallback_route)
            candidate["fallback_route"] = fallback_route
        existing = cfg["routes"].get(name)
        if existing is not None:
            fields = ("consumer", "chat", "adapter")
            if any(existing[k] != candidate[k] for k in fields):
                raise Refused("route_conflict", "route already exists with different configuration; remove it first")
            if fallback_route is not None and existing.get("fallback_route") != fallback_route:
                raise Refused("route_conflict", "use route set-fallback to change an existing fallback")
            if "verification" not in existing:
                # Explicit re-registration opts this route into the managed verification flow.
                existing.update(verification=None, proven=False)
                _mark_managed(home, name)
                _save(home, cfg)
            return {"created": False, "route": _view(existing)}
        for key, value in candidate["adapter"].items():
            if key.endswith("_file"):
                try:
                    info = Path(value).stat()
                except OSError as exc:
                    raise UsageError("credential reference must name an existing private file") from exc
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                    raise UsageError("credential reference must name an owner-only regular file")
        candidate["proven"] = False
        cfg["routes"][name] = candidate
        _mark_managed(home, name)
        _save(home, cfg)
        return {"created": True, "route": _view(candidate)}


def list_routes(home: Path) -> Dict[str, Any]:
    if not notify.configured(home):
        return {"configured": False, "routes": [], "default_route": None}
    cfg = notify.load_config(home)
    return {"configured": True, "source_id": cfg["source_id"], "default_route": cfg["default_route"],
            "routes": [_view(route) | {"adapter": route["adapter"]}
                       for _, route in sorted(cfg["routes"].items())]}


def remove(home: Path, name: str) -> Dict[str, Any]:
    if not notify.ROUTE_RE.fullmatch(name):
        raise UsageError("route name must be 1-32 chars of [A-Za-z0-9._-]")
    with _locked(home):
        if not notify.configured(home):
            return {"removed": False, "name": name}
        cfg = notify.load_config(home)
        if any(r.get("fallback_route") == name for r in cfg["routes"].values()):
            raise Refused("route_in_use", "clear references with route set-fallback --clear before removal")
        removed = cfg["routes"].pop(name, None) is not None
        if removed:
            _mark_managed(home, name)
            if cfg["default_route"] == name:
                cfg["default_route"] = None
            _save(home, cfg)
        return {"removed": removed, "name": name}


def set_default(home: Path, name: str) -> Dict[str, Any]:
    with _locked(home):
        cfg = notify.load_config(home)
        route = _get(cfg, name)
        require_proven(route)
        changed = cfg["default_route"] != name
        if changed:
            cfg["default_route"] = name
            _save(home, cfg)
        return {"default_route": name, "changed": changed}


def require_proven(route: Dict[str, Any]) -> None:
    if not route["proven"]:
        raise Refused("route_unproven", "route needs a visible-message receipt from route verify before task use",
                      route=route["name"])


def _require_fallback(cfg: Dict[str, Any], name: str, fallback: str) -> None:
    if name == fallback:
        raise Refused("fallback_self", "fallback must be a different route")
    require_proven(_get(cfg, fallback))


def set_fallback(home: Path, name: str, fallback: Optional[str]) -> Dict[str, Any]:
    with _locked(home):
        cfg = notify.load_config(home)
        route = _get(cfg, name)
        if fallback is not None:
            _require_fallback(cfg, name, fallback)
        changed = route.get("fallback_route") != fallback
        if changed:
            route.pop("fallback_route", None)
            if fallback is not None:
                route["fallback_route"] = fallback
            _save(home, cfg)
        return {"route": _view(route), "changed": changed}


def doctor(home: Path, env: Dict[str, str]) -> Dict[str, Any]:
    if not notify.configured(home):
        return {"configured": False, "ok": True, "routes": []}
    cfg = notify.load_config(home)
    rows = []
    for name, route in sorted(cfg["routes"].items()):
        adapter = route["adapter"]
        resolved = {kind: bool(notify._secret(adapter, kind, env)) for kind in ("url", "token")} \
            if adapter["type"] == "http" else {}
        proof = ("legacy_config" if "verification" not in route else "receipt") if route["proven"] else "unproven"
        rows.append({"name": name, "proven": route["proven"], "proof": proof, "secret_resolved": resolved})
    default = cfg["default_route"]
    default_usable = default is None or cfg["routes"][default]["proven"]
    return {"configured": True, "source_id": cfg["source_id"], "default_route": default,
            "default_usable": default_usable, "routes": rows,
            "ok": default_usable and all(all(r["secret_resolved"].values()) for r in rows)}


def _receipt(cfg: Dict[str, Any], route: Dict[str, Any], ledger_id: str, *, sent: bool) -> Dict[str, Any]:
    return {"source_id": cfg["source_id"], "ledger_id": ledger_id, "route": route["name"],
            "consumer": route["consumer"], "chat": route["chat"], "proven": route["proven"],
            "sent": sent, "verification": route.get("verification"),
            "verification_id": (route.get("verification") or {}).get("verification_id")}


def _store_verification(home: Path, route: Dict[str, Any]) -> None:
    ledger = Ledger(home)
    try:
        ledger.require_notifier()
        state = route["verification"]
        with ledger.tx() as c:
            c.execute("INSERT INTO managed_routes (route, verification_id) VALUES (?,?) "
                      "ON CONFLICT(route) DO UPDATE SET verification_id=excluded.verification_id",
                      (route["name"], state["verification_id"]))
            c.execute("INSERT INTO route_verifications (verification_id, route, receipt_json) VALUES (?,?,?) "
                      "ON CONFLICT(verification_id) DO UPDATE SET receipt_json=excluded.receipt_json",
                      (state["verification_id"], route["name"], canonical_json(state)))
    finally:
        ledger.close()


def _stored_verification(home: Path, route: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ledger = Ledger(home)
    try:
        ledger.require_notifier()
        row = ledger.conn.execute("SELECT v.receipt_json FROM route_verifications v JOIN managed_routes m "
                                  "ON m.route=v.route AND m.verification_id=v.verification_id WHERE m.route=?",
                                  (route["name"],)).fetchone()
        stored = json.loads(row[0]) if row else None
        return stored if stored and stored["ledger_id"] == ledger.ledger_id else None
    finally:
        ledger.close()


def confirm(home: Path, name: str, *, verification_id: str, message_id: str,
            source_id: Optional[str] = None, ledger_id: Optional[str] = None,
            consumer: Optional[str] = None, chat: Optional[str] = None) -> Dict[str, Any]:
    if not notify.MESSAGE_ID_RE.fullmatch(message_id):
        raise UsageError("invalid --message-id: use 1-256 chars of [A-Za-z0-9._:@/+-]")
    with _locked(home):
        cfg = notify.load_config(home)
        route = _get(cfg, name)
        state = _stored_verification(home, route)
        if not state or state["verification_id"] != verification_id:
            raise Refused("verification_mismatch", "verification id is not the current probe for this route")
        if (state["route_sha256"] != notify.route_fingerprint(cfg["source_id"], route)
                or (state != route.get("verification") and state["state"] != "acked")):
            raise Refused("verification_mismatch", "verification destination or receipt has changed")
        for supplied, expected in ((source_id, cfg["source_id"]), (ledger_id, state["ledger_id"]),
                                   (consumer, route["consumer"]), (chat, route["chat"])):
            if supplied is not None and supplied != expected:
                raise Refused("verification_mismatch", "verification identity does not match this route")
        # Apply the same secret-echo check as the synchronous HTTP receipt, without printing
        # or persisting credential contents. Confirmation never sends a request.
        if route["adapter"]["type"] == "http" and any(
                secret and secret in message_id for secret in
                (notify._secret(route["adapter"], kind, dict(os.environ)) for kind in ("url", "token"))):
            raise UsageError("invalid --message-id")
        if state["state"] == "acked":
            if state["message_id"] != message_id:
                raise Refused("verification_mismatch", "this verification already has a different message id")
            if state != route.get("verification"):
                # The ledger commit may have survived a crash before the config rename.
                route.update(verification=state, proven=True)
                _save(home, cfg)
            return _receipt(cfg, route, state["ledger_id"], sent=False)
        if not state.get("expires_at") or state["expires_at"] <= now_iso():
            raise Refused("verification_expired", "probe has expired; run route verify --retry for a new id")
        if not state.get("async_accepted", state["last_result"] == "verification_awaiting_confirmation"):
            raise Refused("verification_not_confirmable", "probe needs a successful async transport acceptance")
        state.update(state="acked", message_id=message_id, acked_at=now_iso(), next_at=None)
        route.update(verification=state, proven=True)
        _store_verification(home, route)
        _save(home, cfg)
        return _receipt(cfg, route, state["ledger_id"], sent=False)


def verify(home: Path, name: str, *, retry: bool = False,
           env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """One due probe, never a background loop. Repeated calls share a persisted budget/id.

    Hold the config lock through the send so edits/removal cannot borrow a proof of an older
    destination. Persist 'sending' before I/O; a crash consumes a possible wake and is only
    retried after the ACK timeout. The receiver must deduplicate by verification_id.
    """
    with _locked(home):
        cfg = notify.load_config(home)
        route = _get(cfg, name)
        ledger = Ledger(home)
        try:
            ledger_id = ledger.ledger_id
        finally:
            ledger.close()
        fingerprint = notify.route_fingerprint(cfg["source_id"], route)
        if route["proven"]:
            return _receipt(cfg, route, ledger_id, sent=False)
        state = _stored_verification(home, route)
        if state and state["state"] == "acked" and state["route_sha256"] == fingerprint:
            # Recover a crash between the committed proof and the atomic config rename.
            route.update(verification=state, proven=True)
            _save(home, cfg)
            return _receipt(cfg, route, ledger_id, sent=False)
        if state and state["route_sha256"] == fingerprint and route.get("verification") != state:
            # The ledger also owns the current id/budget after an interrupted config rename.
            route["verification"] = state
            _save(home, cfg)
        if state and state.get("expires_at") and state["expires_at"] <= now_iso():
            state.update(state="unacknowledged", next_at=None, last_result="verification_expired")
            route["verification"] = state
            _store_verification(home, route)
            _save(home, cfg)
        if retry and state and state["state"] not in ("failed", "unacknowledged"):
            raise Refused("verification_not_retryable", "--retry only resets an exhausted or failed verification")
        if state is None or state["route_sha256"] != fingerprint or state["ledger_id"] != ledger_id or retry:
            state = {"verification_id": "v-" + uuid.uuid4().hex, "route_sha256": fingerprint, "ledger_id": ledger_id,
                     "state": "pending", "attempts": 0, "wakes": 0, "next_at": now_iso(),
                     "created_at": now_iso(), "expires_at": notify._later(VERIFICATION_TTL_S),
                     "message_id": None, "acked_at": None, "last_result": None, "async_accepted": False}
        route["verification"] = state
        if state["state"] in ("failed", "unacknowledged"):
            return _receipt(cfg, route, ledger_id, sent=False)
        if state["state"] == "sending":
            state.update(state="unconfirmed", last_result="sender_died_while_sending")
            _store_verification(home, route)
            _save(home, cfg)
        if state["next_at"] and state["next_at"] > now_iso():
            return _receipt(cfg, route, ledger_id, sent=False)
        if state["attempts"] >= cfg["retry"]["max_attempts"] or state["wakes"] >= cfg["reminder"]["max_wakes"]:
            state.update(state="unacknowledged" if state["wakes"] else "failed", next_at=None)
            _store_verification(home, route)
            _save(home, cfg)
            return _receipt(cfg, route, ledger_id, sent=False)
        request = {"schema": "cli-relay.route-verify.v1", "verification_id": state["verification_id"],
                   "source_id": cfg["source_id"], "ledger_id": ledger_id, "route": name,
                   "consumer": route["consumer"], "chat": route["chat"], "test_message": TEST_MESSAGE}
        summary = f"{TEST_MESSAGE} {state['verification_id']} source={cfg['source_id']} route={name}"
        payload = {"job": route["adapter"]["job"], "stage": f"{cfg['source_id']}/{name}",
                   "status": "route_verify", "summary": summary,
                   "artifact": f"cli-relay:{cfg['source_id']}:{ledger_id}", "cli_relay": request}
        attempts, wakes = state["attempts"], state["wakes"]
        # Preserve acceptance from older receipts before a reminder overwrites last_result.
        state.setdefault("async_accepted", state["last_result"] == "verification_awaiting_confirmation")
        state.update(state="sending", attempts=attempts + 1, wakes=wakes + 1,
                     next_at=notify._later(cfg["reminder"]["ack_timeout_s"]), last_result="sending")
        _store_verification(home, route)
        _save(home, cfg)
        result = notify.send(route["adapter"], payload, dict(os.environ if env is None else env))
        if (result["outcome"] in ("accepted", "handed_off") and not result.get("message_id")
                and result.get("why") != "verification_awaiting_confirmation"):
            result = {"outcome": "ambiguous", "why": "verification_receipt_invalid"}
        state.update(notify.delivery_result(cfg, result, attempts, wakes), last_result=result["why"])
        if result.get("why") == "verification_awaiting_confirmation":
            state.update(state="awaiting_confirmation", async_accepted=True)
        if result["outcome"] == "accepted" and result.get("message_id"):
            # The validated receiver receipt is the probe ACK. A task's ACKs are never touched.
            state.update(state="acked", message_id=result["message_id"], acked_at=now_iso(), next_at=None)
            route["proven"] = True
        _store_verification(home, route)
        _save(home, cfg)
        return _receipt(cfg, route, ledger_id, sent=True)

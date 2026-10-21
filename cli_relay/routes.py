"""Local route registration and bounded visible-message verification.

Only reference names/paths and non-secret receipts are persisted in notifier.json. A probe
has its own identity and budget; it is never a task, attempt, or task completion event.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from . import notify
from .errors import Refused, UsageError
from .ledger import Ledger, atomic_write_text, canonical_json, now_iso


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
    return {k: route[k] for k in ("name", "consumer", "chat", "proven", "verification")}


def add(home: Path, name: str, *, consumer: str, chat: str, adapter: Dict[str, Any],
        source_id: Optional[str] = None) -> Dict[str, Any]:
    # Validation does not resolve secrets, or read the files they reference.
    candidate = notify._route(name, {"consumer": consumer, "chat": chat, "adapter": adapter})
    with _locked(home):
        if notify.configured(home):
            cfg = notify.load_config(home)
            if source_id is not None and source_id != cfg["source_id"]:
                raise Refused("source_id_conflict", "source_id cannot change during route registration")
        else:
            if source_id is None:
                raise UsageError("first route registration requires --source-id")
            cfg = notify.validate_config({"source_id": source_id, "routes": {}})
        existing = cfg["routes"].get(name)
        if existing is not None:
            fields = ("consumer", "chat", "adapter")
            if any(existing[k] != candidate[k] for k in fields):
                raise Refused("route_conflict", "route already exists with different configuration; remove it first")
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
        removed = cfg["routes"].pop(name, None) is not None
        if removed:
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


def doctor(home: Path, env: Dict[str, str]) -> Dict[str, Any]:
    if not notify.configured(home):
        return {"configured": False, "ok": True, "routes": []}
    cfg = notify.load_config(home)
    rows = []
    for name, route in sorted(cfg["routes"].items()):
        adapter = route["adapter"]
        resolved = {kind: bool(notify._secret(adapter, kind, env)) for kind in ("url", "token")} \
            if adapter["type"] == "http" else {}
        rows.append({"name": name, "proven": route["proven"], "secret_resolved": resolved})
    default = cfg["default_route"]
    default_usable = default is None or cfg["routes"][default]["proven"]
    return {"configured": True, "source_id": cfg["source_id"], "default_route": default,
            "default_usable": default_usable, "routes": rows,
            "ok": default_usable and all(all(r["secret_resolved"].values()) for r in rows)}


def _receipt(cfg: Dict[str, Any], route: Dict[str, Any], ledger_id: str, *, sent: bool) -> Dict[str, Any]:
    return {"source_id": cfg["source_id"], "ledger_id": ledger_id, "route": route["name"],
            "consumer": route["consumer"], "chat": route["chat"], "proven": route["proven"],
            "sent": sent, "verification": route["verification"]}


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
        state = route["verification"]
        if route["proven"]:
            return _receipt(cfg, route, ledger_id, sent=False)
        if retry and state and state["state"] not in ("failed", "unacknowledged"):
            raise Refused("verification_not_retryable", "--retry only resets an exhausted or failed verification")
        if state is None or state["route_sha256"] != fingerprint or state["ledger_id"] != ledger_id or retry:
            state = {"verification_id": "v-" + uuid.uuid4().hex, "route_sha256": fingerprint, "ledger_id": ledger_id,
                     "state": "pending", "attempts": 0, "wakes": 0, "next_at": now_iso(),
                     "created_at": now_iso(), "message_id": None, "acked_at": None, "last_result": None}
            route["verification"] = state
        if state["state"] in ("failed", "unacknowledged"):
            return _receipt(cfg, route, ledger_id, sent=False)
        if state["state"] == "sending":
            state.update(state="unconfirmed", last_result="sender_died_while_sending")
            _save(home, cfg)
        if state["next_at"] and state["next_at"] > now_iso():
            return _receipt(cfg, route, ledger_id, sent=False)
        if state["attempts"] >= cfg["retry"]["max_attempts"] or state["wakes"] >= cfg["reminder"]["max_wakes"]:
            state.update(state="unacknowledged" if state["wakes"] else "failed", next_at=None)
            _save(home, cfg)
            return _receipt(cfg, route, ledger_id, sent=False)
        request = {"schema": "cli-relay.route-verify.v1", "verification_id": state["verification_id"],
                   "source_id": cfg["source_id"], "ledger_id": ledger_id, "route": name,
                   "consumer": route["consumer"], "chat": route["chat"]}
        summary = f"cli-relay visible route verification {state['verification_id']} source={cfg['source_id']} route={name}"
        payload = {"job": route["adapter"]["job"], "stage": f"{cfg['source_id']}/{name}",
                   "status": "route_verify", "summary": summary,
                   "artifact": f"cli-relay:{cfg['source_id']}:{ledger_id}", "cli_relay": request}
        attempts, wakes = state["attempts"], state["wakes"]
        state.update(state="sending", attempts=attempts + 1, wakes=wakes + 1,
                     next_at=notify._later(cfg["reminder"]["ack_timeout_s"]), last_result="sending")
        _save(home, cfg)
        result = notify.send(route["adapter"], payload, dict(os.environ if env is None else env))
        if result["outcome"] in ("accepted", "handed_off") and not result.get("message_id"):
            result = {"outcome": "ambiguous", "why": "verification_receipt_invalid"}
        state.update(notify.delivery_result(cfg, result, attempts, wakes), last_result=result["why"])
        if result["outcome"] == "accepted" and result.get("message_id"):
            # The validated receiver receipt is the probe ACK. A task's ACKs are never touched.
            state.update(state="acked", message_id=result["message_id"], acked_at=now_iso(), next_at=None)
            route["proven"] = True
        _save(home, cfg)
        return _receipt(cfg, route, ledger_id, sent=True)

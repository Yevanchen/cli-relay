"""JSON command line: init, submit, status, wait, events, ack, cancel, resume, reconcile, doctor.

Every command prints one JSON object on stdout (brief --text, --help and --version print text).
Exit codes: 0 ok, 1 internal error, 2 usage, 3 conflict, 4 not found, 5 ledger/storage error,
6 refused (precondition not met), 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, business_status, engines, notify, procs, routes
from .errors import NotFound, Refused, RelayError, UsageError
from .keeper import _hook
from .ledger import ACTIVE_STATES, AFTER_REQUIRE, SCHEDULING_FIELDS, Ledger, canonical_json, init_ledger
from .reconcile import old_run_stopped, reconcile_all, reconcile_task
from .spawn import SpawnFailed, spawn_keeper, spawn_notifier_pass

MAX_PROMPT_BYTES = 1 << 20
_NAME_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,64}$")


def default_home() -> Path:
    return Path(os.environ.get("CLI_RELAY_HOME") or (Path.home() / ".cli-relay"))


def emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _check_name(kind: str, value: str) -> str:
    if not _NAME_RE.match(value or ""):
        raise UsageError(f"invalid {kind}: use 1-64 chars of [A-Za-z0-9._:@/+-]")
    return value


def _check_key(value: str) -> str:
    if not value or len(value) > 256 or any(ord(ch) < 32 for ch in value):
        raise UsageError("invalid key: 1-256 printable characters")
    return value


def _read_prompt(args: argparse.Namespace) -> str:
    if (args.prompt is None) == (args.prompt_file is None):
        raise UsageError("give exactly one of --prompt or --prompt-file")
    if args.prompt_file is not None:
        try:
            data = Path(args.prompt_file).read_bytes()
        except OSError as exc:
            raise UsageError("cannot read --prompt-file", error=str(exc)) from exc
    else:
        data = args.prompt.encode("utf-8")
    if not data.strip():
        raise UsageError("prompt is empty")
    if len(data) > MAX_PROMPT_BYTES:
        raise UsageError("prompt is larger than 1 MiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError("prompt must be UTF-8") from exc


def _parse_options(items: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items or []:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise UsageError(f"--option expects key=value, got {item!r}")
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        out[key] = value
    return out


def _loads(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"unparsable": text[:200]}


def _parse_origin(raw: Optional[str]) -> Dict[str, Any]:
    if raw is None:
        return {}  # preserve the old payload, hash, and output shape byte for byte
    if len(raw.encode("utf-8")) > 4096:
        raise UsageError("--origin must be at most 4096 UTF-8 bytes")

    def unique_pairs(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    try:
        origin = json.loads(raw, object_pairs_hook=unique_pairs)
    except ValueError as exc:
        raise UsageError('--origin must be a JSON object with bot and chat') from exc
    if not isinstance(origin, dict) or set(origin) != {"bot", "chat"}:
        raise UsageError("--origin requires exactly bot and chat")
    if not isinstance(origin["bot"], str) or not _NAME_RE.fullmatch(origin["bot"]):
        raise UsageError("origin.bot must be 1-64 chars of [A-Za-z0-9._:@/+-]")
    chat = origin["chat"]
    if (not isinstance(chat, str) or not 1 <= len(chat) <= 256 or chat != chat.strip()
            or any(not ch.isprintable() for ch in chat)):
        raise UsageError("origin.chat must be 1-256 printable chars without surrounding whitespace")
    return {"origin": origin}


def attempt_view(a: Dict[str, Any], probe: bool = True) -> Dict[str, Any]:
    active = a["state"] in ("waiting", "claimed", "running")
    keeper_alive = procs.same_process_alive(a["keeper_pid"], a["keeper_token"]) if probe and active else None
    cli_alive = procs.same_process_alive(a["cli_pid"], a["cli_token"]) if probe and active and a["cli_pid"] else None
    evidence = _loads(a["evidence_json"])
    return {
        "attempt_no": a["attempt_no"], "attempt_id": a["attempt_id"], "reason": a["reason"], "state": a["state"],
        "outcome": a["outcome"], "host": a["host"], "cwd": a["cwd"],
        "native_session_id": a["native_session_id"], "resume_session_id": a["resume_session_id"],
        "keeper": {"pid": a["keeper_pid"], "pgid": a["keeper_pgid"], "alive": keeper_alive},
        "cli": {"pid": a["cli_pid"], "alive": cli_alive},
        "exit": {"code": a["exit_code"], "signal": a["exit_signal"], "known": bool(a["exit_known"])},
        "native": _loads(a["native_result_json"]), "evidence": evidence,
        **{key: evidence[key] for key in business_status.FIELDS if isinstance(evidence, dict) and key in evidence},
        "cancel_requested_at": a["cancel_requested_at"], "created_at": a["created_at"],
        "claimed_at": a["claimed_at"], "spawn_started_at": a["spawn_started_at"],
        "started_at": a["started_at"], "ended_at": a["ended_at"],
    }


def task_view(ledger: Ledger, task_id: str, probe: bool = True) -> Dict[str, Any]:
    t = ledger.get_task(task_id)
    payload = json.loads(t["payload_json"])
    attempts = [attempt_view(a, probe) for a in ledger.attempts(task_id)]
    current = next((a for a in attempts if a["attempt_no"] == t["current_attempt"]), {})
    if current.get("state") == "waiting":
        current["waiting_on"] = ledger.waiting_on(task_id)
    return {
        "task": {k: t[k] for k in ("task_id", "scope", "engine", "cwd", "state", "current_attempt", "submit_count",
                                   "payload_sha256", "created_at", "updated_at")} | {
                                       "key": t["request_key"], "options": payload.get("options", {}),
                                       **{key: current[key] for key in business_status.FIELDS if key in current},
                                       **({"waiting_on": current["waiting_on"]} if "waiting_on" in current else {}),
                                       **{key: payload[key] for key in SCHEDULING_FIELDS if key in payload},
                                       **({"origin": payload["origin"]} if "origin" in payload else {}),
                                       **engines.request_details(t["engine"], payload)},
        "attempts": attempts,
        "events": [{"event_id": e["event_id"], "attempt_no": e["attempt_no"], "kind": e["kind"],
                    "created_at": e["created_at"], "acked_by": e["acked_by"], "acks": e["acks"],
                    "body": _loads(e["body_json"])}
                   for e in ledger.events_of_task(task_id)],
    }


def _spawner(home: Path):
    def spawn(task_id: str, attempt_id: str) -> None:
        spawn_keeper(home, task_id, attempt_id)
    return spawn


def _resolve_task(ledger: Ledger, args: argparse.Namespace) -> str:
    if getattr(args, "task_id", None):
        return ledger.get_task(args.task_id)["task_id"]
    if getattr(args, "key", None):
        return ledger.find_task(args.scope, args.key)["task_id"]
    raise UsageError("give a task id or --key")


# ------------------------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace) -> int:
    emit({"ok": True, **init_ledger(args.home)})
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    engine = engines.validate_engine(args.engine)
    scope = _check_name("scope", args.scope)
    key = _check_key(args.key)
    prompt = _read_prompt(args)
    cwd = os.path.realpath(args.cwd)
    raw_options = _parse_options(args.option)
    if args.unset_env is not None:
        names = engines.validate_unset_env(args.unset_env)
        if "unset_env" in raw_options and engines.validate_unset_env(raw_options["unset_env"]) != names:
            raise UsageError("--unset-env conflicts with --option unset_env")
        raw_options["unset_env"] = names
    for flag, field in (("allow_write_dir", "allowed_write_dirs"),
                        ("allow_bash", "allowed_bash_commands"), ("network_access", "network_access"),
                        ("billing_mode", "billing_mode")):
        value = getattr(args, flag, None)
        if value is not None:
            if field in raw_options and raw_options[field] != value:
                raise UsageError(f"--{flag.replace('_', '-')} conflicts with --option {field}")
            raw_options[field] = value
    options = engines.validate_options(engine, raw_options, cwd=cwd)
    payload = {"engine": engine, "cwd": cwd, "prompt": prompt, "options": options, **_parse_origin(args.origin)}
    if args.after_require is not None and not args.after:
        raise UsageError("--after-require requires --after")
    if args.after and len(args.after) != 1:
        raise UsageError("--after accepts one dependency")
    if args.queue is not None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.queue):
            raise UsageError("invalid --queue: use 1-64 chars of [A-Za-z0-9._-]")
        payload["queue"] = args.queue
    ledger = Ledger(args.home, integrity=True)
    if args.after:
        payload["after"] = ledger.resolve_dependency(scope, args.after[0])
    if args.after_require is not None:
        payload["after_require"] = args.after_require
    existing = None
    try:
        existing = ledger.find_task(scope, key)
    except NotFound:
        # Preconditions for new work only: a caller re-sending a known key (e.g. after a lost
        # reply) must get its task back even if the directory is gone or the disk is nearly full.
        if not os.path.isdir(cwd):
            raise UsageError("--cwd must be an existing directory")
        ledger.check_disk()
    route = consumer = chat = None
    bound = False
    if existing is None:
        # A retry recovers its first receipt, even if the route was removed or the
        # notifier configuration changed after the original transaction committed.
        cfg = notify.load_config(args.home) if notify.configured(args.home) else None
        if args.notify_route is not None:
            if cfg is None:
                raise Refused("route_not_configured", "register and verify a route before submitting with --notify-route")
            route = _check_route(args.notify_route, cfg)
        if cfg is not None:
            route = route if route is not None else cfg["default_route"]
            if route is not None:
                owner = cfg["routes"][route]
                routes.require_proven(owner)
                consumer, chat = owner["consumer"], owner["chat"]
            bound = True
    native_sid = str(uuid.uuid4()) if engine in ("claude", "fixture") else None
    status, task, sha = ledger.submit(scope=scope, key=key, payload=payload, prompt=prompt,
                                      native_session_id=native_sid, notify_route=route,
                                      notify_consumer=consumer, notify_chat=chat, notify_bound=bound)
    _hook("submit_after_commit")  # fault injection: committed, keeper not yet started, no response sent
    keeper = "already_started"
    att = ledger.current_attempt(task["task_id"])
    if status == "created":
        try:
            spawn_keeper(args.home, task["task_id"], att["attempt_id"])
            keeper = "spawned"
        except SpawnFailed:
            keeper = "spawn_failed_retry_with_same_key"
    else:
        actions = reconcile_task(ledger, task["task_id"], kick=True, spawn=_spawner(args.home))
        _after_reconcile(args.home, actions)
        if any(a["action"] in ("kicked_keeper", "released_claim_and_kicked") for a in actions):
            keeper = "kicked"
        task = ledger.get_task(task["task_id"])
        att = ledger.current_attempt(task["task_id"])
    warnings = engines.bash_rule_warnings(engine, payload)
    emit({"ok": True, "accepted": True, "status": status, "task_id": task["task_id"], "state": task["state"],
          "attempt_no": att["attempt_no"], "attempt_id": att["attempt_id"], "submit_count": task["submit_count"],
          "payload_sha256": sha, "native_session_id": att["native_session_id"], "keeper": keeper,
          **({"origin": payload["origin"]} if "origin" in payload else {}),
          **{key: payload[key] for key in SCHEDULING_FIELDS if key in payload},
          **({"warnings": warnings} if warnings else {}),
          **engines.request_details(task["engine"], json.loads(task["payload_json"])),
          "notify_route": task.get("notify_route"), "notify_consumer": task.get("notify_consumer"),
          "notify_chat": task.get("notify_chat"), "notify_bound": task.get("notify_bound") == 1,
          "notify_owner_state": "manual_restore" if task.get("notify_bound") == -1 else
          ("bound" if task.get("notify_bound") == 1 else "unbound"),
          "ledger_id": ledger.ledger_id})
    return 0


def _check_route(route: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    if not notify.ROUTE_RE.fullmatch(route or ""):
        raise UsageError("invalid --notify-route: use 1-32 chars of [A-Za-z0-9._-]")
    if cfg is not None:
        if route not in cfg["routes"]:
            raise UsageError("--notify-route is not one of the routes in notifier.json", allowed=sorted(cfg["routes"]))
    return route


def _after_reconcile(home: Path, actions: List[Dict[str, Any]]) -> None:
    """Reconciliation that closed an attempt created an event: start a notifier pass for it."""
    if any(a.get("event_id") for a in actions) and notify.configured(home):
        spawn_notifier_pass(home)


def _source(home: Path, ledger: Ledger) -> Dict[str, Any]:
    source_id = default_route = None
    if notify.configured(home):
        try:
            cfg = notify.load_config(home)
            source_id, default_route = cfg["source_id"], cfg["default_route"]
        except RelayError:
            pass
    return {"source_id": source_id, "ledger_id": ledger.ledger_id, "default_route": default_route}


def cmd_status(args: argparse.Namespace) -> int:
    ledger = Ledger(args.home)
    task_id = _resolve_task(ledger, args)
    actions = [] if args.no_reconcile else reconcile_task(ledger, task_id, kick=True, spawn=_spawner(args.home))
    _after_reconcile(args.home, actions)
    emit({"ok": True, **task_view(ledger, task_id), "reconcile": actions})
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    ledger = Ledger(args.home)
    task_id = _resolve_task(ledger, args)
    deadline = time.monotonic() + max(0.0, args.timeout)
    while True:
        _after_reconcile(args.home, reconcile_task(ledger, task_id, kick=True, spawn=_spawner(args.home)))
        state = ledger.get_task(task_id)["state"]
        if state not in ("accepted", "waiting", "running"):
            emit({"ok": True, "timed_out": False, **task_view(ledger, task_id)})
            return 0
        if time.monotonic() >= deadline:
            # Waiting stops here; the task itself keeps running.
            emit({"ok": True, "timed_out": True, **task_view(ledger, task_id)})
            return 0
        time.sleep(max(0.05, min(args.interval, deadline - time.monotonic())))


def cmd_events(args: argparse.Namespace) -> int:
    consumer = _check_name("consumer", args.consumer)
    ledger = Ledger(args.home)
    if ledger.has_notifier() and notify.configured(args.home):
        try:
            cfg = notify.load_config(args.home)
        except RelayError:
            pass  # Stored results and ownership remain readable with an invalid notifier config.
        else:
            ledger.bind_notification_owners(cfg)
    if not args.no_reconcile:
        # Polling is the recovery path too: an attempt that provably never started is kicked here.
        if args.task:
            _after_reconcile(args.home, reconcile_task(ledger, args.task, kick=True, spawn=_spawner(args.home)))
        else:
            _after_reconcile(args.home, reconcile_all(ledger, kick=True, spawn=_spawner(args.home)))
    rows = ledger.events_for(consumer, task_id=args.task, include_acked=args.include_acked)
    warning = None
    try:
        counts = ledger.record_deliveries(consumer, [r["event_id"] for r in rows])
    except RelayError as exc:  # delivery bookkeeping is best effort; never hide events because of it
        counts, warning = {}, exc.code
    source = _source(args.home, ledger)
    owners = ledger.task_notification_owners([r["task_id"] for r in rows])
    source.pop("default_route")
    # Every event names its machine, its ledger and its route, so two hosts are never mixed up.
    events = [{"event_id": r["event_id"], "task_id": r["task_id"], "attempt_no": r["attempt_no"], "kind": r["kind"],
               "created_at": r["created_at"], "acked_at": r["acked_at"],
               "message_id": r["message_id"], "chat": r["chat"],
               "superseded": r["attempt_no"] < r["current_attempt"], "delivery_count": counts.get(r["event_id"]),
               **source, "route": owners.get(r["task_id"], {}).get("route"),
               "owner_consumer": owners.get(r["task_id"], {}).get("consumer"),
               "owner_chat": owners.get(r["task_id"], {}).get("chat"),
               "route_bound": owners.get(r["task_id"], {}).get("bound", False),
               "owner_state": owners.get(r["task_id"], {}).get("binding_state", "manual_restore"),
               "body": _loads(r["body_json"])} for r in rows]
    out: Dict[str, Any] = {"ok": True, "consumer": consumer, **source, "events": events}
    if warning:
        out["warning"] = warning
    emit(out)
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    """Read committed terminal snapshots only: no reconciliation, binding or delivery writes."""
    consumer = _check_name("consumer", args.consumer)
    ledger = Ledger(args.home)
    try:
        ledger.require_notifier()  # the printed --message-id ACK must be executable on this schema
        source = _source(args.home, ledger)
        source.pop("default_route")
        rows = ledger.events_for(consumer, task_id=args.task)
        owners = ledger.task_notification_owners([row["task_id"] for row in rows])
        events, refs = [], []
        for row in rows:
            if row["kind"] not in ("terminal", "unknown"):
                continue
            owner = owners.get(row["task_id"], {})
            if owner.get("consumer") is not None and owner["consumer"] != consumer:
                continue
            if args.route is not None and owner.get("route") != args.route:
                continue
            task = ledger.get_task(row["task_id"])
            body = _loads(row["body_json"])
            result_file = body.get("result_file") or business_status.result_file_details(json.loads(task["payload_json"]))
            if result_file:
                try:
                    exists = Path(result_file["path"]).is_file()
                except OSError:
                    exists = False
                result_file = {**result_file, "exists": exists}
            outcome = body.get("outcome") or row["kind"]
            status = body.get("business_status", "unreported")
            if status == "stopped_for_approval":
                conclusion = "等待批准"
            elif outcome != "succeeded":
                conclusion = {"failed": "执行失败", "blocked": "执行受阻", "needs_input": "需要输入",
                              "unknown": "执行结果未知", "cancelled": "已取消"}.get(outcome, "执行结果未知")
            else:
                conclusion = {"ok": "已完成", "partial": "部分完成", "failed": "业务失败",
                              "unreported": "CLI 已结束，业务结果未报告"}.get(status, "业务结果未报告")
            identity = {key: row[key] for key in ("event_id", "task_id", "attempt_no")}
            short = {"task_last8": row["task_id"][-8:], "event_last8": row["event_id"][-8:]}
            path = result_file["path"] if result_file else "无结果文件"
            path = "".join(ch if ch.isprintable() else f"\\u{ord(ch):04x}" for ch in path)
            message = (f"CLI_RELAY {source['source_id'] or 'local'} {short['task_last8']}/{short['event_last8']} "
                       f"{notify._clean(task['request_key'], 256)}：{conclusion} | {path}")
            events.append({**identity, **source, **short, "key": task["request_key"], "origin": body.get("origin"),
                           "route": owner.get("route"), "owner_chat": owner.get("chat"), "outcome": outcome,
                           "business_status": status, "business_status_source": body.get("business_status_source", "none"),
                           "warnings": body.get("warnings", []), "result_file": result_file, "message": message})
            refs.append({**identity, **source, "consumer": consumer})
        command = None
        if refs:
            argv = ["cli-relay", "--home", str(args.home), "ack", "--consumer", consumer,
                    "--ledger-id", ledger.ledger_id]
            if source["source_id"] is not None:
                argv += ["--source-id", source["source_id"]]
            chats = {event["owner_chat"] for event in events}
            chat = next(iter(chats)) if len(chats) == 1 else None
            if chat and chat.isprintable():
                argv += ["--chat", chat]
            argv += ["--events", canonical_json(refs)]
            command = shlex.join(argv) + " --message-id '<MESSAGE_ID>'"
        if args.text:
            for event in events:
                sys.stdout.write(" ".join(f"{key}={canonical_json(value)}" for key, value in event.items()
                                          if key != "message") + "\n" + event["message"] + "\n")
            if command:
                sys.stdout.write(command + "\n")
        else:
            emit({"ok": True, "consumer": consumer, **source, "events": events, "ack_command": command})
        return 0
    finally:
        ledger.close()


def _batch_events(raw: str) -> List[Dict[str, Any]]:
    try:
        refs = json.loads(raw)
    except ValueError as exc:
        raise UsageError("--events requires the JSON array printed by brief") from exc
    keys = {"event_id", "task_id", "attempt_no", "ledger_id", "source_id", "consumer"}
    if not isinstance(refs, list) or not refs:
        raise UsageError("--events requires a non-empty JSON array")
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != keys
                or any(not isinstance(ref[key], str) or not ref[key] for key in keys - {"attempt_no", "source_id"})
                or type(ref["attempt_no"]) is not int or ref["attempt_no"] < 1
                or (ref["source_id"] is not None and not isinstance(ref["source_id"], str))):
            raise UsageError("each batch event needs event_id, task_id, attempt_no, ledger_id, source_id and consumer")
    return refs


def cmd_ack(args: argparse.Namespace) -> int:
    consumer = _check_name("consumer", args.consumer)
    if args.events is not None:
        if any(value is not None for value in (args.task, args.attempt, args.event)):
            raise UsageError("--events cannot be combined with --task/--attempt/--event")
        refs = _batch_events(args.events)
    else:
        if any(value is None for value in (args.task, args.attempt, args.event)):
            raise UsageError("give --task, --attempt and --event, or --events from brief")
        refs = [{"task_id": args.task, "attempt_no": args.attempt, "event_id": args.event}]
    if args.message_id is not None and not notify.MESSAGE_ID_RE.fullmatch(args.message_id):
        raise UsageError("invalid --message-id: use 1-256 chars of [A-Za-z0-9._:@/+-]")
    if args.chat is not None and (not 1 <= len(args.chat) <= 256 or any(not c.isprintable() for c in args.chat)):
        raise UsageError("--chat must be 1-256 printable characters")
    ledger = Ledger(args.home)
    # A consumer serving two machines names the ledger it means; an ACK never lands on another one.
    source = _source(args.home, ledger)
    if args.ledger_id is not None and args.ledger_id != ledger.ledger_id:
        raise Refused("wrong_ledger", "this event belongs to another ledger", ledger_id=ledger.ledger_id,
                      given_ledger_id=args.ledger_id)
    if args.source_id is not None and args.source_id != source["source_id"]:
        raise Refused("wrong_source", "this event belongs to another machine", source_id=source["source_id"],
                      given_source_id=args.source_id)
    for ref in refs:
        if "source_id" in ref and ref["source_id"] != source["source_id"]:
            raise Refused("wrong_source", "batch event belongs to another machine")
    res = ledger.ack_many(refs, consumer=consumer, message_id=args.message_id, chat=args.chat)
    source.pop("default_route")
    emit({"ok": True, "acked": True, **({"events": res} if args.events is not None else res[0]), **source})
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    ledger = Ledger(args.home)
    task_id = _resolve_task(ledger, args)
    result, att, event_id = ledger.request_cancel(task_id)
    signalled, why_not = "none", None
    if result == "cancel_requested":
        # The request is in the ledger either way; a signal is only sent to a verified process.
        att = ledger.get_attempt(att["attempt_id"])
        keeper_alive = procs.same_process_alive(att["keeper_pid"], att["keeper_token"])
        if keeper_alive is True:
            os.kill(att["keeper_pid"], signal.SIGUSR1)  # the keeper stops the CLI and records the outcome
            signalled = "keeper"
        elif keeper_alive is False:
            signalled = _signal_verified_orphan(att)
            if signalled == "none":
                why_not = "keeper gone and no verified CLI process; reconcile settles the attempt"
        else:
            why_not = "keeper identity unverifiable; a live keeper reads the request from the ledger"
    elif result == "not_cancellable":
        raise Refused("not_cancellable", "the current attempt is not running", state=att["state"],
                      outcome=att["outcome"])
    out = {"ok": True, "result": result, "signalled": signalled, "task_id": task_id,
           "attempt_no": att["attempt_no"], "event_id": event_id}
    if why_not:
        out["not_signalled_reason"] = why_not
    emit(out)
    return 0


def _signal_verified_orphan(att: Dict[str, Any]) -> str:
    """SIGTERM a CLI whose keeper is gone, but only after proving it is still the recorded process.

    The whole process group is signalled only while the verified CLI is still a member of the
    recorded group: a group id cannot be reused while a member exists, so the group is ours.
    Without a verified CLI nothing is signalled (a stale pid or pgid may belong to anyone now).
    """
    if not att["cli_pid"] or procs.same_process_alive(att["cli_pid"], att["cli_token"]) is not True:
        return "none"
    try:
        if att["keeper_pgid"] and os.getpgid(att["cli_pid"]) == att["keeper_pgid"]:
            os.killpg(att["keeper_pgid"], signal.SIGTERM)
            return "orphan_process_group"
        os.kill(att["cli_pid"], signal.SIGTERM)
        return "orphan_cli"
    except (ProcessLookupError, PermissionError):
        return "none"


def cmd_resume(args: argparse.Namespace) -> int:
    ledger = Ledger(args.home, integrity=True)
    ledger.check_disk()
    task_id = _resolve_task(ledger, args)
    task = ledger.get_task(task_id)
    att = ledger.current_attempt(task_id)
    if att["state"] in ACTIVE_STATES:
        raise Refused("attempt_active", "the current attempt is still active; wait or cancel first",
                      state=att["state"])
    proof = old_run_stopped(att)
    deadline = time.monotonic() + 10.0
    while not proof["stopped"] and att["state"] == "finished" and time.monotonic() < deadline:
        time.sleep(0.2)  # a keeper that just committed its result is still exiting
        proof = old_run_stopped(att)
    if not proof["stopped"]:
        raise Refused("old_run_may_be_alive", "cannot prove the previous run stopped; refusing to resume", **proof)
    if args.fresh:
        if not args.confirm_rerun:
            raise Refused("rerun_not_confirmed",
                          "--fresh starts a new native session from the original prompt and may repeat side "
                          "effects; add --confirm-rerun to proceed")
        payload = json.loads(task["payload_json"])
        prompt = payload["prompt"]
        resume_sid = None
        native_sid = str(uuid.uuid4()) if task["engine"] in ("claude", "fixture") else None
        reason = "rerun"
    else:
        prompt = _read_prompt(args)
        resume_sid = att["native_session_id"]
        if not resume_sid:
            raise Refused("no_native_session", "the previous attempt has no recorded native session id; "
                          "exact resume is impossible (use --fresh --confirm-rerun to start over)")
        native_sid = resume_sid
        reason = "resume"
    new = ledger.new_attempt(task_id, expect_attempt_no=att["attempt_no"], reason=reason, prompt=prompt,
                             resume_session_id=resume_sid, native_session_id=native_sid)
    try:
        spawn_keeper(args.home, task_id, new["attempt_id"])
        keeper = "spawned"
    except SpawnFailed:
        keeper = "spawn_failed_run_reconcile"
    payload = json.loads(task["payload_json"])
    emit({"ok": True, "task_id": task_id, "attempt_no": new["attempt_no"], "attempt_id": new["attempt_id"],
          "reason": reason, "resume_session_id": resume_sid, "previous_attempt_stopped": proof, "keeper": keeper,
          **({"origin": payload["origin"]} if "origin" in payload else {}),
          **{key: payload[key] for key in SCHEDULING_FIELDS if key in payload},
          **engines.request_details(task["engine"], payload)})
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    ledger = Ledger(args.home)
    spawn = _spawner(args.home)
    if args.task_id:
        actions = reconcile_task(ledger, ledger.get_task(args.task_id)["task_id"], kick=True, spawn=spawn)
    else:
        actions = reconcile_all(ledger, kick=True, spawn=spawn)
    _after_reconcile(args.home, actions)
    emit({"ok": True, "actions": actions})
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    out: Dict[str, Any] = {"ok": True, "version": __version__, "python": platform.python_version(),
                           "platform": platform.system()}
    out["codex_sandbox_policy"] = {
        "default_sandbox": "read-only", "full_access_default": False,
        "full_access_opt_in": '--option sandbox="danger-full-access" --option allow_full_access=true',
        "allow_full_access_without_sandbox": "harmless; keeps the requested sandbox or the read-only default",
        "risk": "danger-full-access disables the Codex sandbox: commands can access files outside the task "
                "directory and use the network, subject to host permissions",
        "checks": "config-layer permission checks, forbidden flags and subscription/billing checks still apply",
        "network_default": False,
        "network_opt_in": '--option sandbox="workspace-write" --option allow_network=true',
        "network_without_opt_in": "workspace-write runs get sandbox_workspace_write.network_access=false: commands "
                                  "cannot open network connections, not even to localhost, so tests that start a "
                                  "local server (for example a fake webhook) fail inside the sandbox",
        "network_effect": "adds -c sandbox_workspace_write.network_access=true on the first run and on exact "
                          "resume; the sandbox stays workspace-write, never danger-full-access, and no other "
                          "config override is accepted",
        "network_scope": "only with sandbox=\"workspace-write\": with read-only (the default) or "
                         "danger-full-access, allow_network is a usage error",
        "network_risk": "Codex has no loopback-only setting: allow_network opens all network access from the "
                        "sandbox, so commands can reach other hosts and send data out; writes stay limited to "
                        "the workspace",
        "machine_defaults": "doctor --engine codex reports engine.workspace_network for --cwd: layer names and "
                            "booleans only; a relay task never takes this setting from the configuration",
    }
    out["completion_marker_policy"] = {
        "default_required": False, "opt_in": "--option require_completion_marker=true",
        "complete_line": engines.COMPLETION_LINE_PREFIX + "complete",
        "incomplete_line": engines.COMPLETION_LINE_PREFIX + "incomplete",
        "rule": "with the opt-in, a run that would end succeeded ends needs_input unless the last non-empty "
                "line of the CLI's final message is exactly the complete line; the incomplete line, any other "
                "last line or no final message gives needs_input, and other outcomes are unchanged",
        "prompt": "the relay never edits a prompt: ask for one of the two lines in the task prompt and in "
                  "resume prompts, as a plain last line without formatting",
        "limits": "a self-report, not verification: the relay runs no check of its own and reads no other "
                  "text of the result; without the opt-in, succeeded only means the CLI ended its turn "
                  "without an error",
    }
    out["claude_tool_policy"] = {
        "default_allowed_tools": [], "allowable_tools": list(engines.CLAUDE_ALLOWABLE_TOOLS),
        "opt_in": "--option 'allowed_tools=[\"WebSearch\",\"WebFetch\"]'",
        "write_opt_in": "--allow-write-dir './out/**' (repeatable; allowed_write_dirs JSON list)",
        "bash_opt_in": "--allow-bash python3 (repeatable; allowed_bash_commands JSON list)",
        "effect": "adds --allowedTools for that task only; the permission mode stays dontAsk and no "
                  "--settings layer or settings file is changed",
        "precedence": "deny and ask rules of every settings layer, managed included, still win, and managed "
                      "allowManagedPermissionRulesOnly ignores --allowedTools; a tool still denied ends the "
                      "task needs_input",
        "risk": "WebSearch and WebFetch reach the network: fetched pages are untrusted input, and a fetched "
                "URL can carry data out of the task",
        "machine_defaults": "doctor --engine claude reports engine.network_tools for --cwd: tool names and "
                            "booleans only",
    }
    try:
        ledger = Ledger(args.home, integrity=True)
        out["ledger"] = {"home": str(args.home), "integrity": "ok"}
        try:
            ledger.check_disk()
            out["disk"] = {"ok": True, "free_bytes": shutil.disk_usage(str(args.home)).free}
        except RelayError as exc:
            out["ok"] = False
            out["disk"] = {"ok": False, **exc.detail}
    except RelayError as exc:
        out["ok"] = False
        out["ledger"] = {"home": str(args.home), "error": exc.code, "message": exc.message}
    try:
        out["notifier"] = routes.doctor(args.home, dict(os.environ))
        out["ok"] = out["ok"] and out["notifier"]["ok"]
    except RelayError as exc:
        out["ok"] = False
        out["notifier"] = {"ok": False, "error": exc.code}
    if args.engine:
        cwd = args.cwd or os.getcwd()
        pf = engines.preflight(engines.validate_engine(args.engine), dict(os.environ), cwd=cwd)
        out["engine"] = {"name": args.engine, "ok": pf.ok, "reason": pf.reason, **pf.detail}
        if args.engine == "claude":  # informational: never changes ok
            out["engine"]["network_tools"] = engines.claude_tool_rules(dict(os.environ), cwd)
        elif args.engine == "codex":  # informational: never changes ok
            out["engine"]["workspace_network"] = engines.codex_workspace_network(dict(os.environ), cwd)
        out["ok"] = out["ok"] and pf.ok
    emit(out)
    return 0 if out["ok"] else 6


def cmd_keeper(args: argparse.Namespace) -> int:
    from .keeper import run
    return run(args.home, args.task_id, args.attempt_id)


def cmd_migrate(args: argparse.Namespace) -> int:
    ledger = Ledger(args.home, integrity=True)
    emit({"ok": True, **ledger.migrate(backfill=args.backfill), "ledger_id": ledger.ledger_id})
    return 0


def cmd_notify_flush(args: argparse.Namespace) -> int:
    emit({"ok": True, **notify.flush(args.home)})
    return 0


def cmd_notify_run(args: argparse.Namespace) -> int:
    if args.interval < 1:
        raise UsageError("--interval must be at least 1 second")
    notify.load_config(args.home)  # a broken configuration fails here, before the loop starts
    Ledger(args.home).require_notifier()
    emit({"ok": True, **notify.run_forever(args.home, args.interval, args.max_passes)})
    return 0


def cmd_notify_status(args: argparse.Namespace) -> int:
    emit({"ok": True, "notifications": notify.status(args.home, task_id=args.task, event_id=args.event)})
    return 0


def cmd_notify_retry(args: argparse.Namespace) -> int:
    changed = notify.retry(args.home, args.event)
    if not changed:
        raise Refused("not_retryable", "no failed or unacknowledged notification for this event", event_id=args.event)
    emit({"ok": True, "event_id": args.event, "rescheduled": changed})
    return 0


def cmd_notify_bind_owner(args: argparse.Namespace) -> int:
    route = _check_route(args.route)
    consumer = _check_name("consumer", args.consumer)
    if len(args.chat) > 80:
        raise UsageError("--chat must be a label of at most 80 characters")
    ledger = Ledger(args.home)
    try:
        result = ledger.restore_notification_owner(args.task, route=route, consumer=consumer, chat=args.chat)
    finally:
        ledger.close()
    emit({"ok": True, **result})
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    if args.route_cmd == "add":
        adapter = {"type": "http", "timeout_s": args.timeout}
        adapter.update({key: getattr(args, key) for key in ("url_env", "url_file", "token_env", "token_file")
                        if getattr(args, key) is not None})
        result = routes.add(args.home, args.name, consumer=args.consumer, chat=args.chat,
                            adapter=adapter, source_id=args.source_id, fallback_route=args.fallback_route)
    elif args.route_cmd == "list":
        result = routes.list_routes(args.home)
    elif args.route_cmd == "remove":
        result = routes.remove(args.home, args.name)
    elif args.route_cmd == "set-default":
        result = routes.set_default(args.home, args.name)
    elif args.route_cmd == "set-fallback":
        if (args.fallback is None) == (not args.clear):
            raise UsageError("give a fallback route or --clear")
        result = routes.set_fallback(args.home, args.name, args.fallback)
    elif args.route_cmd == "confirm":
        result = routes.confirm(args.home, args.name, verification_id=args.verification_id,
                                message_id=args.message_id, source_id=args.source_id, ledger_id=args.ledger_id,
                                consumer=args.consumer, chat=args.chat)
    else:
        result = routes.verify(args.home, args.name, retry=args.retry)
        emit({"ok": result["proven"], **result})
        return 0 if result["proven"] else 6
    emit({"ok": True, **result})
    return 0


# ------------------------------------------------------------------------------- parser
class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="cli-relay", description=__doc__.splitlines()[0])
    p.add_argument("--home", type=Path, default=None, help="ledger directory (default $CLI_RELAY_HOME or ~/.cli-relay)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the ledger").set_defaults(func=cmd_init)

    s = sub.add_parser("submit", help="submit a task under an idempotency key")
    s.add_argument("--key", required=True)
    s.add_argument("--scope", default="default")
    s.add_argument("--engine", required=True, choices=engines.ENGINES)
    s.add_argument("--cwd", required=True)
    s.add_argument("--prompt")
    s.add_argument("--prompt-file")
    s.add_argument("--option", action="append", default=[], metavar="KEY=VALUE")
    s.add_argument("--allow-write-dir", action="append", metavar="DIR",
                   help="Claude: allow Edit/Write recursively in a task cwd subdirectory (repeatable)")
    s.add_argument("--allow-bash", action="append", metavar="COMMAND",
                   help="Claude: allow a literal Bash command prefix (repeatable)")
    s.add_argument("--network-access", action="store_true", default=None,
                   help="Codex: enable network access; requires --option sandbox=workspace-write")
    s.add_argument("--unset-env", action="append", metavar="NAME",
                   help="remove a billing environment variable for this task, including resume (repeatable)")
    s.add_argument("--billing-mode", choices=engines.CODEX_BILLING_MODES,
                   help="Codex: subscription (default) isolates billing env; api explicitly permits API billing "
                        "and preserves billing env except --unset-env; persisted for resume")
    s.add_argument("--notify-route", help="route in notifier.json that is woken when the task ends")
    s.add_argument("--origin", metavar='JSON', help='optional origin metadata: {"bot":"NAME","chat":"ID"}')
    s.add_argument("--after", action="append", metavar="REF", help="wait for one existing task id or key in this scope")
    s.add_argument("--after-require", choices=AFTER_REQUIRE,
                   help="dependency release condition (default: ok; requires --after)")
    s.add_argument("--queue", metavar="NAME", help="FIFO queue in this home and scope; one running task at a time")
    s.set_defaults(func=cmd_submit)

    for name, func, helptext in (("status", cmd_status, "show a task (reconciles first)"),
                                 ("cancel", cmd_cancel, "cancel the current attempt")):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("task_id", nargs="?")
        c.add_argument("--key")
        c.add_argument("--scope", default="default")
        if name == "status":
            c.add_argument("--no-reconcile", action="store_true")
        c.set_defaults(func=func)

    w = sub.add_parser("wait", help="wait for a terminal state; a timeout never stops the task")
    w.add_argument("task_id", nargs="?")
    w.add_argument("--key")
    w.add_argument("--scope", default="default")
    w.add_argument("--timeout", type=float, default=600.0)
    w.add_argument("--interval", type=float, default=1.0)
    w.set_defaults(func=cmd_wait)

    e = sub.add_parser("events", help="list events not yet acknowledged by a consumer")
    e.add_argument("--consumer", required=True)
    e.add_argument("--task")
    e.add_argument("--include-acked", action="store_true")
    e.add_argument("--no-reconcile", action="store_true")
    e.set_defaults(func=cmd_events)

    b = sub.add_parser("brief", help="read-only terminal results, message lines and a batch ACK command")
    b.add_argument("--consumer", required=True)
    b.add_argument("--task")
    b.add_argument("--route")
    b.add_argument("--text", action="store_true", help="print ready-to-send lines and the ACK command as text")
    b.set_defaults(func=cmd_brief)

    a = sub.add_parser("ack", help="acknowledge an event (idempotent)")
    a.add_argument("--consumer", required=True)
    a.add_argument("--task")
    a.add_argument("--attempt", type=int)
    a.add_argument("--event")
    a.add_argument("--events", metavar="JSON", help="atomic batch using the event references printed by brief")
    a.add_argument("--message-id", help="visible delivery message id, recorded in the ledger")
    a.add_argument("--chat", help="delivery chat label/id, recorded with the ACK")
    a.add_argument("--ledger-id", help="refuse unless the event belongs to this ledger")
    a.add_argument("--source-id", help="refuse unless this machine has this notifier source id")
    a.set_defaults(func=cmd_ack)

    r = sub.add_parser("resume", help="explicitly continue the exact native session in a new attempt")
    r.add_argument("task_id", nargs="?")
    r.add_argument("--key")
    r.add_argument("--scope", default="default")
    r.add_argument("--prompt")
    r.add_argument("--prompt-file")
    r.add_argument("--fresh", action="store_true", help="start a new native session from the original prompt")
    r.add_argument("--confirm-rerun", action="store_true")
    r.set_defaults(func=cmd_resume)

    rc = sub.add_parser("reconcile", help="settle attempts whose keeper is gone; never re-runs")
    rc.add_argument("task_id", nargs="?")
    rc.set_defaults(func=cmd_reconcile)

    d = sub.add_parser("doctor", help="check the ledger, disk space and (optionally) an engine's auth path")
    d.add_argument("--engine", choices=engines.ENGINES)
    d.add_argument("--cwd", help="task directory whose project configuration is checked (default: current)")
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("migrate", help="add the notifier tables to an existing ledger (backup first)")
    m.add_argument("--backfill", action="store_true", help="also notify events from before the migration")
    m.set_defaults(func=cmd_migrate)

    n = sub.add_parser("notify", help="wake the configured consumer when tasks end (see docs/notifier.md)")
    nsub = n.add_subparsers(dest="notify_cmd", required=True)
    nf = nsub.add_parser("flush", help="one pass: enqueue, retry, send what is due")
    nf.set_defaults(func=cmd_notify_flush)
    nr = nsub.add_parser("run", help="a pass every --interval seconds until SIGTERM (for launchd/systemd)")
    nr.add_argument("--interval", type=float, default=60.0)
    nr.add_argument("--max-passes", type=int)
    nr.set_defaults(func=cmd_notify_run)
    ns = nsub.add_parser("status", help="transport state and consumer ACK of each notification")
    ns.add_argument("--task")
    ns.add_argument("--event")
    ns.set_defaults(func=cmd_notify_status)
    nt = nsub.add_parser("retry", help="give a failed or unacknowledged notification a new budget")
    nt.add_argument("--event", required=True)
    nt.set_defaults(func=cmd_notify_retry)
    nb = nsub.add_parser("bind-owner", help="explicitly restore the original owner of a migrated task")
    nb.add_argument("--task", required=True)
    nb.add_argument("--route", required=True)
    nb.add_argument("--consumer", required=True)
    nb.add_argument("--chat", required=True)
    nb.set_defaults(func=cmd_notify_bind_owner)

    route = sub.add_parser("route", help="register and verify per-task notification routes")
    route_sub = route.add_subparsers(dest="route_cmd", required=True)
    ra = route_sub.add_parser("add", help="idempotently register an unproven HTTP route using secret references")
    ra.add_argument("name")
    ra.add_argument("--source-id", help="required when creating notifier.json; unique per machine")
    ra.add_argument("--consumer", required=True)
    ra.add_argument("--chat", required=True)
    for kind in ("url", "token"):
        reference = ra.add_mutually_exclusive_group(required=True)
        reference.add_argument(f"--{kind}-env", metavar="NAME")
        reference.add_argument(f"--{kind}-file", metavar="ABSOLUTE_PATH")
    ra.add_argument("--timeout", type=float, default=20, help="adapter timeout in seconds (1-600)")
    ra.add_argument("--fallback-route", help="one fallback reminder to an existing proven route")
    ra.set_defaults(func=cmd_route)
    route_sub.add_parser("list", help="list routes, references and verification receipts").set_defaults(func=cmd_route)
    for command, helptext in (("remove", "remove a route and clear it as default; keep task events"),
                               ("set-default", "choose a proven route as the default"),
                               ("verify", "send one due visible-message probe; return its persisted receipt")):
        rp = route_sub.add_parser(command, help=helptext)
        rp.add_argument("name")
        if command == "verify":
            rp.add_argument("--retry", action="store_true", help="explicitly grant a failed verification a new budget/id")
        rp.set_defaults(func=cmd_route)
    rf = route_sub.add_parser("set-fallback", help="choose a proven fallback route, or clear it")
    rf.add_argument("name")
    rf.add_argument("fallback", nargs="?")
    rf.add_argument("--clear", action="store_true")
    rf.set_defaults(func=cmd_route)
    confirm = route_sub.add_parser("confirm", help="confirm a visible test message for an async verification")
    confirm.add_argument("name")
    confirm.add_argument("--verification-id", required=True)
    confirm.add_argument("--message-id", required=True)
    for key in ("source-id", "ledger-id", "consumer", "chat"):
        confirm.add_argument("--" + key, help="optional assertion of the verification destination")
    confirm.set_defaults(func=cmd_route)

    k = sub.add_parser("_keeper", help="(internal) supervise one attempt")
    k.add_argument("task_id")
    k.add_argument("attempt_id")
    k.set_defaults(func=cmd_keeper)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        emit(exc.to_json())
        return exc.exit_code
    except SystemExit as exc:  # --help / --version
        return int(exc.code or 0)
    # Absolute from here on: the keeper runs with another working directory.
    args.home = Path(os.path.abspath(os.path.expanduser(str(args.home or default_home()))))
    try:
        return int(args.func(args) or 0)
    except RelayError as exc:
        emit(exc.to_json())
        return exc.exit_code
    except KeyboardInterrupt:
        emit({"ok": False, "error": "interrupted"})
        return 130
    except Exception as exc:  # pragma: no cover - last resort, still JSON
        emit({"ok": False, "error": "internal", "message": f"{type(exc).__name__}: {exc}"})
        return 1

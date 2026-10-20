"""Per-attempt keeper: claim -> preflight -> record 'spawning' -> spawn -> record pid -> wait ->
write exit.json -> commit terminal state + event (one transaction).

The keeper is the only process that ever starts the CLI for an attempt, and only after it won
the pending->claimed compare-and-set and durably recorded 'spawning'. The CLI writes its
stdout/stderr to files (not pipes), so it keeps running if the keeper dies; the CLI stays in
the keeper's process group so an orphaned CLI is still detectable.

SIGTERM or SIGUSR1 sent to the keeper means "cancel": it stops the CLI and records the outcome.

Fault-injection points (tests only): CLI_RELAY_TEST_CRASH=<point>[,<point>] makes the keeper
SIGKILL itself at that point; CLI_RELAY_TEST_PAUSE=<point>:<seconds> sleeps there.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import engines, procs
from .errors import RelayError
from .ledger import Ledger, atomic_write_text, canonical_json, now_iso
from .spawn import spawn_notifier_pass

CANCEL_GRACE_S = 10.0
POLL_S = 0.2
CANCEL_DB_POLL_S = 2.0
LOG_CAP_BYTES = 1 << 20


def _grace() -> float:
    try:
        return max(0.0, float(os.environ.get("CLI_RELAY_CANCEL_GRACE_S", CANCEL_GRACE_S)))
    except ValueError:
        return CANCEL_GRACE_S


def _signal_all(pids: List[int], sig: int) -> int:
    sent = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            sent += 1
        except (ProcessLookupError, PermissionError):
            pass
    return sent


def _hook(point: str) -> None:
    crash = [p.strip() for p in os.environ.get("CLI_RELAY_TEST_CRASH", "").split(",") if p.strip()]
    if point in crash:
        os.kill(os.getpid(), signal.SIGKILL)
    for item in os.environ.get("CLI_RELAY_TEST_PAUSE", "").split(","):
        name, _, secs = item.partition(":")
        if name.strip() == point and secs:
            time.sleep(float(secs))


class _Log:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __call__(self, msg: str) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > LOG_CAP_BYTES:
                return
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{now_iso()} {msg}\n")
        except OSError:
            pass


def run(home: Path, task_id: str, attempt_id: str) -> int:
    cancel = {"set": False}

    def _on_cancel_signal(signum: int, frame: Any) -> None:
        cancel["set"] = True

    signal.signal(signal.SIGUSR1, _on_cancel_signal)
    signal.signal(signal.SIGTERM, _on_cancel_signal)
    detached = os.environ.get("CLI_RELAY_KEEPER_DETACHED") == "1" and os.getsid(0) == os.getpgid(0)

    ledger = Ledger(home)
    att = ledger.get_attempt(attempt_id)
    if att["task_id"] != task_id:
        return 2
    me = os.getpid()
    if not ledger.claim(attempt_id, keeper_pid=me, keeper_token=procs.start_token(me),
                        keeper_pgid=os.getpgid(0), host=socket.gethostname()):
        return 0  # another keeper owns it, or it was cancelled/closed meanwhile
    _hook("after_claim")
    task = ledger.get_task(task_id)
    att = ledger.get_attempt(attempt_id)
    payload = json.loads(task["payload_json"])
    engine = task["engine"]
    run_dir = ledger.run_dir(task_id, att["attempt_no"])
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = _Log(run_dir / "keeper.log")
    env = dict(os.environ)

    def notify_pass() -> None:
        # A terminal event was committed: wake the consumer now rather than at the next periodic pass.
        if (Path(home) / "notifier.json").is_file():
            spawn_notifier_pass(home)

    def close(outcome: str, evidence: Dict[str, Any]) -> None:
        ledger.finish(attempt_id, from_states=("claimed",), outcome=outcome, evidence=evidence, source="keeper")
        notify_pass()

    try:
        ledger.check_integrity()
        ledger.check_disk()
    except RelayError as exc:
        log(f"refusing to start: {exc.code}")
        close("blocked", {"preflight": {"reason": exc.code, **exc.detail}})
        return 0
    try:
        pf = engines.preflight(engine, env, cwd=payload["cwd"], options=payload.get("options"))
    except Exception as exc:  # an unexpected failure must end the attempt, not crash-loop the keeper
        log(f"preflight failed: {exc!r}")
        close("blocked", {"preflight": {"reason": "preflight_error", "error": type(exc).__name__}})
        return 0
    if not pf.ok:
        log(f"preflight blocked: {pf.reason}")
        close("blocked", {"preflight": {"reason": pf.reason, **pf.detail}})
        return 0
    permission_checkpoint = None
    if engine == "codex":  # reconciliation must look for the thread record where this run writes it
        ledger.add_evidence(attempt_id, {"codex_home": str(engines.codex_home(env))})
        if att["resume_session_id"]:
            permission_checkpoint = engines.codex_permission_checkpoint(env, att["resume_session_id"])
            ledger.add_evidence(attempt_id, {"codex_permission_checkpoint": permission_checkpoint})
            if "offset" not in permission_checkpoint:
                close("blocked", {"permission_check": {"reason": "permissions_unverified",
                                                        "missing": "permission_checkpoint"}})
                return 0
    try:
        spec = engines.build(engine, payload=payload, attempt=att, run_dir=run_dir,
                             fixture_state_dir=str(Path(home) / "fixture-sessions"), env=env)
    except Exception as exc:
        reason = "unsafe_or_missing_cli" if isinstance(exc, RuntimeError) else "build_error"
        close("blocked", {"preflight": {"reason": reason, "error": str(exc) if isinstance(exc, RuntimeError)
                                        else type(exc).__name__}})
        return 0
    prompt_path = run_dir / "prompt.txt"
    try:
        atomic_write_text(prompt_path, att["prompt"])
    except OSError as exc:  # never start the CLI on a prompt that is not stored in full
        log(f"cannot store the prompt: {exc!r}")
        close("blocked", {"preflight": {"reason": "prompt_write_failed", "errno": errno.errorcode.get(exc.errno)}})
        return 0
    stdout_path, stderr_path = run_dir / "stdout.jsonl", run_dir / "stderr.log"

    if cancel["set"] or not ledger.mark_spawning(attempt_id, keeper_pid=me):
        close("cancelled", {"cancelled_before_spawn": True})
        return 0
    _hook("before_spawn")
    with open(prompt_path, "rb") as fin, open(stdout_path, "ab") as fout, open(stderr_path, "ab") as ferr:
        try:
            child = subprocess.Popen(spec.argv, stdin=fin, stdout=fout, stderr=ferr, cwd=spec.cwd,
                                     env=spec.env, close_fds=True)
        except OSError as exc:
            log(f"spawn failed: {exc!r}")
            close("failed", {"spawn_error": type(exc).__name__})
            return 0
    _hook("after_spawn_before_record")
    ledger.mark_running(attempt_id, keeper_pid=me, cli_pid=child.pid, cli_token=procs.start_token(child.pid))
    log(f"spawned {engine} pid={child.pid}")
    _hook("after_record")

    grace = _grace()
    my_pgid = os.getpgid(0)
    cleanup: Dict[str, Any] = {"terminated": 0, "killed": 0}

    def others_in_group() -> Optional[List[int]]:
        # A detached keeper owns its process group: keeper + CLI + CLI children that stayed in it.
        # A group id cannot be reused while the keeper is a member, so every other member is ours.
        if not detached:
            return None
        members = procs.group_members(my_pgid)
        return None if members is None else [p for p in members if p != me]

    def terminate_cli() -> None:
        # The keeper's own SIGTERM handler only sets the cancel flag, so it survives this.
        if detached:
            try:
                os.killpg(my_pgid, signal.SIGTERM)
                return
            except OSError:
                pass
        child.terminate()

    def kill_cli() -> None:
        others = others_in_group()
        if others is None:
            child.kill()
            cleanup["killed"] += 1
        else:  # every process of the attempt, not just the direct child
            cleanup["killed"] += _signal_all(others, signal.SIGKILL)

    def stop_group() -> bool:
        """Return proof that the keeper's group is empty after a cancel or guard stop."""
        others = others_in_group()
        if others is None:
            cleanup["enumeration"] = "unavailable"
            return False
        if others:
            cleanup["terminated"] += _signal_all(others, signal.SIGTERM)
            deadline = time.monotonic() + grace
            while others and time.monotonic() < deadline:
                time.sleep(0.1)
                others = others_in_group()
            if others:
                log(f"grace expired: SIGKILL to {len(others)} remaining process(es) of the group")
                cleanup["killed"] += _signal_all(others, signal.SIGKILL)
                deadline = time.monotonic() + 5.0
                while others_in_group() and time.monotonic() < deadline:
                    time.sleep(0.05)
        remaining = others_in_group()
        cleanup["remaining"] = None if remaining is None else len(remaining)
        return remaining == []

    watcher = engines.StreamWatcher(stdout_path)
    recorded_sid: Optional[str] = att["native_session_id"]
    violation: Optional[Dict[str, Any]] = None
    signalled_at: Optional[float] = None
    killed = False
    last_db_check = 0.0

    def inspect(events: List[Dict[str, Any]]) -> None:
        nonlocal recorded_sid, violation, signalled_at
        for ev in events:
            sid = engines.session_id_from_event(engine, ev)
            if sid and sid != recorded_sid:
                ledger.set_native_session(attempt_id, sid)
                recorded_sid = sid
            v = engines.billing_violation(engine, ev)
            if v and violation is None:
                violation = v
                log(f"billing guard tripped: {v}")
                terminate_cli()
                signalled_at = time.monotonic()

    while True:
        try:
            rc = child.wait(timeout=POLL_S)
            break
        except subprocess.TimeoutExpired:
            pass
        inspect(watcher.poll())
        now = time.monotonic()
        if not cancel["set"] and now - last_db_check >= CANCEL_DB_POLL_S:
            last_db_check = now
            try:
                cancel["set"] = ledger.cancel_requested(attempt_id)
            except Exception:  # a transient read error must not stop supervision
                pass
        if cancel["set"] and signalled_at is None:
            log("cancel requested: SIGTERM to the CLI process group")
            terminate_cli()
            signalled_at = now
        if signalled_at is not None and not killed and now - signalled_at > grace:
            log("grace expired: SIGKILL to the CLI and its process group")
            kill_cli()
            killed = True
    inspect(watcher.poll())
    exit_info: Dict[str, Any] = {"exit_code": rc if rc >= 0 else None, "signal": -rc if rc < 0 else None,
                                 "ended_at": now_iso(), "cli_pid": child.pid}
    if violation is not None:
        exit_info["billing_guard"] = violation  # survives a keeper that dies before the commit below
    last_message = Path(spec.last_message_path) if spec.last_message_path else None
    parsed = engines.parse_output(engine, stdout_path, last_message)
    if parsed["session_id"] and parsed["session_id"] != recorded_sid:
        ledger.set_native_session(attempt_id, parsed["session_id"])
    cancel_requested = cancel["set"] or ledger.cancel_requested(attempt_id)
    outcome, decided = engines.decide(engine, parsed, env=env, session_id=parsed["session_id"] or recorded_sid,
                                      exit_code=exit_info["exit_code"], exit_signal=exit_info["signal"],
                                      exit_known=True, cancel_requested=cancel_requested, guard=violation,
                                      sandbox=engines.requested_sandbox(engine, payload),
                                      allow_full_access=engines.full_access_enabled(engine, payload),
                                      permission_checkpoint=permission_checkpoint,
                                      resume_session_id=att["resume_session_id"])
    cleanup_stopped = True
    if signalled_at is not None or outcome in ("cancelled", "blocked"):
        cleanup_stopped = stop_group()
        exit_info["process_group_cleanup"] = cleanup
    else:
        lingering = others_in_group()
        if lingering:  # a normal exit leaves them alone, but the record says they exist
            cleanup["remaining_after_normal_exit"] = len(lingering)
    atomic_write_text(run_dir / "exit.json", canonical_json(exit_info) + "\n")
    _hook("after_exit_before_commit")

    evidence: Dict[str, Any] = {"exit_source": "keeper_waitpid", **decided}
    if "process_group_cleanup" in exit_info or "remaining_after_normal_exit" in cleanup:
        evidence["process_group_cleanup"] = cleanup
    if "process_group_cleanup" in exit_info:
        evidence["processes_stopped"] = cleanup_stopped
    if violation is not None:
        evidence["terminated"] = violation
    elif cancel_requested and outcome != "cancelled":
        evidence["cancel_lost_race"] = True
    if parsed["init"]:
        evidence["init"] = parsed["init"]
    _hook("before_finish_commit")
    if cleanup_stopped:
        ledger.finish(attempt_id, from_states=("running", "claimed", "unknown"), outcome=outcome,
                      exit_code=exit_info["exit_code"], exit_signal=exit_info["signal"], exit_known=True,
                      native=parsed["native"], evidence=evidence, source="keeper")
        log(f"finished outcome={outcome} exit={exit_info}")
    else:
        ledger.mark_unknown(attempt_id, from_states=("running", "claimed"), processes_stopped=False,
                            evidence={**evidence, "unknown_reason": "process_group_not_stopped"})
        log(f"process group stop unverified: {cleanup}")
    _hook("after_commit")
    notify_pass()
    return 0

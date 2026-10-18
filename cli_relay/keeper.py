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

CANCEL_GRACE_S = 10.0
POLL_S = 0.2
CANCEL_DB_POLL_S = 2.0
LOG_CAP_BYTES = 1 << 20


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

    def close(outcome: str, evidence: Dict[str, Any]) -> None:
        ledger.finish(attempt_id, from_states=("claimed",), outcome=outcome, evidence=evidence, source="keeper")

    try:
        ledger.check_integrity()
        ledger.check_disk()
    except RelayError as exc:
        log(f"refusing to start: {exc.code}")
        close("blocked", {"preflight": {"reason": exc.code, **exc.detail}})
        return 0
    pf = engines.preflight(engine, env, cwd=payload["cwd"])
    if not pf.ok:
        log(f"preflight blocked: {pf.reason}")
        close("blocked", {"preflight": {"reason": pf.reason, **pf.detail}})
        return 0
    if engine == "codex":  # reconciliation must look for the thread record where this run writes it
        ledger.add_evidence(attempt_id, {"codex_home": str(engines.codex_home(env))})
    try:
        spec = engines.build(engine, payload=payload, attempt=att, run_dir=run_dir,
                             fixture_state_dir=str(Path(home) / "fixture-sessions"), env=env)
    except RuntimeError as exc:
        close("blocked", {"preflight": {"reason": "unsafe_or_missing_cli", "error": str(exc)}})
        return 0
    prompt_path = run_dir / "prompt.txt"
    atomic_write_text(prompt_path, att["prompt"])
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

    def terminate_cli() -> None:
        # A detached keeper owns its process group: keeper + CLI + CLI children that stayed in it.
        # The keeper's own SIGTERM handler only sets the cancel flag, so it survives this.
        if detached:
            try:
                os.killpg(os.getpgid(0), signal.SIGTERM)
                return
            except OSError:
                pass
        child.terminate()

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
        if signalled_at is not None and not killed and now - signalled_at > CANCEL_GRACE_S:
            log("grace expired: SIGKILL to the CLI")
            child.kill()
            killed = True
    inspect(watcher.poll())

    exit_info: Dict[str, Any] = {"exit_code": rc if rc >= 0 else None, "signal": -rc if rc < 0 else None,
                                 "ended_at": now_iso(), "cli_pid": child.pid}
    if violation is not None:
        exit_info["billing_guard"] = violation  # survives a keeper that dies before the commit below
    atomic_write_text(run_dir / "exit.json", canonical_json(exit_info) + "\n")
    _hook("after_exit_before_commit")

    last_message = Path(spec.last_message_path) if spec.last_message_path else None
    parsed = engines.parse_output(engine, stdout_path, last_message)
    if parsed["session_id"] and parsed["session_id"] != recorded_sid:
        ledger.set_native_session(attempt_id, parsed["session_id"])
    cancel_requested = cancel["set"] or ledger.cancel_requested(attempt_id)
    outcome, decided = engines.decide(engine, parsed, env=env, session_id=parsed["session_id"] or recorded_sid,
                                      exit_code=exit_info["exit_code"], exit_signal=exit_info["signal"],
                                      exit_known=True, cancel_requested=cancel_requested, guard=violation)
    evidence: Dict[str, Any] = {"exit_source": "keeper_waitpid", **decided}
    if violation is not None:
        evidence["terminated"] = violation
    elif cancel_requested and outcome != "cancelled":
        evidence["cancel_lost_race"] = True
    if parsed["init"]:
        evidence["init"] = parsed["init"]
    _hook("before_finish_commit")
    ledger.finish(attempt_id, from_states=("running", "claimed", "unknown"), outcome=outcome,
                  exit_code=exit_info["exit_code"], exit_signal=exit_info["signal"], exit_known=True,
                  native=parsed["native"], evidence=evidence, source="keeper")
    log(f"finished outcome={outcome} exit={exit_info}")
    _hook("after_commit")
    return 0

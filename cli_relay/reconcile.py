"""Reconciliation: decide only what can be proven about attempts whose keeper is gone.

Never re-runs anything. The only "restart" it performs is re-spawning a keeper for an
attempt whose CLI provably never started (pending, or claimed without a 'spawning' record);
the keeper's compare-and-set claim guarantees at most one CLI per attempt.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import engines, procs
from .ledger import Ledger, iso_age_seconds, now_iso

KICK_GRACE_S = 5.0

Spawner = Callable[[str, str], None]


def _read_exit(run_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((run_dir / "exit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _evidence(att: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(att["evidence_json"]) if att.get("evidence_json") else {}
    except ValueError:
        return {}


def reconcile_attempt(ledger: Ledger, att: Dict[str, Any], *, kick: bool,
                      spawn: Optional[Spawner]) -> Dict[str, Any]:
    action: Dict[str, Any] = {"task_id": att["task_id"], "attempt_no": att["attempt_no"],
                              "attempt_id": att["attempt_id"], "state": att["state"], "action": "none"}
    state = att["state"]
    if state == "finished":
        return action
    if att.get("host") and att["host"] != socket.gethostname():
        action["action"] = "skipped_foreign_host"
        return action
    if state == "pending":
        age = iso_age_seconds(att["updated_at"])
        try:
            grace = float(os.environ.get("CLI_RELAY_KICK_GRACE_S", KICK_GRACE_S))
        except ValueError:
            grace = KICK_GRACE_S
        if kick and spawn is not None and (age is None or age >= grace):
            spawn(att["task_id"], att["attempt_id"])
            action["action"] = "kicked_keeper"
        return action

    keeper_alive = procs.same_process_alive(att["keeper_pid"], att["keeper_token"])
    action["keeper_alive"] = keeper_alive
    if keeper_alive is not False:
        return action  # the keeper owns the attempt (or we cannot prove it is gone)

    task = ledger.get_task(att["task_id"])
    engine = task["engine"]
    run_dir = ledger.run_dir(att["task_id"], att["attempt_no"])
    exit_info = _read_exit(run_dir)
    parsed = engines.parse_output(engine, run_dir / "stdout.jsonl", run_dir / "last_message.txt")
    if parsed["session_id"] and parsed["session_id"] != att["native_session_id"]:
        ledger.set_native_session(att["attempt_id"], parsed["session_id"])
    native = parsed["native"]
    cancel_requested = bool(att.get("cancel_requested_at"))

    if exit_info is not None:
        # The keeper died after recording the exit status but before committing it.
        outcome = engines.classify(native, exit_code=exit_info.get("exit_code"), exit_signal=exit_info.get("signal"),
                                   exit_known=True, cancel_requested=cancel_requested)
        eid = ledger.finish(att["attempt_id"], from_states=(state,), outcome=outcome,
                            exit_code=exit_info.get("exit_code"), exit_signal=exit_info.get("signal"),
                            exit_known=True, native=native,
                            evidence={"exit_source": "exit.json", "reconciled_at": now_iso()}, source="reconcile")
        action.update(action="finished_from_exit_record", event_id=eid)
        return action

    if state == "unknown":
        if native is not None:  # late evidence
            outcome = engines.classify(native, exit_code=None, exit_signal=None, exit_known=False,
                                       cancel_requested=cancel_requested)
            eid = ledger.finish(att["attempt_id"], from_states=("unknown",), outcome=outcome, exit_known=False,
                                native=native, evidence={"late_native_result_at": now_iso()}, source="reconcile")
            action.update(action="finished_from_late_native_result", event_id=eid)
        return action

    if state == "claimed" and not att["spawn_started_at"]:
        # 'spawning' is committed before Popen, so its absence proves the CLI never started.
        released = ledger.release_claim(att["attempt_id"], keeper_pid=att["keeper_pid"],
                                        evidence={"claim_released_at": now_iso(),
                                                  "claim_released_reason": "keeper_died_before_spawn"})
        action["action"] = "released_claim" if released else "none"
        if released and kick and spawn is not None:
            spawn(att["task_id"], att["attempt_id"])
            action["action"] = "released_claim_and_kicked"
        return action

    cli_alive = procs.same_process_alive(att["cli_pid"], att["cli_token"]) if att["cli_pid"] else None
    group_alive = procs.group_alive(att["keeper_pgid"])
    action.update(cli_alive=cli_alive, group_alive=group_alive)
    maybe_running = (cli_alive is True
                     or (cli_alive is None and group_alive is not False))
    if maybe_running:
        if "keeper_lost_at" not in _evidence(att):
            ledger.add_evidence(att["attempt_id"], {"keeper_lost_at": now_iso(), "cli_running_without_keeper": True})
        action["action"] = "observing_cli_without_keeper"
        return action

    if native is not None:
        outcome = engines.classify(native, exit_code=None, exit_signal=None, exit_known=False,
                                   cancel_requested=cancel_requested)
        eid = ledger.finish(att["attempt_id"], from_states=(state,), outcome=outcome, exit_known=False,
                            native=native,
                            evidence={"exit_source": "unavailable_keeper_lost", "reconciled_at": now_iso()},
                            source="reconcile")
        action.update(action="finished_from_native_result_exit_unknown", event_id=eid)
        return action

    if cancel_requested:
        # A cancel was requested and the run has stopped without producing a result.
        eid = ledger.finish(att["attempt_id"], from_states=(state,), outcome="cancelled", exit_known=False,
                            evidence={"exit_source": "unavailable_keeper_lost",
                                      "stopped_after_cancel_without_result": True, "reconciled_at": now_iso()},
                            source="reconcile")
        action.update(action="finished_cancelled_without_result", event_id=eid)
        return action

    try:
        stdout_bytes = (run_dir / "stdout.jsonl").stat().st_size
    except OSError:
        stdout_bytes = None
    evidence = {
        "unknown_reason": "no_exit_status_and_no_native_result",
        "keeper_alive": False,
        "cli_alive": cli_alive,
        "process_group_alive": group_alive,
        "spawn_recorded": bool(att["spawn_started_at"]),
        "cli_pid_recorded": bool(att["cli_pid"]),
        "stdout_bytes": stdout_bytes,
        "native_trace_found": engines.native_trace_exists(engine, att["native_session_id"]),
        "marked_unknown_at": now_iso(),
    }
    eid = ledger.mark_unknown(att["attempt_id"], from_states=(state,), evidence=evidence)
    action.update(action="marked_unknown", event_id=eid)
    return action


def reconcile_task(ledger: Ledger, task_id: str, *, kick: bool, spawn: Optional[Spawner]) -> List[Dict[str, Any]]:
    return [reconcile_attempt(ledger, a, kick=kick, spawn=spawn)
            for a in ledger.attempts(task_id) if a["state"] != "finished"]


def reconcile_all(ledger: Ledger, *, kick: bool, spawn: Optional[Spawner]) -> List[Dict[str, Any]]:
    return [reconcile_attempt(ledger, a, kick=kick, spawn=spawn) for a in ledger.open_attempts()]


def old_run_stopped(att: Dict[str, Any]) -> Dict[str, Any]:
    """Evidence that nothing of a closed attempt can still be running (required before resume)."""
    keeper = procs.same_process_alive(att["keeper_pid"], att["keeper_token"]) if att["keeper_pid"] else False
    cli = procs.same_process_alive(att["cli_pid"], att["cli_token"]) if att["cli_pid"] else False
    group = procs.group_alive(att["keeper_pgid"]) if att["keeper_pgid"] else False
    stopped = keeper is False and cli is False and group is False
    return {"stopped": stopped, "keeper_alive": keeper, "cli_alive": cli, "process_group_alive": group}

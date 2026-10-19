"""Independent audit 3: reconciliation decides from a snapshot. A cancel committed between that
snapshot and the closing write left the attempt `unknown` with the cancel recorded, and `unknown`
never converged. The closing transaction must see the cancel, and an attempt already stuck that
way must converge to `cancelled` once its processes are provably gone."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from helpers import run_cli

from cli_relay import procs, reconcile
from cli_relay.ledger import Ledger, now_iso


def dead_process() -> tuple:
    p = subprocess.Popen(["/bin/sleep", "30"])
    token = procs.start_token(p.pid)
    p.kill()
    p.wait()
    return p.pid, token


class CancelAndReconcileRace(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-ind03-"))
        self.home = self.tmp / "home"
        (self.tmp / "work").mkdir()
        self.assertEqual(run_cli(self.home, "init")[0], 0)
        self.ledger = Ledger(self.home)
        payload = {"engine": "fixture", "cwd": str(self.tmp / "work"), "prompt": "p", "options": {}}
        _, task, _ = self.ledger.submit(scope="default", key="k-ind03", payload=payload, prompt="p",
                                        native_session_id=str(uuid.uuid4()))
        self.task_id = task["task_id"]
        self.att = self.ledger.current_attempt(self.task_id)
        pid, token = dead_process()
        aid = self.att["attempt_id"]
        self.assertTrue(self.ledger.claim(aid, keeper_pid=pid, keeper_token=token, keeper_pgid=pid,
                                          host=socket.gethostname()))
        self.assertTrue(self.ledger.mark_spawning(aid, keeper_pid=pid))
        self.assertTrue(self.ledger.mark_running(aid, keeper_pid=pid, cli_pid=pid, cli_token=token))

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cancel_after_the_snapshot_is_seen_by_the_closing_write(self) -> None:
        snapshot = self.ledger.get_attempt(self.att["attempt_id"])  # reconcile's view: no cancel yet
        self.assertIsNone(snapshot["cancel_requested_at"])
        result, _, _ = self.ledger.request_cancel(self.task_id)  # lands before the closing write
        self.assertEqual(result, "cancel_requested")
        self.ledger.mark_unknown(self.att["attempt_id"], from_states=("running",),
                                 evidence={"unknown_reason": "no_exit_status_and_no_native_result"},
                                 processes_stopped=True)
        att = self.ledger.get_attempt(self.att["attempt_id"])
        self.assertEqual((att["state"], att["outcome"]), ("finished", "cancelled"))
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "cancelled")
        kinds = [e["kind"] for e in self.ledger.events_of_task(self.task_id)]
        self.assertEqual(kinds, ["terminal"], "one terminal event, no unknown event")

    def reconcile_with_group(self, alive):
        with patch.object(procs, "same_process_alive", return_value=False), \
             patch.object(procs, "group_alive", return_value=alive), \
             patch.object(procs.os, "killpg") as killpg:
            result = reconcile.reconcile_task(self.ledger, self.task_id, kick=False, spawn=None)
        killpg.assert_not_called()
        return result

    def test_cancel_waits_for_group_proof_without_repeating_unknown_events(self) -> None:
        self.ledger.request_cancel(self.task_id)
        for group_alive in (True, None, True):
            self.reconcile_with_group(group_alive)
            self.assertEqual(self.ledger.get_task(self.task_id)["state"], "unknown")
            self.assertEqual([e["kind"] for e in self.ledger.events_of_task(self.task_id)], ["unknown"])
        self.reconcile_with_group(False)
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "cancelled")
        self.assertEqual([e["kind"] for e in self.ledger.events_of_task(self.task_id)], ["unknown", "terminal"])

    def test_exit_record_does_not_override_a_live_group_after_cancel(self) -> None:
        self.ledger.request_cancel(self.task_id)
        run_dir = self.ledger.run_dir(self.task_id, 1)
        run_dir.mkdir(parents=True)
        (run_dir / "exit.json").write_text(json.dumps({"exit_code": None, "signal": 15,
                                                      "process_group_cleanup": {"remaining": 0}}))
        self.reconcile_with_group(True)
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "unknown")
        self.reconcile_with_group(False)
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "cancelled")
        evidence = json.loads(self.ledger.get_attempt(self.att["attempt_id"])["evidence_json"])
        self.assertEqual(evidence["process_group_cleanup"]["remaining"], 0)
        self.assertIs(evidence["processes_stopped"], True)
        self.assertIs(evidence["process_group_alive"], False)

    def test_guard_without_exit_record_finishes_only_after_group_stops(self) -> None:
        with patch.object(reconcile.engines, "decide", return_value=("blocked", {"guard": "fixture"})):
            self.reconcile_with_group(True)
            self.assertEqual(self.ledger.get_task(self.task_id)["state"], "unknown")
            self.reconcile_with_group(False)
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "blocked")

    def test_unverifiable_cli_identity_is_not_stopped_even_with_empty_group(self) -> None:
        self.ledger.request_cancel(self.task_id)
        att = self.ledger.get_attempt(self.att["attempt_id"])
        with patch.object(procs, "same_process_alive", side_effect=[False, None]), \
             patch.object(procs, "group_alive", return_value=False):
            reconcile.reconcile_attempt(self.ledger, att, kick=False, spawn=None)
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "unknown")

    def test_an_attempt_already_stuck_in_unknown_with_a_cancel_converges(self) -> None:
        # State left behind by the race in a ledger written by an earlier version.
        conn = sqlite3.connect(str(self.home / "ledger.db"))
        conn.execute("UPDATE attempts SET state='unknown', cancel_requested_at=? WHERE attempt_id=?",
                     (now_iso(), self.att["attempt_id"]))
        conn.execute("UPDATE tasks SET state='unknown' WHERE task_id=?", (self.task_id,))
        conn.commit()
        conn.close()
        reconcile.reconcile_task(self.ledger, self.task_id, kick=False, spawn=None)
        att = self.ledger.get_attempt(self.att["attempt_id"])
        self.assertEqual((att["state"], att["outcome"]), ("finished", "cancelled"))
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()

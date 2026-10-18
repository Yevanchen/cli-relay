"""Audit #1: cancel must never signal a process group it cannot attribute to the attempt."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time
import unittest

from helpers import RelayTestCase, base_env, wait_until

from cli_relay import procs


class CancelProcessGroupSafety(RelayTestCase):
    def dead_keeper_row(self, task_id: str):
        return wait_until(lambda: (lambda r: r if r["keeper_pid"] and procs.same_process_alive(
            r["keeper_pid"], r["keeper_token"]) is False else None)(self.attempt_row(task_id)))

    def test_cancel_does_not_signal_a_recycled_process_group(self) -> None:
        sub = self.submit("k-a01", duration=0.2, env=base_env({"CLI_RELAY_TEST_CRASH": "after_claim"}))
        row = self.dead_keeper_row(sub["task_id"])
        # The recorded pgid now belongs to an unrelated, live process group (pgid reuse).
        stranger = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        try:
            conn = sqlite3.connect(str(self.home / "ledger.db"))
            conn.execute("UPDATE attempts SET keeper_pgid=? WHERE attempt_id=?", (stranger.pid, row["attempt_id"]))
            conn.commit()
            conn.close()
            out = self.cli("cancel", sub["task_id"])
            time.sleep(0.5)
            self.assertIsNone(stranger.poll(), "an unrelated process group must never be signalled")
            self.assertEqual(out["signalled"], "none")
        finally:
            stranger.kill()
            stranger.wait(timeout=10)

    def test_cancel_still_stops_a_verified_orphan_cli(self) -> None:
        sub = self.submit("k-a01b", duration=30, env=base_env({"CLI_RELAY_TEST_CRASH": "after_record"}))
        row = self.dead_keeper_row(sub["task_id"])
        self.assertTrue(procs.same_process_alive(row["cli_pid"], row["cli_token"]))
        out = self.cli("cancel", sub["task_id"])
        self.assertNotEqual(out["signalled"], "none")
        wait_until(lambda: procs.same_process_alive(row["cli_pid"], row["cli_token"]) is False, timeout=15)
        done = wait_until(lambda: (lambda s: s if s["task"]["state"] not in ("accepted", "running") else None)(
            self.status(sub["task_id"])), timeout=30, interval=0.3)
        self.assertEqual(done["task"]["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()

"""Audit #12: a cancel that lands just before the terminal commit is recorded as a lost race."""

from __future__ import annotations

import unittest

from helpers import RelayTestCase, base_env, wait_until


class CancelLostRaceIsRecorded(RelayTestCase):
    def test_cancel_between_outcome_decision_and_commit(self) -> None:
        env = base_env({"CLI_RELAY_TEST_PAUSE": "before_finish_commit:3"})
        sub = self.submit("k-a12", duration=0.1, env=env)
        # Wait until the CLI has exited (exit.json written): the keeper is now paused before its commit.
        run_dir = self.home / "runs" / sub["task_id"] / "1"
        wait_until(lambda: (run_dir / "exit.json").exists(), timeout=20)
        res = self.cli("cancel", sub["task_id"])
        self.assertEqual(res["result"], "cancel_requested")
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        att = done["attempts"][0]
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertIsNotNone(att["cancel_requested_at"])
        self.assertTrue((att["evidence"] or {}).get("cancel_lost_race"),
                        "a cancel that arrived before the commit must be recorded as lost")


if __name__ == "__main__":
    unittest.main()

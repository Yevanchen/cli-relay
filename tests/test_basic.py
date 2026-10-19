"""Round trip: submit -> keeper -> fixture CLI -> terminal event -> events -> ack."""

from __future__ import annotations

import unittest

from helpers import REPO, RelayTestCase, base_env, run_cli, side_effects


class BasicRoundTrip(RelayTestCase):
    def test_submit_wait_events_ack(self) -> None:
        sub = self.submit("k-basic", duration=0.3)
        self.assertTrue(sub["accepted"])
        self.assertEqual(sub["status"], "created")
        self.assertEqual(sub["keeper"], "spawned")
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertFalse(done["timed_out"])
        self.assertEqual(done["task"]["state"], "succeeded")
        att = done["attempts"][0]
        self.assertEqual(att["exit"], {"code": 0, "signal": None, "known": True})
        self.assertEqual(att["native"]["result"], "ok")
        self.assertEqual(att["native_session_id"], sub["native_session_id"])
        self.assertTrue(att["host"])
        self.assertEqual(att["cwd"], str(self.work.resolve()))
        self.assertEqual(len(side_effects(self.effects)), 1)

        ev = self.cli("events", "--consumer", "grok")["events"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "terminal")
        self.assertEqual(ev[0]["body"]["outcome"], "succeeded")
        ack = self.cli("ack", "--consumer", "grok", "--task", sub["task_id"], "--attempt", "1",
                       "--event", ev[0]["event_id"])
        self.assertFalse(ack["duplicate"])
        self.assertEqual(self.cli("events", "--consumer", "grok")["events"], [])

    def test_second_entry_point_with_minimal_environment(self) -> None:
        sub = self.submit("k-entry", duration=0.2)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        # A different process with a bare environment (as an SSH command would have) sees the same task.
        bare = {"PATH": "/usr/bin:/bin", "HOME": base_env()["HOME"], "PYTHONPATH": str(REPO)}
        code, out = run_cli(self.home, "status", "--key", "k-entry", env=bare)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["task"]["task_id"], sub["task_id"])
        self.assertEqual(out["task"]["state"], "succeeded")
        self.assertEqual(out["events"][0]["kind"], "terminal")

    def test_wait_timeout_does_not_stop_the_task(self) -> None:
        sub = self.submit("k-wait", duration=2.5)
        waited = self.cli("wait", sub["task_id"], "--timeout", "0.5", "--interval", "0.1")
        self.assertTrue(waited["timed_out"])
        self.assertIn(waited["task"]["state"], ("accepted", "running"))
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_usage_errors_are_json(self) -> None:
        code, out = run_cli(self.home, "submit", "--key", "x")
        self.assertEqual(code, 2)
        self.assertEqual(out["error"], "usage")
        code, out = run_cli(self.home, "status", "t-does-not-exist")
        self.assertEqual(code, 4)
        self.assertEqual(out["error"], "not_found")


if __name__ == "__main__":
    unittest.main()

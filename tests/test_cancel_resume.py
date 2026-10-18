"""Cancel/complete races, explicit resume of the exact native session, stale writers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import uuid

from helpers import RelayTestCase, base_env, db_rows, side_effects, wait_until

from cli_relay.ledger import Ledger


class CancelAndResume(RelayTestCase):
    def test_cancel_running_task(self) -> None:
        sub = self.submit("k-cancel", duration=30)
        wait_until(lambda: self.attempt_row(sub["task_id"])["cli_pid"])
        res = self.cli("cancel", sub["task_id"])
        self.assertEqual((res["result"], res["signalled"]), ("cancel_requested", "keeper"))
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "cancelled")
        att = done["attempts"][0]
        self.assertTrue(att["exit"]["known"])
        self.assertEqual(att["exit"]["signal"], 15)
        self.assertEqual(len(side_effects(self.effects)), 0)

    def test_cancel_pending_attempt_before_any_keeper(self) -> None:
        ledger = Ledger(self.home)
        payload = {"engine": "fixture", "cwd": str(self.work.resolve()), "prompt": "p",
                   "options": {"duration_s": 0.2, "side_effect_file": str(self.effects)}}
        _, task, _ = ledger.submit(scope="default", key="k-pending", payload=payload, prompt="p",
                                   native_session_id=str(uuid.uuid4()))
        res = self.cli("cancel", task["task_id"])
        self.assertEqual(res["result"], "cancelled_before_start")
        again = self.submit("k-pending", prompt="p", duration=0.2)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["state"], "cancelled")
        self.assertEqual(len(side_effects(self.effects)), 0)

    def test_cancel_after_finish_is_refused(self) -> None:
        sub = self.submit("k-cancel-late", duration=0.1)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        out = self.cli("cancel", sub["task_id"], expect=6)
        self.assertEqual(out["error"], "not_cancellable")

    def test_cancel_racing_completion_has_one_consistent_outcome(self) -> None:
        for i in range(5):
            sub = self.submit(f"k-race-{i}", duration=0.0)
            self.cli("cancel", sub["task_id"], expect=None)
            done = self.cli("wait", sub["task_id"], "--timeout", "30")
            state = done["task"]["state"]
            self.assertIn(state, ("cancelled", "succeeded"))
            terminal = [e for e in done["events"] if e["kind"] == "terminal"]
            self.assertEqual(len(terminal), 1)
            if state == "succeeded" and done["attempts"][0]["cancel_requested_at"]:
                self.assertTrue(done["attempts"][0]["evidence"].get("cancel_lost_race"))
        self.assertLessEqual(len(side_effects(self.effects)), 5)

    def test_resume_continues_the_exact_native_session(self) -> None:
        sub = self.submit("k-resume", prompt="first prompt", duration=0.1)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        res = self.cli("resume", sub["task_id"], "--prompt", "second prompt")
        self.assertEqual(res["reason"], "resume")
        self.assertEqual(res["resume_session_id"], sub["native_session_id"])
        self.assertTrue(res["previous_attempt_stopped"]["stopped"])
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        a1, a2 = done["attempts"]
        self.assertEqual(a2["resume_session_id"], a1["native_session_id"])
        self.assertEqual(a2["native"]["session_id"], a1["native_session_id"])
        self.assertIn("previous prompt: first prompt", a2["native"]["result"])
        self.assertEqual(len(side_effects(self.effects)), 2)
        events = self.cli("events", "--consumer", "c")["events"]
        self.assertEqual([(e["attempt_no"], e["superseded"]) for e in events], [(1, True), (2, False)])

    def test_resume_refused_while_attempt_active(self) -> None:
        sub = self.submit("k-resume-active", duration=30)
        out = self.cli("resume", sub["task_id"], "--prompt", "x", expect=6)
        self.assertEqual(out["error"], "attempt_active")
        self.cli("cancel", sub["task_id"])
        self.cli("wait", sub["task_id"], "--timeout", "30")

    def test_concurrent_resumes_create_one_attempt(self) -> None:
        sub = self.submit("k-resume-race", duration=0.1)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        args = [sys.executable, "-m", "cli_relay", "--home", str(self.home), "resume", sub["task_id"],
                "--prompt", "again"]
        ps = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=base_env())
              for _ in range(4)]
        outs = [json.loads(p.communicate(timeout=60)[0]) for p in ps]
        ok = [o for o in outs if o["ok"]]
        self.assertEqual(len(ok), 1, outs)
        self.assertTrue(all(o["error"] in ("concurrent_resume", "attempt_active") for o in outs if not o["ok"]))
        self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM attempts")), 2)

    def test_stale_writers_cannot_change_a_closed_or_superseded_attempt(self) -> None:
        sub = self.submit("k-stale", duration=0.1)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        self.cli("resume", sub["task_id"], "--prompt", "two")
        self.cli("wait", sub["task_id"], "--timeout", "30")
        ledger = Ledger(self.home)
        old = ledger.attempts(sub["task_id"])[0]
        self.assertFalse(ledger.claim(old["attempt_id"], keeper_pid=os.getpid(), keeper_token=None,
                                      keeper_pgid=os.getpgid(0), host="x"))
        self.assertFalse(ledger.mark_running(old["attempt_id"], keeper_pid=old["keeper_pid"], cli_pid=1,
                                             cli_token=None))
        self.assertIsNone(ledger.finish(old["attempt_id"], from_states=("running", "claimed"), outcome="failed"))
        self.assertEqual(ledger.get_task(sub["task_id"])["state"], "succeeded")
        self.assertEqual(ledger.get_task(sub["task_id"])["current_attempt"], 2)


if __name__ == "__main__":
    unittest.main()

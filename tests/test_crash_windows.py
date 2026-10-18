"""Responsibility 2: after a crash, reconcile the same task; never re-run on missing evidence.

Every crash here is a real SIGKILL of a real OS process (the keeper kills itself at an
injected point, or a test kills a recorded pid after verifying its start time).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest

from helpers import RelayTestCase, base_env, db_rows, side_effects, wait_until

from cli_relay import procs


class CrashWindows(RelayTestCase):
    def crash_env(self, point: str) -> dict:
        return base_env({"CLI_RELAY_TEST_CRASH": point})

    def keeper_dead(self, task_id: str, attempt_no: int = 1) -> bool:
        row = self.attempt_row(task_id, attempt_no)
        return bool(row["keeper_pid"]) and procs.same_process_alive(row["keeper_pid"], row["keeper_token"]) is False

    def test_submit_crash_after_commit_before_keeper(self) -> None:
        # The submit process dies after COMMIT and before starting a keeper: the caller sees no reply.
        args = [sys.executable, "-m", "cli_relay", "--home", str(self.home), "submit", "--key", "k-w1",
                "--engine", "fixture", "--cwd", str(self.work), "--prompt", "p", "--option", "duration_s=0.2",
                "--option", f"side_effect_file={json.dumps(str(self.effects))}"]
        res = subprocess.run(args, capture_output=True, text=True,
                             env=self.crash_env("submit_after_commit"))
        self.assertEqual(res.returncode, -signal.SIGKILL)
        self.assertEqual(res.stdout, "")
        row = db_rows(self.home, "SELECT state FROM attempts")[0]
        self.assertEqual(row["state"], "pending", "nothing ran yet")
        # The caller retries with the same key: the pending attempt gets a keeper, the work runs once.
        again = subprocess.run(args, capture_output=True, text=True,
                               env=base_env({"CLI_RELAY_KICK_GRACE_S": "0"}))
        out = json.loads(again.stdout)
        self.assertEqual(out["status"], "duplicate")
        self.assertEqual(out["keeper"], "kicked")
        done = self.cli("wait", out["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_keeper_crash_after_claim_is_released_and_restarted(self) -> None:
        sub = self.submit("k-w2", duration=0.2, env=self.crash_env("after_claim"))
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        self.assertIsNone(self.attempt_row(sub["task_id"])["spawn_started_at"])
        rec = self.cli("reconcile", sub["task_id"])
        self.assertEqual(rec["actions"][0]["action"], "released_claim_and_kicked")
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(len(side_effects(self.effects)), 1)
        self.assertEqual(len(done["attempts"]), 1, "same attempt, no new one")

    def test_keeper_crash_before_spawn_is_unknown_and_not_rerun(self) -> None:
        sub = self.submit("k-w3", duration=0.2, env=self.crash_env("before_spawn"))
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        st = self.status(sub["task_id"])
        self.assertEqual(st["task"]["state"], "unknown")
        self.assertEqual(st["reconcile"][0]["action"], "marked_unknown")
        ev = st["attempts"][0]["evidence"]
        self.assertTrue(ev["spawn_recorded"])
        self.assertFalse(ev["cli_pid_recorded"])
        time.sleep(1.0)
        self.status(sub["task_id"])
        self.assertEqual(len(side_effects(self.effects)), 0, "unknown must not trigger a re-run")
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM attempts")), 1)
        kinds = [e["kind"] for e in self.cli("events", "--consumer", "c")["events"]]
        self.assertEqual(kinds, ["unknown"])
        # Exact resume is impossible (the native session never existed) and is reported as such.
        res = self.cli("resume", sub["task_id"], "--prompt", "continue")
        self.assertEqual(res["reason"], "resume")
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "failed")
        self.assertIn("No conversation found", done["attempts"][1]["native"]["result"])
        # Only an explicit, confirmed re-run starts the work again.
        refused = self.cli("resume", sub["task_id"], "--fresh", expect=6)
        self.assertEqual(refused["error"], "rerun_not_confirmed")
        self.cli("resume", sub["task_id"], "--fresh", "--confirm-rerun")
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual([a["reason"] for a in done["attempts"]], ["initial", "resume", "rerun"])
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_keeper_crash_after_spawn_before_pid_record(self) -> None:
        sub = self.submit("k-w4", duration=2.0, env=self.crash_env("after_spawn_before_record"))
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        st = self.status(sub["task_id"])
        self.assertEqual(st["reconcile"][0]["action"], "observing_cli_without_keeper")
        self.assertEqual(st["task"]["state"], "running", "an orphaned CLI is still running: do not judge")
        self.assertIsNone(st["attempts"][0]["cli"]["pid"])
        refused = self.cli("resume", sub["task_id"], "--prompt", "x", expect=6)
        self.assertEqual(refused["error"], "attempt_active")
        done = wait_until(lambda: (lambda s: s if s["task"]["state"] == "succeeded" else None)(
            self.status(sub["task_id"])), timeout=30, interval=0.3)
        att = done["attempts"][0]
        self.assertFalse(att["exit"]["known"], "exit status is unknowable without the keeper")
        self.assertTrue(att["native"]["ok"])
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_keeper_killed_while_cli_keeps_running(self) -> None:
        sub = self.submit("k-w5", duration=2.5)
        row = wait_until(lambda: (lambda r: r if r["cli_pid"] else None)(self.attempt_row(sub["task_id"])))
        self.assertTrue(procs.same_process_alive(row["keeper_pid"], row["keeper_token"]))
        os.kill(row["keeper_pid"], signal.SIGKILL)
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        self.assertTrue(procs.same_process_alive(row["cli_pid"], row["cli_token"]), "the CLI survives its keeper")
        st = self.status(sub["task_id"])
        self.assertEqual(st["reconcile"][0]["action"], "observing_cli_without_keeper")
        done = wait_until(lambda: (lambda s: s if s["task"]["state"] == "succeeded" else None)(
            self.status(sub["task_id"])), timeout=30, interval=0.3)
        self.assertFalse(done["attempts"][0]["exit"]["known"])
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_keeper_crash_after_exit_before_commit(self) -> None:
        sub = self.submit("k-w6", duration=0.2, env=self.crash_env("after_exit_before_commit"))
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        st = self.status(sub["task_id"])
        self.assertEqual(st["reconcile"][0]["action"], "finished_from_exit_record")
        self.assertEqual(st["task"]["state"], "succeeded")
        self.assertEqual(st["attempts"][0]["exit"], {"code": 0, "signal": None, "known": True})
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_keeper_crash_after_commit_is_harmless(self) -> None:
        sub = self.submit("k-w7", duration=0.2, env=self.crash_env("after_commit"))
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        st = self.status(sub["task_id"])
        self.assertEqual(st["task"]["state"], "succeeded")
        self.assertEqual([e["kind"] for e in st["events"]], ["terminal"])
        self.assertEqual(st["reconcile"], [])

    def test_cli_killed_is_recorded_as_failed_not_rerun(self) -> None:
        sub = self.submit("k-w8", duration=5.0)
        row = wait_until(lambda: (lambda r: r if r["cli_pid"] else None)(self.attempt_row(sub["task_id"])))
        self.assertTrue(procs.same_process_alive(row["cli_pid"], row["cli_token"]))
        os.kill(row["cli_pid"], signal.SIGKILL)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        att = done["attempts"][0]
        self.assertEqual(done["task"]["state"], "failed")
        self.assertEqual(att["exit"], {"code": None, "signal": 9, "known": True})
        self.assertIsNone(att["native"])
        self.assertEqual(len(done["attempts"]), 1)
        self.assertEqual(len(side_effects(self.effects)), 0)


if __name__ == "__main__":
    unittest.main()

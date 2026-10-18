"""Caller disconnects (SSH session teardown, killed waiter) and PID reuse."""

from __future__ import annotations

import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import unittest

from helpers import REPO, RelayTestCase, base_env, side_effects, wait_until

from cli_relay import procs


class Disconnect(RelayTestCase):
    def test_caller_session_teardown_does_not_stop_the_task(self) -> None:
        # A shell in its own session stands in for an SSH login: it submits, then the whole session dies.
        submit = [sys.executable, "-m", "cli_relay", "--home", str(self.home), "submit", "--key", "k-ssh",
                  "--engine", "fixture", "--cwd", str(self.work), "--prompt", "p", "--option", "duration_s=2",
                  "--option", f"side_effect_file={json.dumps(str(self.effects))}"]
        script = " ".join(shlex.quote(a) for a in submit) + "; sleep 60"
        shell = subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.PIPE, text=True, env=base_env(),
                                 start_new_session=True)
        out = json.loads(shell.stdout.readline())
        self.assertTrue(out["accepted"])
        os.killpg(shell.pid, signal.SIGHUP)
        time.sleep(0.2)
        try:
            os.killpg(shell.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # macOS reports EPERM for a group that only holds the zombie leader
        shell.wait(timeout=10)
        shell.stdout.close()
        bare = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"), "PYTHONPATH": str(REPO)}
        done = self.cli("wait", out["task_id"], "--timeout", "30", env=bare)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertTrue(done["attempts"][0]["exit"]["known"], "the keeper survived the caller")
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_killed_waiter_does_not_stop_the_task(self) -> None:
        sub = self.submit("k-waiter", duration=2)
        waiter = subprocess.Popen([sys.executable, "-m", "cli_relay", "--home", str(self.home), "wait",
                                   sub["task_id"], "--timeout", "60"], stdout=subprocess.DEVNULL, env=base_env())
        time.sleep(0.5)
        waiter.kill()
        waiter.wait(timeout=10)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_recycled_pid_is_not_mistaken_for_the_keeper(self) -> None:
        sub = self.submit("k-pid-reuse", duration=0.2, env=base_env({"CLI_RELAY_TEST_CRASH": "after_claim"}))
        row = wait_until(lambda: (lambda r: r if r["keeper_pid"] and procs.same_process_alive(
            r["keeper_pid"], r["keeper_token"]) is False else None)(self.attempt_row(sub["task_id"])))
        # An unrelated live process now "owns" the recorded pid (simulated by pointing the record at it).
        stranger = subprocess.Popen(["/bin/sleep", "60"])
        try:
            conn = sqlite3.connect(str(self.home / "ledger.db"))
            conn.execute("UPDATE attempts SET keeper_pid=? WHERE attempt_id=?", (stranger.pid, row["attempt_id"]))
            conn.commit()
            conn.close()
            self.assertIs(procs.same_process_alive(stranger.pid, row["keeper_token"]), False)
            rec = self.cli("reconcile", sub["task_id"])
            self.assertEqual(rec["actions"][0]["action"], "released_claim_and_kicked")
            done = self.cli("wait", sub["task_id"], "--timeout", "30")
            self.assertEqual(done["task"]["state"], "succeeded")
            self.assertEqual(len(side_effects(self.effects)), 1)
            self.assertIsNone(stranger.poll(), "the unrelated process must not be touched")
        finally:
            stranger.kill()
            stranger.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()

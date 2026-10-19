"""Independent audit 4: a cancelled attempt must not be recorded as finished while a tool process
the CLI left in the same process group is still running. The keeper stops the whole group (except
itself) and only commits once the group is verified empty; what it did is in the evidence."""

from __future__ import annotations

import os
import signal
import subprocess
import unittest

from helpers import RelayTestCase, base_env, wait_until

from cli_relay import procs


class GroupMembers(unittest.TestCase):
    def test_lists_live_members_of_a_group(self) -> None:
        mine = procs.group_members(os.getpgid(0))
        self.assertIsNotNone(mine)
        self.assertIn(os.getpid(), mine)
        other = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        try:
            self.assertEqual(wait_until(lambda: procs.group_members(other.pid), timeout=5), [other.pid])
            self.assertNotIn(other.pid, procs.group_members(os.getpgid(0)))
        finally:
            other.kill()
            other.wait()
        self.assertEqual(procs.group_members(other.pid), [], "a dead (reaped) process is not a member")


class CancelStopsTheWholeProcessGroup(RelayTestCase):
    def descendant(self):
        pid_file = self.tmp / "descendant.pid"
        pid = wait_until(lambda: int(pid_file.read_text().split()[0]) if pid_file.exists() and pid_file.read_text().strip()
                         else None, timeout=20)
        return pid, procs.start_token(pid)

    def test_a_term_ignoring_descendant_is_stopped_before_the_commit(self) -> None:
        env = base_env({"CLI_RELAY_CANCEL_GRACE_S": "1"})
        sub = self.submit("k-ind04", duration=30, env=env, extra_options=[
            "descendant_s=60", f'descendant_pid_file="{self.tmp / "descendant.pid"}"'])
        pid, token = self.descendant()
        try:
            self.assertTrue(procs.same_process_alive(pid, token))
            wait_until(lambda: self.attempt_row(sub["task_id"])["state"] == "running", timeout=20)
            self.assertEqual(self.cli("cancel", sub["task_id"])["result"], "cancel_requested")
            done = wait_until(lambda: (lambda s: s if s["task"]["state"] not in ("accepted", "running") else None)(
                self.status(sub["task_id"])), timeout=60, interval=0.3)
            self.assertEqual(done["task"]["state"], "cancelled")
            self.assertIs(procs.same_process_alive(pid, token), False,
                          "the terminal state was committed while a process of the attempt still ran")
            cleanup = done["attempts"][0]["evidence"]["process_group_cleanup"]
            self.assertEqual(cleanup["remaining"], 0)
            self.assertGreaterEqual(cleanup["killed"], 1)
        finally:
            if procs.same_process_alive(pid, token):
                os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()

"""Audit #5: with default settings, polling `status` eventually starts a provably-unstarted attempt."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
import unittest

from helpers import RelayTestCase, base_env, side_effects


class PendingAttemptIsRecoveredByPolling(RelayTestCase):
    def test_resubmit_within_grace_then_only_status_polling(self) -> None:
        args = [sys.executable, "-m", "cli_relay", "--home", str(self.home), "submit", "--key", "k-a05",
                "--engine", "fixture", "--cwd", str(self.work), "--prompt", "p", "--option", "duration_s=0.2",
                "--option", f"side_effect_file={json.dumps(str(self.effects))}"]
        crashed = subprocess.run(args, capture_output=True, text=True,
                                 env=base_env({"CLI_RELAY_TEST_CRASH": "submit_after_commit"}))
        self.assertEqual(crashed.returncode, -signal.SIGKILL)
        # Retry at once (inside the default 5 s grace): the reply is a duplicate, no keeper yet.
        again = json.loads(subprocess.run(args, capture_output=True, text=True, env=base_env()).stdout)
        self.assertEqual(again["status"], "duplicate")
        # From now on the caller only polls status, as the README tells it to.
        deadline = time.monotonic() + 20
        state = None
        while time.monotonic() < deadline:
            state = self.status(again["task_id"])["task"]["state"]
            if state not in ("accepted", "running"):
                break
            time.sleep(0.5)
        self.assertEqual(state, "succeeded", "a task that provably never started must not stay accepted")
        self.assertEqual(len(side_effects(self.effects)), 1)


if __name__ == "__main__":
    unittest.main()

"""Audit #6: a relative --home (or CLI_RELAY_HOME) must reach the keeper as the same ledger."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from helpers import RelayTestCase, base_env, side_effects


class RelativeHome(RelayTestCase):
    def run_in(self, cwd, *args, env=None):
        p = subprocess.run([sys.executable, "-m", "cli_relay", *args], capture_output=True, text=True,
                           cwd=str(cwd), env=env or base_env(), timeout=60)
        return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])

    def test_relative_home_flag(self) -> None:
        code, out = self.run_in(self.tmp, "--home", "rel-ledger", "init")
        self.assertEqual(code, 0, out)
        code, sub = self.run_in(self.tmp, "--home", "rel-ledger", "submit", "--key", "k-a06", "--engine", "fixture",
                                "--cwd", str(self.work), "--prompt", "p", "--option", "duration_s=0.2",
                                "--option", f"side_effect_file={json.dumps(str(self.effects))}")
        self.assertEqual(code, 0, sub)
        code, done = self.run_in(self.tmp, "--home", "rel-ledger", "wait", sub["task_id"], "--timeout", "15")
        self.assertEqual(done["task"]["state"], "succeeded", "the keeper must find the same ledger")
        self.assertEqual(len(side_effects(self.effects)), 1)

    def test_relative_home_environment_variable(self) -> None:
        env = base_env({"CLI_RELAY_HOME": "env-ledger"})
        self.assertEqual(self.run_in(self.tmp, "init", env=env)[0], 0)
        code, sub = self.run_in(self.tmp, "submit", "--key", "k-a06e", "--engine", "fixture", "--cwd", str(self.work),
                                "--prompt", "p", "--option", "duration_s=0.2", env=env)
        self.assertEqual(code, 0, sub)
        code, done = self.run_in(self.tmp, "wait", sub["task_id"], "--timeout", "15", env=env)
        self.assertEqual(done["task"]["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()

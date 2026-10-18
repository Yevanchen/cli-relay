"""Audit #14/#15: the real-CLI acceptance script checks exactly its own events and fails loudly.

The script's fixture mode runs the same five steps without a model, so its logic is tested in CI.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from helpers import REPO, RelayTestCase, base_env


class RealCliScript(RelayTestCase):
    def run_script(self, *extra: str):
        out = self.tmp / "result.json"
        p = subprocess.run([sys.executable, str(REPO / "scripts" / "real_cli_acceptance.py"), "--engine", "fixture",
                            "--home", str(self.home), "--workdir", str(self.work), "--out", str(out), *extra],
                           capture_output=True, text=True, env=base_env(), timeout=300)
        return p.returncode, json.loads(out.read_text())

    def test_passes_and_acks_only_its_own_events(self) -> None:
        # An unrelated task with an unacknowledged event already sits in the same ledger.
        other = self.submit("k-unrelated", duration=0.1)
        self.cli("wait", other["task_id"], "--timeout", "30")
        code, report = self.run_script()
        self.assertEqual((code, report["result"]), (0, "PASS"), report)
        acked = report["steps"][-1]["events"]
        self.assertEqual(sorted((e["attempt_no"], e["outcome"]) for e in acked),
                         [(1, "failed"), (1, "succeeded"), (2, "succeeded")])
        left = self.cli("events", "--consumer", "acceptance")["events"]
        self.assertEqual([e["task_id"] for e in left], [other["task_id"]],
                         "events outside this run must stay unacknowledged")

    def test_a_failed_step_gives_a_nonzero_exit_code(self) -> None:
        code, report = self.run_script("--sabotage-resume")
        self.assertEqual(report["result"], "FAIL")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

"""Responsibility 1: a repeated request key never blindly re-runs."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from helpers import RelayTestCase, base_env, db_rows, side_effects


class Idempotency(RelayTestCase):
    def test_duplicate_submit_returns_same_task_and_runs_once(self) -> None:
        first = self.submit("k-dup", duration=0.5)
        second = self.submit("k-dup", duration=0.5)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(second["submit_count"], 2)
        self.cli("wait", first["task_id"], "--timeout", "30")
        third = self.submit("k-dup", duration=0.5)
        self.assertEqual(third["task_id"], first["task_id"])
        self.assertEqual(third["state"], "succeeded")
        self.assertEqual(len(side_effects(self.effects)), 1)
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM attempts")), 1)

    def test_concurrent_duplicate_submits_converge_on_one_execution(self) -> None:
        args = [sys.executable, "-m", "cli_relay", "--home", str(self.home), "submit", "--key", "k-race",
                "--engine", "fixture", "--cwd", str(self.work), "--prompt", "same payload",
                "--option", "duration_s=0.5", "--option", f"side_effect_file={json.dumps(str(self.effects))}"]
        procs = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=base_env())
                 for _ in range(8)]
        outs = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, stderr)
            outs.append(json.loads(stdout))
        task_ids = {o["task_id"] for o in outs}
        self.assertEqual(len(task_ids), 1)
        self.assertEqual(sorted(o["status"] for o in outs).count("created"), 1)
        task_id = task_ids.pop()
        self.cli("wait", task_id, "--timeout", "30")
        self.assertEqual(len(side_effects(self.effects)), 1, "the work must run exactly once")
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM attempts")), 1)
        self.assertEqual(db_rows(self.home, "SELECT submit_count FROM tasks")[0][0], 8)
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM submissions")), 8)

    def test_same_key_with_different_payload_is_a_conflict(self) -> None:
        first = self.submit("k-conflict", prompt="original", duration=0.2)
        out = self.submit("k-conflict", prompt="something else", duration=0.2, expect=3)
        self.assertEqual(out["error"], "conflict")
        self.assertEqual(out["detail"]["task_id"], first["task_id"])
        self.assertNotEqual(out["detail"]["existing_payload_sha256"], out["detail"]["submitted_payload_sha256"])
        self.assertNotIn("something else", json.dumps(out), "payload content must not be echoed")
        self.cli("wait", first["task_id"], "--timeout", "30")
        self.assertEqual(len(side_effects(self.effects)), 1)
        results = [r["result"] for r in db_rows(self.home, "SELECT result FROM submissions ORDER BY id")]
        self.assertEqual(results, ["created", "conflict"])

    def test_cwd_and_options_are_part_of_the_payload(self) -> None:
        self.submit("k-opts", duration=0.2)
        out = self.submit("k-opts", duration=0.3, expect=3)  # different option value
        self.assertEqual(out["error"], "conflict")

    def test_scope_isolates_keys(self) -> None:
        a = self.submit("k-shared", scope="caller-a", duration=0.2)
        b = self.submit("k-shared", scope="caller-b", duration=0.2)
        self.assertNotEqual(a["task_id"], b["task_id"])
        for t in (a, b):
            self.cli("wait", t["task_id"], "--timeout", "30")
        self.assertEqual(len(side_effects(self.effects)), 2)

    def test_lost_submit_response_is_recovered_by_resubmitting(self) -> None:
        # The caller never sees the first response (e.g. the connection dropped after COMMIT).
        subprocess.run([sys.executable, "-m", "cli_relay", "--home", str(self.home), "submit", "--key", "k-lost",
                        "--engine", "fixture", "--cwd", str(self.work), "--prompt", "p",
                        "--option", "duration_s=0.5", "--option",
                        f"side_effect_file={json.dumps(str(self.effects))}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=base_env(), check=True)
        again = self.cli("submit", "--key", "k-lost", "--engine", "fixture", "--cwd", str(self.work),
                         "--prompt", "p", "--option", "duration_s=0.5",
                         "--option", f"side_effect_file={json.dumps(str(self.effects))}")
        self.assertEqual(again["status"], "duplicate")
        self.cli("wait", again["task_id"], "--timeout", "30")
        self.assertEqual(len(side_effects(self.effects)), 1)


if __name__ == "__main__":
    unittest.main()

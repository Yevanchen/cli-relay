"""Audit #9: the soak verdict must fail when tasks did not run, or the long task/run was too short."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import REPO, run_cli

from cli_relay.ledger import Ledger


def load_soak():
    spec = importlib.util.spec_from_file_location("cli_relay_soak_under_test", REPO / "scripts" / "soak.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SoakVerdict(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-a09-"))
        self.root = self.tmp / "soak"
        self.home = self.root / "ledger"
        for d in (self.root / "work", self.root / "effects"):
            d.mkdir(parents=True)
        code, out = run_cli(self.home, "init")
        self.assertEqual(code, 0, out)
        self.ledger = Ledger(self.home)

    def tearDown(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def task(self, key: str, duration: float) -> str:
        payload = {"engine": "fixture", "cwd": str(self.root / "work"), "prompt": f"soak {key}",
                   "options": {"duration_s": duration}}
        _, task, _ = self.ledger.submit(scope="default", key=key, payload=payload, prompt=f"soak {key}",
                                        native_session_id=str(uuid.uuid4()))
        return task["task_id"]

    def close(self, task_id: str, attempt_no: int, outcome: str, *, exit_known: int = 1,
              ran_s: float = 1.0, state: str = "finished") -> None:
        # Through the ledger, so each closed attempt has its event, as a real run would.
        aid = self.ledger.attempts(task_id)[attempt_no - 1]["attempt_id"]
        if state == "finished":
            self.ledger.finish(aid, from_states=("pending",), outcome=outcome, exit_code=0 if exit_known else None,
                               exit_known=bool(exit_known), native={"ok": outcome == "succeeded"}, source="test")
        else:
            self.ledger.mark_unknown(aid, from_states=("pending",), evidence={"synthetic": True})
        end = datetime.now(timezone.utc)
        conn = sqlite3.connect(str(self.home / "ledger.db"))
        conn.execute("UPDATE attempts SET started_at=?, ended_at=? WHERE attempt_id=?",
                     (iso(end - timedelta(seconds=ran_s)), iso(end), aid))
        conn.commit()
        conn.close()

    def effects(self, key: str, n: int) -> None:
        if n:
            (self.root / "effects" / f"{key}.jsonl").write_text("".join('{"x":1}\n' for _ in range(n)))

    def build(self, *, total_s: float, elapsed_s: float, long_ran_s: float, short_outcome: str,
              short_effects: int) -> dict:
        keys = {}
        long_id = self.task("soak-long", total_s * 0.98)
        self.close(long_id, 1, "succeeded", ran_s=long_ran_s)
        self.effects("soak-long", 1)
        kill_id = self.task("soak-keeper-kill", max(5.0, total_s * 0.08))
        self.close(kill_id, 1, "succeeded", exit_known=0)
        self.effects("soak-keeper-kill", 1)
        unk_id = self.task("soak-unknown", 1.0)
        self.close(unk_id, 1, "unknown", state="unknown")
        self.ledger.new_attempt(unk_id, expect_attempt_no=1, reason="rerun", prompt="soak soak-unknown",
                                resume_session_id=None, native_session_id=str(uuid.uuid4()))
        self.close(unk_id, 2, "succeeded")
        self.effects("soak-unknown", 1)
        short_id = self.task("soak-short-0000", 0.05)
        self.close(short_id, 1, short_outcome)
        self.effects("soak-short-0000", short_effects)
        for key, tid, dur in (("soak-long", long_id, total_s * 0.98), ("soak-keeper-kill", kill_id, 5.0),
                              ("soak-unknown", unk_id, 1.0), ("soak-short-0000", short_id, 0.05)):
            keys[key] = {"task_id": tid, "expected_effects": 1, "duration_s": dur}
        state = {"started_wall": time.time() - elapsed_s, "total_s": total_s,
                 "done": {"setup": True, "kill_keeper": True, "rerun_unknown": True}, "keys": keys, "events": {},
                 "short_index": 1, "next_short_at": 1e12, "next_ack_at": 1e12, "next_ckpt_at": 1e12,
                 "restarts": 1, "gaps": [], "notes": [], "ack_duplicates": 1}
        (self.root / "state.json").write_text(json.dumps(state))
        soak = load_soak()
        soak.Soak(self.root, total_s / 3600.0).finish()
        return json.loads((self.root / "summary.json").read_text())

    def test_unexecuted_short_task_and_too_short_long_task_fail(self) -> None:
        summary = self.build(total_s=90000.0, elapsed_s=2.0, long_ran_s=1.0, short_outcome="blocked", short_effects=0)
        self.assertEqual(summary["verdict"], "FAIL", summary["verdicts"])

    def test_consistent_run_passes(self) -> None:
        summary = self.build(total_s=10.0, elapsed_s=12.0, long_ran_s=9.8, short_outcome="succeeded", short_effects=1)
        self.assertEqual(summary["verdict"], "PASS", summary["verdicts"])

    # Each fact on its own must be able to fail the verdict.
    def test_a_short_task_that_never_executed_fails(self) -> None:
        summary = self.build(total_s=10.0, elapsed_s=12.0, long_ran_s=9.8, short_outcome="blocked", short_effects=0)
        self.assertEqual(summary["verdict"], "FAIL")
        self.assertFalse(summary["verdicts"]["every_key_executed_exactly_as_expected"])
        self.assertIn("soak-short-0000", summary["keys_not_as_expected"])

    def test_a_long_task_cut_short_fails(self) -> None:
        summary = self.build(total_s=10.0, elapsed_s=12.0, long_ran_s=3.0, short_outcome="succeeded", short_effects=1)
        self.assertEqual(summary["verdict"], "FAIL")
        self.assertFalse(summary["verdicts"]["long_task_ran_its_full_duration"])

    def test_a_run_shorter_than_planned_fails(self) -> None:
        summary = self.build(total_s=10.0, elapsed_s=4.0, long_ran_s=9.8, short_outcome="succeeded", short_effects=1)
        self.assertEqual(summary["verdict"], "FAIL")
        self.assertFalse(summary["verdicts"]["ran_for_the_planned_time"])

    def test_verdict_command_re_evaluates_a_finished_soak_without_writing(self) -> None:
        self.build(total_s=90000.0, elapsed_s=2.0, long_ran_s=1.0, short_outcome="blocked", short_effects=0)
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        p = subprocess.run([sys.executable, str(REPO / "scripts" / "soak.py"), "verdict", "--dir", str(self.root)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(json.loads(p.stdout)["verdict"], "FAIL")
        after = {q: q.stat().st_mtime_ns for q in self.root.rglob("*") if q.is_file() and q.suffix != ".db-shm"}
        self.assertEqual({k: v for k, v in before.items() if k.suffix != ".db-shm"}, after)


if __name__ == "__main__":
    unittest.main()

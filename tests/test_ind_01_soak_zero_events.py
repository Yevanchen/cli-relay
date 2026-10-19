"""Independent audit 1: the soak verdict must not PASS without the events, deliveries and ACKs the run
should have produced. Every finished attempt owes a terminal event and every unknown attempt an
unknown event; each must have been delivered to and acknowledged by the soak consumer."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import REPO, run_cli

from cli_relay.ledger import Ledger

TOTAL_S = 10.0


def load_soak():
    spec = importlib.util.spec_from_file_location("cli_relay_soak_zero_events", REPO / "scripts" / "soak.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SoakVerdictNeedsEventFacts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-ind01-"))
        self.root = self.tmp / "soak"
        self.home = self.root / "ledger"
        for d in (self.root / "work", self.root / "effects"):
            d.mkdir(parents=True)
        self.assertEqual(run_cli(self.home, "init")[0], 0)
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

    def attempt_id(self, task_id: str, no: int) -> str:
        return self.ledger.attempts(task_id)[no - 1]["attempt_id"]

    def close(self, task_id: str, no: int, *, unknown: bool = False, exit_known: bool = True, ran_s: float = 1.0,
              with_event: bool) -> None:
        aid = self.attempt_id(task_id, no)
        if with_event and unknown:
            self.ledger.mark_unknown(aid, from_states=("pending",), evidence={"synthetic": True})
        elif with_event:
            self.ledger.finish(aid, from_states=("pending",), outcome="succeeded", exit_code=0 if exit_known else None,
                               exit_known=exit_known, native={"ok": True, "result": "ok"}, source="test")
        else:  # the counterexample: states written directly, no event rows at all
            conn = sqlite3.connect(str(self.home / "ledger.db"))
            conn.execute("UPDATE attempts SET state=?, outcome=?, exit_known=? WHERE attempt_id=?",
                         ("unknown" if unknown else "finished", None if unknown else "succeeded",
                          1 if exit_known else 0, aid))
            conn.execute("UPDATE tasks SET state=? WHERE task_id=?", ("unknown" if unknown else "succeeded", task_id))
            conn.commit()
            conn.close()
        end = datetime.now(timezone.utc)
        conn = sqlite3.connect(str(self.home / "ledger.db"))
        conn.execute("UPDATE attempts SET started_at=?, ended_at=? WHERE attempt_id=?",
                     (iso(end - timedelta(seconds=ran_s)), iso(end), aid))
        conn.commit()
        conn.close()

    def effects(self, key: str) -> None:
        (self.root / "effects" / f"{key}.jsonl").write_text('{"x":1}\n')

    def build(self, *, with_events: bool, skip_event_for: str = "") -> dict:
        keys = {}
        long_id = self.task("soak-long", TOTAL_S * 0.98)
        self.close(long_id, 1, ran_s=TOTAL_S * 0.98, with_event=with_events and skip_event_for != "soak-long")
        kill_id = self.task("soak-keeper-kill", 5.0)
        self.close(kill_id, 1, exit_known=False, with_event=with_events and skip_event_for != "soak-keeper-kill")
        unk_id = self.task("soak-unknown", 1.0)
        self.close(unk_id, 1, unknown=True, with_event=with_events)
        self.ledger.new_attempt(unk_id, expect_attempt_no=1, reason="rerun", prompt="soak soak-unknown",
                                resume_session_id=None, native_session_id=str(uuid.uuid4()))
        self.close(unk_id, 2, with_event=with_events)
        short_id = self.task("soak-short-0000", 0.05)
        self.close(short_id, 1, with_event=with_events and skip_event_for != "soak-short-0000")
        for key, tid, dur in (("soak-long", long_id, TOTAL_S * 0.98), ("soak-keeper-kill", kill_id, 5.0),
                              ("soak-unknown", unk_id, 1.0), ("soak-short-0000", short_id, 0.05)):
            keys[key] = {"task_id": tid, "expected_effects": 1, "duration_s": dur}
            self.effects(key)
        state = {"started_wall": time.time() - TOTAL_S * 1.2, "total_s": TOTAL_S,
                 "done": {"setup": True, "kill_keeper": True, "rerun_unknown": True}, "keys": keys, "events": {},
                 "short_index": 1, "next_short_at": 1e12, "next_ack_at": 1e12, "next_ckpt_at": 1e12,
                 "restarts": 1, "gaps": [], "notes": [], "ack_duplicates": 1}
        (self.root / "state.json").write_text(json.dumps(state))
        load_soak().Soak(self.root, TOTAL_S / 3600.0).finish()
        return json.loads((self.root / "summary.json").read_text())

    def counts(self) -> dict:
        conn = sqlite3.connect(str(self.home / "ledger.db"))
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("events", "acks", "deliveries")}
        finally:
            conn.close()

    def test_zero_events_acks_and_deliveries_fail(self) -> None:
        summary = self.build(with_events=False)
        self.assertEqual(self.counts(), {"events": 0, "acks": 0, "deliveries": 0})
        self.assertEqual(summary["verdict"], "FAIL", summary["verdicts"])

    def test_one_attempt_without_its_event_fails(self) -> None:
        summary = self.build(with_events=True, skip_event_for="soak-short-0000")
        self.assertEqual(summary["verdict"], "FAIL", summary["verdicts"])

    def test_the_same_run_with_its_events_delivered_and_acked_passes(self) -> None:
        summary = self.build(with_events=True)
        self.assertEqual(self.counts()["events"], 5, "4 terminal events and 1 unknown event")
        self.assertEqual(summary["verdict"], "PASS", summary["verdicts"])


if __name__ == "__main__":
    unittest.main()

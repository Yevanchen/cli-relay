"""Responsibility 3: terminal results stay deliverable until the consumer ACKs them."""

from __future__ import annotations

import unittest

from helpers import REPO, RelayTestCase, db_rows


class AckSemantics(RelayTestCase):
    def finished_task(self, key: str) -> dict:
        sub = self.submit(key, duration=0.1)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        return sub

    def test_events_are_redelivered_with_stable_ids_until_ack(self) -> None:
        sub = self.finished_task("k-redeliver")
        first = self.cli("events", "--consumer", "grok")["events"]
        second = self.cli("events", "--consumer", "grok")["events"]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["event_id"], second[0]["event_id"])
        self.assertEqual(first[0]["body"], second[0]["body"])
        self.assertEqual((first[0]["delivery_count"], second[0]["delivery_count"]), (1, 2))
        self.cli("ack", "--consumer", "grok", "--task", sub["task_id"], "--attempt", "1",
                 "--event", first[0]["event_id"])
        self.assertEqual(self.cli("events", "--consumer", "grok")["events"], [])
        kept = self.cli("events", "--consumer", "grok", "--include-acked")["events"]
        self.assertEqual(kept[0]["event_id"], first[0]["event_id"])
        self.assertIsNotNone(kept[0]["acked_at"])

    def test_lost_or_repeated_ack_is_idempotent(self) -> None:
        sub = self.finished_task("k-ack-twice")
        ev = self.cli("events", "--consumer", "grok")["events"][0]
        args = ("ack", "--consumer", "grok", "--task", sub["task_id"], "--attempt", "1", "--event", ev["event_id"])
        one = self.cli(*args)
        # The consumer never saw the reply (ACK response lost) and sends it again.
        two = self.cli(*args)
        self.assertFalse(one["duplicate"])
        self.assertTrue(two["duplicate"])
        self.assertEqual(one["acked_at"], two["acked_at"])
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM acks")), 1)

    def test_mismatched_ack_is_rejected(self) -> None:
        a = self.finished_task("k-mis-a")
        b = self.finished_task("k-mis-b")
        events = {e["task_id"]: e for e in self.cli("events", "--consumer", "grok")["events"]}
        ev_a = events[a["task_id"]]
        wrong_attempt = self.cli("ack", "--consumer", "grok", "--task", a["task_id"], "--attempt", "2",
                                 "--event", ev_a["event_id"], expect=6)
        self.assertEqual(wrong_attempt["error"], "ack_mismatch")
        wrong_task = self.cli("ack", "--consumer", "grok", "--task", b["task_id"], "--attempt", "1",
                              "--event", ev_a["event_id"], expect=6)
        self.assertEqual(wrong_task["error"], "ack_mismatch")
        unknown = self.cli("ack", "--consumer", "grok", "--task", a["task_id"], "--attempt", "1",
                           "--event", "e-does-not-exist", expect=6)
        self.assertEqual(unknown["error"], "ack_mismatch")
        self.assertEqual(db_rows(self.home, "SELECT COUNT(*) FROM acks")[0][0], 0)
        self.assertEqual(len(self.cli("events", "--consumer", "grok")["events"]), 2)

    def test_consumers_are_independent(self) -> None:
        sub = self.finished_task("k-two-consumers")
        ev = self.cli("events", "--consumer", "grok")["events"][0]
        self.cli("ack", "--consumer", "grok", "--task", sub["task_id"], "--attempt", "1", "--event", ev["event_id"])
        self.assertEqual(self.cli("events", "--consumer", "grok")["events"], [])
        other = self.cli("events", "--consumer", "phone")["events"]
        self.assertEqual([e["event_id"] for e in other], [ev["event_id"]])

    def test_offline_consumer_reads_everything_later(self) -> None:
        subs = [self.finished_task(f"k-offline-{i}") for i in range(3)]
        later = self.cli("events", "--consumer", "late-reader")["events"]
        self.assertEqual(sorted(e["task_id"] for e in later), sorted(s["task_id"] for s in subs))

    def test_ledger_code_never_deletes(self) -> None:
        # Retention is a code property: no statement in the package removes rows.
        for path in (REPO / "cli_relay").glob("*.py"):
            text = path.read_text(encoding="utf-8").upper()
            self.assertNotIn("DELETE FROM", text, f"{path.name} must not delete ledger rows")
            self.assertNotIn("DROP TABLE", text, f"{path.name} must not drop ledger tables")


if __name__ == "__main__":
    unittest.main()

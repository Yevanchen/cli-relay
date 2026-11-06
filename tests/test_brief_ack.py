"""C2-C5: receipt migration, atomic batches, read-only briefs and terminal result paths."""

from __future__ import annotations

import json
import shlex
import sqlite3
import unittest
from unittest import mock

from direct_route_helpers import DirectRouteCase
from helpers import run_cli
from notify_helpers import FakeHTTPReceiver
from cli_relay import cli, notify
from cli_relay.ledger import FORMAT, NOTIFIER_DDL, SCHEMA, Ledger, canonical_json, now_iso, payload_hash


class BriefAndAck(DirectRouteCase):
    def refs(self, *events, consumer="reader"):
        return [{"task_id": task["task_id"], "attempt_no": 1, "event_id": event,
                 "ledger_id": self.ledger.ledger_id, "source_id": None, "consumer": consumer}
                for task, event in events]

    def batch(self, refs, *, consumer="reader", expected=0):
        return self.command("ack", "--consumer", consumer, "--events", canonical_json(refs),
                            "--message-id", "batch-visible", "--chat", "chat-1", expected=expected)

    def test_message_id_and_chat_are_stored_shown_and_first_receipt_wins(self):
        task, event = self.finish()
        args = ["ack", "--consumer", "reader", "--task", task["task_id"], "--attempt", "1", "--event", event]
        first = self.command(*args, "--message-id", "msg-123", "--chat", "chat-123")
        again = self.command(*args, "--message-id", "msg-new", "--chat", "chat-new")
        self.assertTrue(again["duplicate"])
        self.assertEqual((again["acked_at"], again["message_id"], again["chat"]),
                         (first["acked_at"], "msg-123", "chat-123"))
        ack = self.ledger.conn.execute("SELECT message_id, chat FROM acks").fetchone()
        self.assertEqual(tuple(ack), ("msg-123", "chat-123"))
        event = self.command("events", "--consumer", "reader", "--include-acked", "--no-reconcile")["events"][0]
        self.assertEqual((event["message_id"], event["chat"]), ("msg-123", "chat-123"))
        record = self.command("status", task["task_id"], "--no-reconcile")["events"][0]
        self.assertEqual(record["acked_by"], ["reader"])
        self.assertEqual((record["acks"][0]["message_id"], record["acks"][0]["chat"]), ("msg-123", "chat-123"))

    def test_atomic_batch_happy_duplicate_and_repeated_reference(self):
        a, b = self.finish("a"), self.finish("b")
        refs = self.refs(a, b, a)
        first = self.batch(refs)["events"]
        self.assertEqual([r["duplicate"] for r in first], [False, False, True])
        self.assertTrue(all(r["duplicate"] for r in self.batch(refs)["events"]))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 2)

    def test_one_bad_reference_rolls_back_the_entire_batch(self):
        refs = self.refs(self.finish("a"), self.finish("b"))
        for key, value, error in (("ledger_id", "l-other", "wrong_ledger"),
                                  ("consumer", "other", "wrong_consumer"),
                                  ("event_id", "e-missing", "ack_mismatch"),
                                  ("attempt_no", 2, "ack_mismatch"),
                                  ("source_id", "other-host", "wrong_source")):
            bad = [refs[0], {**refs[1], key: value}]
            self.assertEqual(self.batch(bad, expected=6)["error"], error)
            self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
        self.batch(refs)

    def test_brief_fields_and_printed_batch_command_execute_as_a_real_cli(self):
        self.add()
        self.prove()
        result = self.work / "out" / "result file.json"
        result.parent.mkdir()
        result.write_text('{"status":"partial"}')
        origin = {"bot": "bot", "chat": "original-chat"}
        a = self.finish("a", route="bot", options={"result_file": "out/result file.json"}, origin=origin)
        b = self.finish("b", route="bot", result="status=failed")
        with mock.patch.object(cli, "reconcile_all", side_effect=AssertionError("brief reconciled")), \
                mock.patch.object(Ledger, "record_deliveries", side_effect=AssertionError("brief delivered")), \
                mock.patch.object(Ledger, "bind_notification_owners", side_effect=AssertionError("brief bound")), \
                mock.patch.object(notify, "send", side_effect=AssertionError("brief sent")):
            report = self.command("brief", "--consumer", "bot")
            self.assertEqual(self.command("brief", "--consumer", "bot"), report)
            text = self.command("brief", "--consumer", "bot", "--text", text=True)
        self.assertEqual(len(report["events"]), 2)
        ev = next(e for e in report["events"] if e["event_id"] == a[1])
        for key in ("event_id", "task_id", "attempt_no", "ledger_id", "source_id", "key", "origin", "outcome",
                    "business_status", "business_status_source", "warnings", "result_file"):
            self.assertIn(key, ev)
        self.assertEqual((ev["task_last8"], ev["event_last8"]), (a[0]["task_id"][-8:], a[1][-8:]))
        self.assertEqual(ev["origin"], origin)
        self.assertEqual(ev["result_file"]["path"], str(result.resolve()))
        self.assertTrue(ev["result_file"]["exists"])
        self.assertEqual(len(ev["result_file"]["sha256"]), 64)
        self.assertIn("部分完成", ev["message"])
        self.assertIn(str(result.resolve()), ev["message"])
        self.assertEqual(len(ev["message"].splitlines()), 1)
        self.assertIn(ev["message"], text)
        self.assertIn('event_id="' + a[1] + '"', text)
        self.assertIn('business_status="partial"', text)
        self.assertIn('warnings=', text)
        self.assertIn('origin={"bot":"bot","chat":"original-chat"}', text)
        self.assertEqual(text.splitlines()[-1], report["ack_command"])
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
        argv = shlex.split(report["ack_command"].replace("<MESSAGE_ID>", "sent-message-1"))
        self.assertEqual(argv[:3], ["cli-relay", "--home", str(self.home)])
        code, ack = run_cli(self.home, *argv[3:])
        self.assertEqual(code, 0, ack)
        self.assertEqual(len(ack["events"]), 2)
        self.assertEqual(self.command("brief", "--consumer", "bot")["events"], [])
        self.assertEqual({tuple(row) for row in self.ledger.conn.execute("SELECT message_id,chat FROM acks")},
                         {("sent-message-1", "bot-chat")})
        self.assert_no_secret()

    def test_brief_filters_consumer_route_task_and_ignores_waiting_events(self):
        for name in ("a", "b"):
            self.add(name)
            self.prove(name)
        a = self.finish("a", route="a")
        self.finish("b", route="b")
        with self.ledger.tx() as c:
            c.execute("INSERT INTO events(event_id, task_id, attempt_no, kind, body_json, created_at) "
                      "VALUES ('e-wait',?,1,'waiting','{}',?)", (a[0]["task_id"], now_iso()))
        self.assertEqual([e["event_id"] for e in self.command("brief", "--consumer", "a")["events"]], [a[1]])
        self.assertEqual(self.command("brief", "--consumer", "a", "--route", "b")["events"], [])
        self.assertEqual(len(self.command("brief", "--consumer", "a", "--task", a[0]["task_id"])["events"]), 1)
        self.assertEqual(self.command("brief", "--consumer", "unrelated")["events"], [])

    def test_brief_preserves_existing_delivery_count_and_terminal_file_evidence(self):
        task, event = self.finish(options={"result_file": "later.json"})
        before = self.command("events", "--consumer", "reader", "--no-reconcile")["events"][0]["delivery_count"]
        brief = self.command("brief", "--consumer", "reader")
        self.assertFalse(brief["events"][0]["result_file"]["exists"])
        (self.work / "later.json").write_text('{"status":"ok"}')
        self.assertFalse(self.command("brief", "--consumer", "reader")["events"][0]["result_file"]["exists"])
        self.assertEqual(self.ledger.conn.execute("SELECT count FROM deliveries").fetchone()[0], before)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)

    def test_terminal_result_file_absolute_path_and_exists_reach_http_payload(self):
        self.add()
        self.prove()
        for exists in (True, False):
            path = self.work / (str(exists) + ".json")
            if exists:
                path.write_text('{"status":"ok"}')
            task, event = self.finish(str(exists), route="bot", options={"result_file": path.name})
            expected = {"path": str(path.resolve()), "exists": exists}
            body = json.loads(self.ledger.events_of_task(task["task_id"])[0]["body_json"])
            self.assertEqual({k: body["result_file"][k] for k in expected}, expected)
            with FakeHTTPReceiver() as receiver:
                self.command("notify", "flush")
            received = receiver.requests[0]["cli_relay"]["events"][0]["result_file"]
            self.assertEqual({k: received[k] for k in expected}, expected)

    def test_reused_result_path_cannot_rewrite_attempt_snapshot_or_brief(self):
        path = self.work / "result.json"
        path.write_text('{"status":"partial","stop_reason":"needs_user"}')
        task, event = self.finish("snapshots", options={"result_file":"result.json"})
        first = self.command("brief", "--consumer", "reader")["events"][0]
        frozen = first["result_evidence"]
        path.write_text('{"status":"ok"}')
        new = self.ledger.new_attempt(task["task_id"], expect_attempt_no=1, reason="fixture",
                                      prompt="fixture", resume_session_id=None, native_session_id=None)
        self.ledger.finish(new["attempt_id"], from_states=("pending",), outcome="succeeded", native={"result":"status=ok"})
        briefs = self.command("brief", "--consumer", "reader")["events"]
        old = next(e for e in briefs if e["event_id"] == event)
        second = next(e for e in briefs if e["event_id"] != event)
        self.assertEqual(old["result_evidence"], frozen)
        self.assertEqual(old["business_stop_reason"], "needs_user")
        self.assertNotEqual(second["result_evidence"]["sha256"], frozen["sha256"])
        self.assertEqual(second["business_status"], "ok")
        path.unlink()
        self.assertEqual(self.command("brief", "--consumer", "reader")["events"], briefs)

    def test_without_result_file_payload_hash_and_terminal_shape_stay_compatible(self):
        task, event = self.finish()
        expected = {"engine": "fixture", "cwd": str(self.work.resolve()), "prompt": "p", "options": {}}
        self.assertEqual(task["payload_sha256"], payload_hash(expected))
        self.assertNotIn("result_file", json.loads(self.ledger.events_of_task(task["task_id"])[0]["body_json"]))
        brief = self.command("brief", "--consumer", "reader")["events"][0]
        self.assertIsNone(brief["result_file"])
        self.assertIn("无结果文件", brief["message"])
        # Legacy routes can have an empty chat label. The printed receipt command must
        # still execute; an unknown/empty delivery chat is simply omitted.
        self.command("route", "add", "bot", "--source-id", "test-mac", "--consumer", "bot", "--chat", "",
                     "--url-env", "ROUTE_URL", "--token-env", "ROUTE_TOKEN")
        self.prove()
        self.finish("empty-chat", route="bot")
        command = self.command("brief", "--consumer", "bot", "--route", "bot")["ack_command"]
        argv = shlex.split(command.replace("<MESSAGE_ID>", "empty-chat-message"))
        self.assertNotIn("--chat", argv)
        code, ack = run_cli(self.home, *argv[3:])
        self.assertEqual(code, 0, ack)

    def test_malformed_batch_and_receipt_inputs_are_refused_without_writes(self):
        for value in ("x", "null", "[]", '[{"event_id":"e-1"}]'):
            self.command("ack", "--consumer", "reader", "--events", value, expected=2)
        task, event = self.finish()
        self.command("ack", "--consumer", "reader", "--events", canonical_json(self.refs((task, event))),
                     "--task", task["task_id"], expected=2)
        for flag, value in (("--message-id", "bad\n"), ("--chat", "bad\n")):
            self.command("ack", "--consumer", "reader", "--task", task["task_id"], "--attempt", "1",
                         "--event", event, flag, value, expected=2)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)


class AckMigration(DirectRouteCase):
    def test_previous_schema_opens_and_migrates_with_backup_and_preserved_ack(self):
        self.ledger.close()
        self.home = self.root / "old-ledger"
        self.home.mkdir()
        conn = sqlite3.connect(self.home / "ledger.db")
        conn.executescript(SCHEMA)
        for ddl in NOTIFIER_DDL[:5]:  # the old transport tables/indexes, without direct-route additions
            conn.execute(ddl)
        for name, kind in (("notify_route", "TEXT"), ("notify_consumer", "TEXT"), ("notify_chat", "TEXT"),
                           ("notify_bound", "INTEGER NOT NULL DEFAULT 0")):
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
        conn.executemany("INSERT INTO meta(k,v) VALUES (?,?)", [("schema_version", "1"),
                         ("notifier_schema", "2"), ("notifier_cutoff", "2000-01-01T00:00:00.000Z")])
        conn.commit()
        conn.close()
        (self.home / "LEDGER.json").write_text(json.dumps({"format": FORMAT, "ledger_id": "l-old"}))
        self.ledger = Ledger(self.home)
        self.addCleanup(self.ledger.close)
        task, event = self.finish()
        self.command("ack", "--consumer", "old", "--task", task["task_id"], "--attempt", "1", "--event", event)
        before = self.command("events", "--consumer", "old", "--include-acked", "--no-reconcile")["events"][0]
        self.assertIsNone(before["message_id"])
        self.assertEqual(self.command("brief", "--consumer", "old", expected=6)["error"], "not_migrated")
        self.command("ack", "--consumer", "new", "--task", task["task_id"], "--attempt", "1", "--event", event,
                     "--message-id", "m1", expected=6)
        migrated = self.command("migrate")
        backup = sqlite3.connect(migrated["backup"])
        self.assertNotIn("message_id", {row[1] for row in backup.execute("PRAGMA table_info(acks)")})
        self.assertEqual(backup.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 1)
        backup.close()
        self.assertEqual(self.ledger.notifier_cutoff(), "2000-01-01T00:00:00.000Z")
        self.command("ack", "--consumer", "new", "--task", task["task_id"], "--attempt", "1", "--event", event,
                     "--message-id", "m1", "--chat", "c1")
        old = self.command("status", task["task_id"], "--no-reconcile")["events"][0]["acks"]
        self.assertEqual({(a["consumer"], a["message_id"]) for a in old}, {("old", None), ("new", "m1")})
        self.assertFalse(self.command("migrate")["migrated"])


if __name__ == "__main__":
    unittest.main()

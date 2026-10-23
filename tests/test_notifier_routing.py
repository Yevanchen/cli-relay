"""Routing ownership survives configuration edits, duplicate submits and migration.

Only temporary ledgers and direct calls are used. Process spawns and notification
adapters are mocked; no native coding CLI, HTTP endpoint or model is contacted.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from notify_helpers import write_config

from cli_relay import cli, notify
from cli_relay.ledger import FORMAT, NOTIFIER_DDL, SCHEMA, Ledger, init_ledger, now_iso


class ImmutableNotificationOwners(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="cli-relay-routing-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "ledger"
        self.work = self.root / "work"
        self.work.mkdir()
        init_ledger(self.home)
        self.ledger = Ledger(self.home)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.ledger.close)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.root), "PATH": "/usr/bin:/bin"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for name, result in (("spawn_keeper", None), ("spawn_notifier_pass", None), ("reconcile_task", [])):
            patcher = mock.patch.object(cli, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

    def config(self, *, default="a", consumer_a="consumer-a", chat_a="chat-a") -> dict:
        raw = {
            "source_id": "mac-a", "default_route": default,
            "routes": {name: {"consumer": f"consumer-{name}", "chat": f"chat-{name}",
                              "adapter": {"type": "command", "argv": ["/bin/true"]}}
                       for name in ("a", "b")},
        }
        raw["routes"]["a"].update(consumer=consumer_a, chat=chat_a)
        self.write_config(raw)
        return raw

    def write_config(self, raw: dict) -> None:
        write_config(self.home, raw)

    def command(self, *args: str, expected=0) -> dict:
        output = io.StringIO()
        opened = []

        def open_ledger(*positional, **kwargs):
            ledger = Ledger(*positional, **kwargs)
            opened.append(ledger)
            return ledger

        try:
            with contextlib.redirect_stdout(output), mock.patch.object(cli, "Ledger", side_effect=open_ledger), \
                    mock.patch.object(notify, "Ledger", side_effect=open_ledger):
                code = cli.main(["--home", str(self.home), *args])
        finally:
            for ledger in opened:
                ledger.close()
        result = json.loads(output.getvalue())
        self.assertEqual(code, expected, result)
        return result

    def submit(self, key="k", route=None, **kwargs) -> dict:
        args = ["submit", "--engine", "fixture", "--key", key, "--cwd", str(self.work), "--prompt", "p"]
        if route is not None:
            args += ["--notify-route", route]
        return self.command(*args, **kwargs)

    def finish(self, task: dict) -> str:
        att = self.ledger.current_attempt(task["task_id"])
        return self.ledger.finish(att["attempt_id"], from_states=("pending",), outcome="succeeded")

    def events(self) -> list:
        return self.command("events", "--consumer", "reader", "--no-reconcile", "--include-acked")["events"]

    def flush(self) -> dict:
        with mock.patch.object(notify, "send", return_value={"outcome": "accepted"}) as sender:
            result = notify.flush(self.home, env={})
        result["mock_send_count"] = sender.call_count
        return result

    def test_default_is_bound_in_submit_before_completion(self) -> None:
        self.config()
        task = self.submit()
        self.assertEqual((task["notify_route"], task["notify_consumer"], task["notify_chat"], task["notify_bound"]),
                         ("a", "consumer-a", "chat-a", True))
        self.config(default="b")
        self.finish(task)
        self.assertEqual(self.events()[0]["route"], "a")
        self.flush()
        row = self.ledger.conn.execute("SELECT route FROM notifications").fetchone()
        self.assertEqual(row[0], "a")

    def test_events_keep_the_route_already_enqueued(self) -> None:
        self.config()
        task = self.submit()
        self.finish(task)
        notify.enqueue(self.ledger, notify.load_config(self.home))
        self.config(default="b")
        event = self.events()[0]
        self.assertEqual((event["route"], event["owner_consumer"], event["owner_chat"]),
                         ("a", "consumer-a", "chat-a"))

    def test_explicit_duplicate_keeps_first_owner_even_after_route_removed(self) -> None:
        cfg = self.config()
        first = self.submit(route="a")
        second = self.submit(route="b")
        self.assertEqual((second["status"], second["task_id"], second["attempt_id"], second["notify_route"]),
                         ("duplicate", first["task_id"], first["attempt_id"], "a"))
        cfg["routes"].pop("a")
        cfg["default_route"] = "b"
        self.write_config(cfg)
        again = self.submit(route="a")
        self.assertEqual((again["status"], again["task_id"], again["notify_consumer"]),
                         ("duplicate", first["task_id"], "consumer-a"))
        (self.home / "notifier.json").write_text("not json")
        self.assertEqual(self.submit(route="a")["task_id"], first["task_id"])

    def test_known_no_route_is_not_rebound_to_a_future_default(self) -> None:
        self.config(default=None)
        task = self.submit()
        self.assertTrue(task["notify_bound"])
        self.assertIsNone(task["notify_route"])
        self.config(default="a")
        self.finish(task)
        self.assertIsNone(self.events()[0]["route"])
        self.assertEqual(self.flush()["mock_send_count"], 0)

    def test_preconfiguration_task_binds_once_when_first_known(self) -> None:
        task = self.submit()
        self.assertFalse(task["notify_bound"])
        self.finish(task)
        self.config(default="a")
        self.assertEqual(self.events()[0]["route"], "a")
        self.config(default="b")
        self.assertEqual(self.events()[0]["route"], "a")

    def test_preconfiguration_explicit_route_wins_over_later_default(self) -> None:
        # Historical tasks could reserve a route before any configuration existed. New
        # submit commands require a registered, proven route, but old reservations still bind.
        _, task, _ = self.ledger.submit(scope="default", key="k", prompt="p", native_session_id=None,
            payload={"engine": "fixture", "cwd": str(self.work), "prompt": "p", "options": {}}, notify_route="a")
        self.config(default="b")
        self.finish(task)
        self.assertEqual(self.events()[0]["route"], "a")

    def test_nonowner_ack_does_not_become_owner_ack_after_config_change(self) -> None:
        self.config()
        task = self.submit()
        eid = self.finish(task)
        notify.enqueue(self.ledger, notify.load_config(self.home))
        self.ledger.ack(task_id=task["task_id"], attempt_no=1, event_id=eid, consumer="consumer-b")
        self.config(consumer_a="consumer-b", chat_a="chat-b")
        self.assertEqual(notify.sync_acks(self.ledger, notify.load_config(self.home)), 0)
        self.assertEqual(self.flush()["mock_send_count"], 0)
        row = self.ledger.conn.execute("SELECT state,last_result_json FROM notifications").fetchone()
        self.assertEqual((row["state"], json.loads(row["last_result_json"])["why"]), ("failed", "route_owner_changed"))
        self.ledger.ack(task_id=task["task_id"], attempt_no=1, event_id=eid, consumer="consumer-a")
        self.assertEqual(notify.sync_acks(self.ledger, notify.load_config(self.home)), 1)

    def test_wrong_source_or_ledger_does_not_write_ack(self) -> None:
        self.config()
        task = self.submit()
        eid = self.finish(task)
        args = ["ack", "--consumer", "consumer-a", "--task", task["task_id"], "--attempt", "1", "--event", eid]
        wrong_source = self.command(*args, "--ledger-id", self.ledger.ledger_id, "--source-id", "mac-b", expected=6)
        self.assertEqual(wrong_source["error"], "wrong_source")
        wrong_ledger = self.command(*args, "--ledger-id", "l-other", expected=6)
        self.assertEqual(wrong_ledger["error"], "wrong_ledger")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)

    def legacy_task(self, *, sent=False) -> dict:
        self.ledger.close()
        self.home = self.root / "legacy"
        self.home.mkdir()
        conn = sqlite3.connect(self.home / "ledger.db")
        conn.executescript(SCHEMA)
        for ddl in NOTIFIER_DDL:
            conn.execute(ddl)
        conn.execute("ALTER TABLE tasks ADD COLUMN notify_route TEXT")
        conn.executemany("INSERT INTO meta(k,v) VALUES (?,?)", [
            ("schema_version", "1"), ("notifier_schema", "1"), ("notifier_cutoff", "")])
        conn.commit()
        conn.close()
        (self.home / "LEDGER.json").write_text(json.dumps({"format": FORMAT, "ledger_id": "l-legacy"}))
        self.ledger = Ledger(self.home)
        self.addCleanup(self.ledger.close)
        payload = {"engine": "fixture", "cwd": str(self.work), "prompt": "p", "options": {}}
        _, task, _ = self.ledger.submit(scope="default", key="legacy", payload=payload, prompt="p", native_session_id=None)
        eid = self.finish(task)
        if sent:
            now = now_iso()
            self.ledger.conn.execute(
                "INSERT INTO notifications(event_id,route,state,attempts,wakes,first_sent_at,last_sent_at,created_at,updated_at) "
                "VALUES (?, 'a','accepted',1,1,?,?,?,?)", (eid, now, now, now, now))
        task["event_id"] = eid
        return task

    def test_migrated_sent_task_needs_manual_owner_and_rejects_route_rewrite(self) -> None:
        task = self.legacy_task(sent=True)
        migrated = self.ledger.migrate()
        self.assertTrue(Path(migrated["backup"]).exists())
        self.assertEqual(self.ledger.notifier_cutoff(), "")
        self.config(default="b", consumer_a="new-owner", chat_a="new-chat")
        self.assertEqual(self.ledger.bind_notification_owners(notify.load_config(self.home)), 0)
        event = self.events()[0]
        self.assertEqual(event["owner_state"], "manual_restore")
        self.assertIsNone(event["owner_consumer"])
        self.assertEqual(self.flush()["mock_send_count"], 0)
        wrong = self.command("notify", "bind-owner", "--task", task["task_id"], "--route", "b",
                             "--consumer", "old-owner", "--chat", "old-chat", expected=6)
        self.assertEqual(wrong["error"], "historical_route_mismatch")
        self.command("notify", "bind-owner", "--task", task["task_id"], "--route", "a",
                     "--consumer", "old-owner", "--chat", "old-chat")
        owner = self.ledger.task_notification_owners([task["task_id"]])[task["task_id"]]
        self.assertEqual((owner["route"], owner["consumer"], owner["chat"]), ("a", "old-owner", "old-chat"))

    def test_manual_restore_repairs_only_unsent_unresolved_placeholder(self) -> None:
        task = self.legacy_task()
        self.ledger.migrate()
        self.config()
        self.assertEqual(self.flush()["mock_send_count"], 0)
        before = self.ledger.conn.execute("SELECT route,state FROM notifications").fetchone()
        self.assertEqual(tuple(before), ("-", "failed"))
        self.command("notify", "bind-owner", "--task", task["task_id"], "--route", "a",
                     "--consumer", "consumer-a", "--chat", "chat-a")
        after = self.ledger.conn.execute("SELECT route,state FROM notifications").fetchone()
        self.assertEqual(tuple(after), ("a", "failed"))
        self.command("notify", "retry", "--event", task["event_id"])
        self.assertEqual(self.flush()["mock_send_count"], 1)
        again = self.command("notify", "bind-owner", "--task", task["task_id"], "--route", "b",
                             "--consumer", "consumer-b", "--chat", "chat-b", expected=6)
        self.assertEqual(again["error"], "owner_not_unresolved")


if __name__ == "__main__":
    unittest.main()

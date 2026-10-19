"""Regression counterexamples: synthetic ledger events and loopback HTTP, no native CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401
from notify_helpers import FakeWebhook, http_config, write_config
from cli_relay import cli, notify
from cli_relay.errors import LedgerWriteFailed
from cli_relay.ledger import Ledger, init_ledger


class NotifierFaults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-notifier-fault-")
        self.root = Path(self.temp.name)
        self.home = self.root / "ledger"
        init_ledger(self.home)
        ledger = Ledger(self.home)
        _, task, _ = ledger.submit(scope="audit", key="one", payload={"engine": "fixture", "cwd": str(self.root),
                                  "prompt": "synthetic", "options": {}}, prompt="synthetic", native_session_id=None)
        attempt = ledger.current_attempt(task["task_id"])
        ledger.finish(attempt["attempt_id"], from_states=("pending",), outcome="succeeded", exit_known=True,
                      exit_code=0, native={"ok": True, "result": "synthetic; no CLI was launched"})
        self.event = ledger.events_for("bot")[0]
        ledger.close()
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        self.temp.cleanup()

    def config(self, behaviours=None, *, retry_after="1", **kwargs):
        hook = FakeWebhook(behaviours, retry_after=retry_after)
        self.servers.append(hook)
        cfg = http_config(self.root, hook.url, **kwargs)
        write_config(self.home, cfg)
        return hook, cfg

    def flush(self):
        return notify.flush(self.home, env={})

    def row(self):
        return notify.status(self.home)[0]

    def due_now(self):
        ledger = Ledger(self.home)
        with ledger.tx() as c:
            c.execute("UPDATE notifications SET next_at='2000-01-01T00:00:00.000Z'")
        ledger.close()

    def ack(self):
        ledger = Ledger(self.home)
        ledger.ack(task_id=self.event["task_id"], attempt_no=self.event["attempt_no"],
                   event_id=self.event["event_id"], consumer="bot")
        ledger.close()

    def test_invalid_port_is_recorded_and_repair_needs_no_process_restart(self):
        hook, cfg = self.config()
        url_file = Path(cfg["routes"]["bot"]["adapter"]["url_file"])
        url_file.write_text("http://127.0.0.1:bad/hook")
        self.flush()
        self.assertEqual((self.row()["state"], self.row()["last_result"]["why"]), ("failed", "url_invalid"))
        self.assertEqual(hook.requests, [])
        url_file.write_text(hook.url)
        self.assertEqual(notify.retry(self.home, self.event["event_id"]), 1)
        self.flush()
        self.assertEqual(self.row()["state"], "accepted")
        self.assertEqual(len(hook.requests), 1)

    def test_malformed_token_never_reaches_cli_output_or_send_records(self):
        hook, cfg = self.config()
        secret = "SYNTHETIC_TOKEN_MUST_NOT_BE_LOGGED"
        Path(cfg["routes"]["bot"]["adapter"]["token_file"]).write_text(secret + "\nsecond-line")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = cli.main(["--home", str(self.home), "notify", "flush"])
        self.assertEqual(code, 0)
        self.assertEqual(self.row()["last_result"]["why"], "token_invalid")
        self.assertNotIn(secret, captured.getvalue())
        self.assertNotIn(secret, json.dumps(self.row()))
        self.assertEqual(hook.requests, [])
        ledger = Ledger(self.home)
        details = [r[0] for r in ledger.conn.execute("SELECT detail_json FROM notification_sends")]
        self.assertNotIn(secret, json.dumps(details))
        ledger.close()

    def test_non_utf8_secret_is_a_recorded_failure(self):
        hook, cfg = self.config()
        Path(cfg["routes"]["bot"]["adapter"]["token_file"]).write_bytes(b"\xff\xfe")
        self.flush()
        self.assertEqual(self.row()["last_result"]["why"], "credentials_unavailable")
        self.assertEqual(self.row()["state"], "failed")
        self.assertEqual(hook.requests, [])

    def test_unexpected_adapter_error_is_redacted_and_releases_the_claim(self):
        hook, _ = self.config()
        secret = "synthetic-exception-secret"
        with patch.object(notify, "send_http", side_effect=ValueError(secret)):
            self.flush()
        row = self.row()
        self.assertEqual((row["state"], row["wakes"]), ("unconfirmed", 1))
        self.assertNotIn(secret, json.dumps(row))
        self.due_now()
        self.flush()
        self.assertEqual((self.row()["state"], len(hook.requests)), ("accepted", 1))

    def test_failed_result_write_recovers_own_abandoned_claim_without_resending(self):
        hook, _ = self.config()
        with patch.object(notify, "_record", side_effect=LedgerWriteFailed("synthetic write failure")):
            with self.assertRaises(LedgerWriteFailed):
                self.flush()
        self.assertEqual(self.row()["state"], "sending")
        second = self.flush()
        self.assertEqual((second["unconfirmed"], self.row()["state"], self.row()["wakes"]), (1, "unconfirmed", 1))
        self.assertEqual(len(hook.requests), 1)
        self.ack()
        self.flush()
        self.assertEqual((self.row()["state"], len(hook.requests)), ("acked", 1))

    def test_abandoned_claim_before_send_is_safe_to_retry_in_same_process(self):
        hook, _ = self.config()
        with patch.object(notify, "build_payload", side_effect=RuntimeError("synthetic payload failure")):
            with self.assertRaises(RuntimeError):
                self.flush()
        self.assertEqual(self.row()["state"], "claimed")
        self.assertEqual(hook.requests, [])
        self.flush()
        self.assertEqual((self.row()["state"], len(hook.requests)), ("accepted", 1))

    def test_live_concurrent_pass_in_same_process_is_not_reclaimed(self):
        self.config()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked_send(*args):
            calls.append(1)
            entered.set()
            if not release.wait(10):
                raise AssertionError("test did not release sender")
            return {"outcome": "accepted", "why": "test"}

        with patch.object(notify, "send_http", side_effect=blocked_send), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.flush)
            try:
                self.assertTrue(entered.wait(10))
                second = self.flush()
                self.assertEqual((second["reclaimed"], second["unconfirmed"], second["sends"]), (0, 0, []))
            finally:
                release.set()
            first.result(timeout=10)
        self.assertEqual((calls, self.row()["state"]), ([1], "accepted"))

    def test_ack_before_dead_claim_recovery_prevents_the_post(self):
        hook, _ = self.config()
        ledger = Ledger(self.home)
        notify.enqueue(ledger, notify.load_config(self.home))
        with ledger.tx() as c:
            c.execute("UPDATE notifications SET state='claimed', claimed_by=?, claimed_token='dead', claim_id='dead'",
                      (os.getpid(),))
        ledger.close()
        self.ack()
        result = self.flush()
        self.assertEqual((result["reclaimed"], result["acked"], self.row()["state"]), (1, 1, "acked"))
        self.assertEqual(hook.requests, [])

    def test_retry_after_is_a_minimum_not_a_backoff_cap(self):
        self.config(["429"], retry_after="3600")
        self.flush()
        due = datetime.fromisoformat(self.row()["next_at"].replace("Z", "+00:00"))
        self.assertGreater((due - datetime.now(timezone.utc)).total_seconds(), 3598)
        date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
        self.assertGreater(notify._retry_after(date), 118)

    def test_http_errors_consume_the_possible_wake_budget(self):
        hook, _ = self.config(["500"] * 3, max_attempts=6, max_wakes=1)
        self.flush()
        self.due_now()
        self.flush()
        self.assertEqual((len(hook.requests), self.row()["attempts"], self.row()["wakes"]), (1, 1, 1))
        self.assertEqual(self.row()["state"], "failed")

    def test_nonzero_command_may_have_delivered_and_uses_wake_budget(self):
        marker = self.root / "command-calls"
        program = self.root / "notify.sh"
        program.write_text(f"#!/bin/sh\necho called >> '{marker}'\nexit 1\n")
        program.chmod(0o700)
        cfg = http_config(self.root, "http://unused.invalid", max_wakes=1)
        cfg["routes"]["bot"]["adapter"] = {"type": "command", "argv": [str(program)]}
        write_config(self.home, cfg)
        self.flush()
        self.assertEqual((self.row()["state"], self.row()["wakes"]), ("unconfirmed", 1))
        self.due_now()
        self.flush()
        self.assertEqual((self.row()["state"], marker.read_text().splitlines()), ("unacknowledged", ["called"]))

    def test_reducing_budget_does_not_send_an_already_pending_retry(self):
        hook, cfg = self.config(["500"], max_wakes=2)
        self.flush()
        self.assertEqual((self.row()["state"], self.row()["wakes"]), ("pending", 1))
        cfg["reminder"]["max_wakes"] = 1
        write_config(self.home, cfg)
        self.due_now()
        self.flush()
        self.assertEqual((len(hook.requests), self.row()["state"]), (1, "failed"))


if __name__ == "__main__":
    unittest.main()

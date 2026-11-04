"""C6: one durable fallback attempt, original ownership and bounded restart behavior."""

from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from direct_route_helpers import DirectRouteCase
from notify_helpers import FakeHTTPReceiver
from cli_relay import notify, routes

PAST = "2000-01-01T00:00:00.000Z"


class FallbackReminders(DirectRouteCase):
    def configure(self, *, fallback=True, max_wakes=1):
        for name in ("friday", "bot"):
            self.add(name)
            self.prove(name)
        cfg = notify.load_config(self.home)
        cfg["reminder"]["max_wakes"] = max_wakes
        routes._save(self.home, cfg)
        if fallback:
            self.command("route", "set-fallback", "bot", "friday")

    def age(self, *, next_at=True):
        with self.ledger.tx() as c:
            c.execute("UPDATE notifications SET first_sent_at=?", (PAST,))
            if next_at:
                c.execute("UPDATE notifications SET next_at=? WHERE next_at IS NOT NULL", (PAST,))

    def ack(self, task, event):
        self.command("ack", "--consumer", "bot", "--task", task["task_id"], "--attempt", "1", "--event", event,
                     "--message-id", "bot-delivered", "--chat", "bot-chat")

    def test_timeout_sends_exactly_one_marked_fallback_and_no_proxy_ack(self):
        self.configure()
        task, event = self.finish(route="bot")
        with FakeHTTPReceiver() as receiver:
            self.command("notify", "flush")
            self.assertEqual(len(receiver.requests), 1)
            self.age()
            result = self.command("notify", "flush")
            for _ in range(4):
                self.command("notify", "flush")
        self.assertEqual(len(receiver.requests), 2)
        fallback = receiver.requests[1]
        self.assertEqual(fallback["status"], "fallback_reminder")
        self.assertIn("FALLBACK REMINDER", fallback["summary"])
        self.assertIn(event, fallback["summary"])
        record = fallback["cli_relay"]
        self.assertTrue(record["fallback"])
        self.assertEqual((record["route"], record["consumer"], record["original_route"]), ("friday", "friday", "bot"))
        ref = record["events"][0]
        self.assertEqual((ref["original_consumer"], ref["original_chat"], ref["fallback_reason"]),
                         ("bot", "bot-chat", "ack_timeout"))
        self.assertGreaterEqual(ref["notification_age_s"], 1800)
        self.assertEqual(result["fallback_sends"][0]["events"], [event])
        state = self.command("notify", "status")["notifications"][0]
        self.assertEqual(state["state"], "unacknowledged")
        self.assertEqual((state["fallback"]["route"], state["fallback"]["state"]), ("friday", "accepted"))
        self.assertFalse(state["consumer_acked"])
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
        self.assertEqual(self.command("brief", "--consumer", "friday")["events"], [])
        self.assertEqual(len(self.command("brief", "--consumer", "bot")["events"]), 1)
        self.assert_no_secret()

    def test_ack_in_time_or_before_first_pass_prevents_fallback(self):
        self.configure()
        first = self.finish("first", route="bot")
        with FakeHTTPReceiver() as receiver:
            self.command("notify", "flush")
            self.ack(*first)
            self.age()
            second = self.finish("second", route="bot")
            self.ack(*second)
            self.command("notify", "flush")
        self.assertEqual(len(receiver.requests), 1)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM notification_fallbacks").fetchone()[0], 0)

    def test_no_fallback_route_preserves_existing_behavior(self):
        self.configure(fallback=False)
        self.finish(route="bot")
        with FakeHTTPReceiver() as receiver:
            self.command("notify", "flush")
            self.age()
            for _ in range(3):
                self.assertEqual(self.command("notify", "flush")["fallback_sends"], [])
        self.assertEqual(len(receiver.requests), 1)
        self.assertEqual(self.command("notify", "status")["notifications"][0]["state"], "unacknowledged")

    def test_failed_original_send_triggers_immediate_fallback_without_retries(self):
        self.configure()
        self.finish(route="bot")
        with FakeHTTPReceiver([401, {}]) as receiver:
            result = self.command("notify", "flush")
            for _ in range(3):
                self.command("notify", "flush")
        self.assertEqual(len(receiver.requests), 2)
        self.assertEqual(receiver.requests[-1]["cli_relay"]["events"][0]["fallback_reason"], "send_failed")
        self.assertEqual(len(result["fallback_sends"]), 1)

    def test_failed_fallback_is_final_even_after_original_notification_retry(self):
        self.configure()
        task, event = self.finish(route="bot")
        with FakeHTTPReceiver([401, 500]) as receiver:
            self.command("notify", "flush")
            self.command("notify", "retry", "--event", event)
            self.command("notify", "flush")
            self.age()
            self.command("notify", "flush")
        self.assertEqual(sum(r["status"] == "fallback_reminder" for r in receiver.requests), 1)
        self.assertEqual(self.command("notify", "status")["notifications"][0]["fallback"]["state"], "failed")

    def test_unproven_self_and_missing_fallback_are_refused(self):
        self.add()
        self.add("unproven")
        for target, error in (("bot", "fallback_self"), ("unproven", "route_unproven"),
                              ("missing", "route_not_configured")):
            self.assertEqual(self.command("route", "set-fallback", "bot", target, expected=6)["error"], error)
        self.command("route", "add", "new", "--consumer", "new", "--chat", "new-chat", "--url-env", "ROUTE_URL",
                     "--token-env", "ROUTE_TOKEN", "--fallback-route", "unproven", expected=6)
        self.assertNotIn("new", notify.load_config(self.home)["routes"])

    def test_add_fallback_clear_and_legacy_friday_default_preserve_proof_rules(self):
        raw = {"source_id": "test-mac", "default_route": "friday", "routes": {
            "friday": {"consumer": "friday", "chat": "friday-chat", "adapter": {
                "type": "http", "url_env": "ROUTE_URL", "token_env": "ROUTE_TOKEN"}}}}
        (self.home / "notifier.json").write_text(json.dumps(raw))
        self.add(fallback="friday")
        cfg = notify.load_config(self.home)
        self.assertEqual(cfg["default_route"], "friday")
        self.assertNotIn("verification", cfg["routes"]["friday"])
        self.assertTrue(cfg["routes"]["friday"]["proven"])
        self.assertFalse(cfg["routes"]["bot"]["proven"])
        self.assertEqual(cfg["routes"]["bot"]["fallback_route"], "friday")
        self.command("route", "remove", "friday", expected=6)
        self.command("route", "set-fallback", "bot", "--clear")
        self.assertNotIn("fallback_route", notify.load_config(self.home)["routes"]["bot"])
        self.assertEqual(notify.load_config(self.home)["default_route"], "friday")

    def test_fallback_waits_for_proof_if_target_loses_it(self):
        self.configure()
        self.finish(route="bot")
        with FakeHTTPReceiver() as receiver:
            self.command("notify", "flush")
            self.age()
            cfg = notify.load_config(self.home)
            cfg["routes"]["friday"]["verification"] = None
            routes._save(self.home, cfg)
            self.assertEqual(self.command("notify", "flush")["fallback_sends"], [])
            self.prove("friday")
            self.command("notify", "flush")
        self.assertEqual(len(receiver.requests), 2)
        self.assertEqual(receiver.requests[-1]["status"], "fallback_reminder")

    def test_target_reregistered_during_original_send_waits_for_new_proof(self):
        # Start with a proven legacy target so registration can revoke its proof through
        # the same management operation an operator uses during a running notifier pass.
        routes._save(self.home, notify.validate_config({"source_id": "test-mac", "routes": {
            "friday": {"consumer": "friday", "chat": "friday-chat", "adapter": {
                "type": "http", "url_env": "ROUTE_URL", "token_env": "ROUTE_TOKEN"}}}}))
        self.add(fallback="friday")
        self.prove()
        _, event = self.finish(route="bot")

        def revoke_proof(request):
            self.assertEqual(request["route"], "bot")
            self.add("friday")
            self.assertFalse(notify.load_config(self.home)["routes"]["friday"]["proven"])
            return 401

        with FakeHTTPReceiver([revoke_proof]) as receiver:
            result = self.command("notify", "flush")
            self.assertEqual(result["fallback_sends"], [])
            self.assertEqual(self.command("notify", "flush")["fallback_sends"], [])
            self.assertEqual(len(receiver.requests), 1)
            self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM notification_fallbacks").fetchone()[0], 0)
            self.prove("friday")
            recovered = self.command("notify", "flush")
            self.assertEqual(recovered["fallback_sends"][0]["events"], [event])
            self.assertEqual(self.command("notify", "flush")["fallback_sends"], [])
        self.assertEqual(len(receiver.requests), 2)
        self.assertEqual(receiver.requests[-1]["status"], "fallback_reminder")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM notification_fallbacks").fetchone()[0], 1)
        self.assertEqual(self.command("notify", "status")["notifications"][0]["fallback"]["state"], "accepted")
        self.assert_no_secret()

    def test_initial_timeout_is_not_postponed_by_an_original_route_reminder(self):
        self.configure(max_wakes=2)
        self.finish(route="bot")
        with FakeHTTPReceiver() as receiver:
            self.command("notify", "flush")
            self.age()
            self.command("notify", "flush")
        self.assertEqual([r["cli_relay"]["route"] for r in receiver.requests], ["bot", "bot", "friday"])
        self.assertTrue(receiver.requests[1]["cli_relay"]["reminder"])

    def test_crash_after_claim_is_recovered_and_sends_once(self):
        self.configure()
        self.finish(route="bot")
        with FakeHTTPReceiver() as receiver:
            self.command("notify", "flush")
            self.age()

            def crash(name):
                if name == "fallback_after_claim":
                    raise SystemExit(91)

            with mock.patch.object(notify, "_hook", side_effect=crash), self.assertRaises(SystemExit):
                notify.flush(self.home)
            self.command("notify", "flush")
            self.command("notify", "flush")
        self.assertEqual(len(receiver.requests), 2)
        self.assertEqual(self.command("notify", "status")["notifications"][0]["fallback"]["state"], "accepted")

    def test_crash_after_send_is_unconfirmed_and_never_sends_a_second_fallback(self):
        self.configure()
        self.finish(route="bot")
        with FakeHTTPReceiver() as receiver:
            self.command("notify", "flush")
            self.age()

            def crash(name):
                if name == "fallback_after_send":
                    raise SystemExit(91)

            with mock.patch.object(notify, "_hook", side_effect=crash), self.assertRaises(SystemExit):
                notify.flush(self.home)
            for _ in range(3):
                self.command("notify", "flush")
        self.assertEqual(len(receiver.requests), 2)
        self.assertEqual(self.command("notify", "status")["notifications"][0]["fallback"]["state"], "unconfirmed")

    def test_concurrent_passes_batch_events_without_duplicate_fallbacks(self):
        self.configure()
        events = [self.finish(str(i), route="bot")[1] for i in range(3)]
        with FakeHTTPReceiver() as receiver:
            notify.flush(self.home)
            self.age()
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda _: notify.flush(self.home), range(3)))
        self.assertEqual(len(receiver.requests), 2)
        self.assertEqual({e["event_id"] for e in receiver.requests[-1]["cli_relay"]["events"]}, set(events))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM notification_fallbacks").fetchone()[0], 3)

    def test_original_ack_during_fallback_claim_suppresses_the_send(self):
        self.configure()
        task, event = self.finish(route="bot")
        with FakeHTTPReceiver() as receiver:
            self.command("notify", "flush")
            self.age()

            def ack_after_claim(name):
                if name == "fallback_after_claim":
                    self.ack(task, event)

            with mock.patch.object(notify, "_hook", side_effect=ack_after_claim):
                result = notify.flush(self.home)
        self.assertEqual(result["fallback_sends"], [])
        self.assertEqual(len(receiver.requests), 1)
        self.assertEqual(self.command("notify", "status")["notifications"][0]["fallback"]["state"], "skipped")

    def test_original_send_crash_retains_send_age_for_fallback_recovery(self):
        self.configure()
        self.finish(route="bot")

        def crash(name):
            if name == "notify_after_send":
                raise SystemExit(91)

        with FakeHTTPReceiver() as receiver:
            with mock.patch.object(notify, "_hook", side_effect=crash), self.assertRaises(SystemExit):
                notify.flush(self.home)
            with self.ledger.tx() as c:
                c.execute("UPDATE notifications SET updated_at=?", (PAST,))
            self.command("notify", "flush")
        self.assertEqual(len(receiver.requests), 2)
        self.assertEqual(receiver.requests[-1]["status"], "fallback_reminder")


if __name__ == "__main__":
    unittest.main()

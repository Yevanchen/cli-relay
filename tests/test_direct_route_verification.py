"""C1: asynchronous proof, identity/expiry checks and ledger-backed authorization."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from direct_route_helpers import DirectRouteCase, TOKEN
from notify_helpers import FakeHTTPReceiver
from cli_relay import notify, routes


class AsyncRouteVerification(DirectRouteCase):
    def pending(self):
        self.add()
        with FakeHTTPReceiver() as receiver:
            result = self.command("route", "verify", "bot", expected=6)
        self.assertEqual(result["verification"]["state"], "awaiting_confirmation")
        self.assertFalse(result["proven"])
        self.assertRegex(result["verification_id"], r"^v-[a-f0-9]{32}$")
        self.assertIn(routes.TEST_MESSAGE, receiver.requests[0]["summary"])
        self.assertEqual(receiver.requests[0]["cli_relay"]["test_message"], routes.TEST_MESSAGE)
        return result

    def confirm(self, proof, *extra, expected=0):
        return self.command("route", "confirm", "bot", "--verification-id", proof["verification_id"],
                            "--message-id", "visible-123", *extra, expected=expected)

    def test_bare_2xx_pending_then_confirm_records_message_and_enables_task(self):
        pending = self.pending()
        self.command("route", "set-default", "bot", expected=6)
        with mock.patch.object(notify, "send") as send:
            confirmed = self.confirm(pending, "--source-id", "test-mac", "--ledger-id", self.ledger.ledger_id,
                                     "--consumer", "bot", "--chat", "bot-chat")
            self.confirm(pending)
        send.assert_not_called()
        self.assertTrue(confirmed["proven"])
        self.assertEqual(confirmed["verification"]["message_id"], "visible-123")
        stored = json.loads(self.ledger.conn.execute("SELECT receipt_json FROM route_verifications").fetchone()[0])
        self.assertEqual(stored, confirmed["verification"])
        self.command("route", "set-default", "bot")
        self.assertEqual(self.finish()[0]["notify_consumer"], "bot")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
        self.assert_no_secret()

    def test_confirm_recovers_committed_receipt_after_config_rename_failure(self):
        for failure in (SystemExit(91), OSError("synthetic rename failure")):
            with self.subTest(failure=type(failure).__name__):
                self.command("route", "remove", "bot")
                pending = self.pending()
                path = self.home / notify.CONFIG_NAME
                before = path.read_bytes()

                def fail_rename(src, dst):
                    self.assertEqual(str(path), dst)
                    stored = routes._stored_verification(self.home, {"name": "bot"})
                    self.assertEqual(stored["state"], "acked")
                    self.assertEqual(stored["message_id"], "visible-123")
                    raise failure

                with mock.patch("cli_relay.ledger.os.replace", side_effect=fail_rename), \
                        self.assertRaises(type(failure)):
                    routes.confirm(self.home, "bot", verification_id=pending["verification_id"],
                                   message_id="visible-123")
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(notify.load_config(self.home)["routes"]["bot"]["proven"])
                committed = routes._stored_verification(self.home, {"name": "bot"})
                conflict = self.command("route", "confirm", "bot", "--verification-id", pending["verification_id"],
                                        "--message-id", "different-message", expected=6)
                self.assertEqual(conflict["error"], "verification_mismatch")
                with mock.patch.object(notify, "send") as send:
                    confirmed = self.confirm(pending)
                    self.assertEqual(self.confirm(pending), confirmed)
                send.assert_not_called()
                self.assertTrue(confirmed["proven"])
                self.assertFalse(confirmed["sent"])
                self.assertEqual(confirmed["verification"], committed)
                recovered = notify.load_config(self.home)["routes"]["bot"]
                self.assertTrue(recovered["proven"])
                self.assertEqual(recovered["verification"], committed)
                self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
                self.assert_no_secret()

    def test_async_acceptance_survives_failed_reminder_and_delayed_confirmation(self):
        pending = self.pending()
        with mock.patch.object(routes, "now_iso", return_value=pending["verification"]["next_at"]), \
                FakeHTTPReceiver([401]) as receiver:
            failed = self.command("route", "verify", "bot", expected=6)
        self.assertEqual(failed["verification_id"], pending["verification_id"])
        self.assertEqual(failed["verification"]["state"], "failed")
        self.assertEqual(len(receiver.requests), 1)
        self.assertFalse(failed["proven"])
        with mock.patch.object(routes, "now_iso", return_value=notify._later(7200)), \
                mock.patch.object(notify, "send") as send:
            confirmed = self.confirm(pending)
        send.assert_not_called()
        self.assertTrue(confirmed["proven"])
        self.assertTrue(confirmed["verification"]["async_accepted"])
        stored = json.loads(self.ledger.conn.execute("SELECT receipt_json FROM route_verifications").fetchone()[0])
        self.assertEqual(stored, confirmed["verification"])
        self.assertTrue(notify.load_config(self.home)["routes"]["bot"]["proven"])
        self.assert_no_secret()

    def test_older_async_receipt_retains_acceptance_when_reminder_fails(self):
        pending = self.pending()
        cfg = notify.load_config(self.home)
        cfg["routes"]["bot"]["verification"].pop("async_accepted", None)
        routes._store_verification(self.home, cfg["routes"]["bot"])
        routes._save(self.home, cfg)
        with mock.patch.object(routes, "now_iso", return_value=pending["verification"]["next_at"]), \
                FakeHTTPReceiver([401]):
            self.command("route", "verify", "bot", expected=6)
        self.assertTrue(self.confirm(pending)["proven"])

    def test_failed_probe_and_new_retry_do_not_borrow_async_acceptance(self):
        pending = self.pending()
        with mock.patch.object(routes, "now_iso", return_value=pending["verification"]["next_at"]), \
                FakeHTTPReceiver([401, 401]):
            self.command("route", "verify", "bot", expected=6)
            newer = self.command("route", "verify", "bot", "--retry", expected=6)
        self.assertNotEqual(newer["verification_id"], pending["verification_id"])
        self.assertEqual(self.confirm(pending, expected=6)["error"], "verification_mismatch")
        self.assertEqual(self.confirm(newer, expected=6)["error"], "verification_not_confirmable")

    def test_wrong_id_route_and_asserted_identity_refused(self):
        pending = self.pending()
        self.command("route", "confirm", "bot", "--verification-id", "v-" + "0" * 32,
                     "--message-id", "m1", expected=6)
        self.add("other")
        self.command("route", "confirm", "other", "--verification-id", pending["verification_id"],
                     "--message-id", "m1", expected=6)
        for flag in ("--source-id", "--ledger-id", "--consumer", "--chat"):
            self.assertEqual(self.confirm(pending, flag, "wrong", expected=6)["error"], "verification_mismatch")
        self.assertFalse(notify.load_config(self.home)["routes"]["bot"]["proven"])

    def test_expired_id_refused_and_retry_replaces_it(self):
        pending = self.pending()
        with mock.patch.object(routes, "now_iso", return_value=pending["verification"]["expires_at"]):
            self.assertEqual(self.confirm(pending, expected=6)["error"], "verification_expired")
            with FakeHTTPReceiver():
                newer = self.command("route", "verify", "bot", "--retry", expected=6)
        self.assertNotEqual(newer["verification_id"], pending["verification_id"])
        self.assertEqual(self.confirm(pending, expected=6)["error"], "verification_mismatch")

    def test_changed_destination_or_source_cannot_borrow_pending_confirmation(self):
        pending = self.pending()
        path = self.home / "notifier.json"
        original = json.loads(path.read_text())
        for target, key, value in (("root", "source_id", "another-mac"), ("route", "consumer", "other"),
                                   ("route", "chat", "another-chat"), ("adapter", "token_env", "OTHER_TOKEN")):
            cfg = json.loads(json.dumps(original))
            obj = cfg if target == "root" else cfg["routes"]["bot"]
            if target == "adapter":
                obj = obj["adapter"]
            obj[key] = value
            path.write_text(json.dumps(cfg))
            self.assertEqual(self.confirm(pending, expected=6)["error"], "verification_mismatch")

    def test_async_wake_budget_is_bounded_and_stale_id_is_not_confirmable(self):
        self.add()
        cfg = notify.load_config(self.home)
        cfg["reminder"]["max_wakes"] = 1
        routes._save(self.home, cfg)
        with FakeHTTPReceiver() as receiver:
            first = self.command("route", "verify", "bot", expected=6)
            self.assertFalse(self.command("route", "verify", "bot", expected=6)["sent"])
            with mock.patch.object(routes, "now_iso", return_value=first["verification"]["next_at"]):
                last = self.command("route", "verify", "bot", expected=6)
            for _ in range(3):
                self.command("route", "verify", "bot", expected=6)
            self.assertEqual(last["verification"]["state"], "unacknowledged")
            self.assertEqual(len(receiver.requests), 1)
            second = self.command("route", "verify", "bot", "--retry", expected=6)
        self.assertNotEqual(first["verification_id"], second["verification_id"])
        self.confirm(first, expected=6)
        self.confirm(second)

    def test_synchronous_receipt_still_proves_without_confirm(self):
        self.add()
        with FakeHTTPReceiver([lambda req: {**req, "visible": True, "message_id": "sync-visible"}]) as receiver:
            proof = self.command("route", "verify", "bot")
            self.assertFalse(self.command("route", "verify", "bot")["sent"])
        self.assertEqual(proof["verification"]["message_id"], "sync-visible")
        self.assertEqual(len(receiver.requests), 1)

    def test_invalid_sync_identity_never_becomes_async_confirmable(self):
        self.add()
        with FakeHTTPReceiver([lambda req: {**req, "consumer": "wrong", "visible": True, "message_id": "m1"}]):
            proof = self.command("route", "verify", "bot", expected=6)
        self.assertEqual(proof["verification"]["state"], "unconfirmed")
        self.assertEqual(self.confirm(proof, expected=6)["error"], "verification_not_confirmable")

    def test_editing_config_receipt_cannot_prove_a_managed_route(self):
        pending = self.pending()
        path = self.home / "notifier.json"
        cfg = json.loads(path.read_text())
        cfg["routes"]["bot"]["verification"].update(state="acked", message_id="forged", acked_at="2026-11-05T00:00:00.000Z")
        path.write_text(json.dumps(cfg))
        self.assertFalse(self.command("route", "list")["routes"][0]["proven"])
        self.command("route", "set-default", "bot", expected=6)
        self.assertEqual(self.confirm(pending, expected=6)["error"], "verification_mismatch")
        # The authoritative budget cannot be reset by editing the config either.
        cfg["routes"]["bot"]["verification"].update(state="awaiting_confirmation", message_id=None, acked_at=None,
                                                  attempts=0, wakes=0, next_at="2000-01-01T00:00:00.000Z")
        path.write_text(json.dumps(cfg))
        with FakeHTTPReceiver() as receiver:
            result = self.command("route", "verify", "bot", expected=6)
        self.assertFalse(result["sent"])
        self.assertEqual(receiver.requests, [])

    def test_invalid_or_secret_message_id_is_not_written(self):
        pending = self.pending()
        for value in ("", "bad\n", TOKEN):
            self.command("route", "confirm", "bot", "--verification-id", pending["verification_id"],
                         "--message-id", value, expected=2)
        self.assert_no_secret()

    def test_removing_verification_key_does_not_give_registered_route_legacy_proof(self):
        self.add()
        path = self.home / "notifier.json"
        cfg = json.loads(path.read_text())
        cfg["routes"]["bot"].pop("verification")
        path.write_text(json.dumps(cfg))
        self.assertFalse(self.command("route", "list")["routes"][0]["proven"])
        self.command("route", "set-default", "bot", expected=6)
        with FakeHTTPReceiver():
            pending = self.command("route", "verify", "bot", expected=6)
        self.confirm(pending)

    def test_old_proof_cannot_be_restored_after_remove_and_reregister(self):
        pending = self.pending()
        self.confirm(pending)
        path = self.home / "notifier.json"
        old = json.loads(path.read_text())["routes"]["bot"]["verification"]
        self.command("route", "remove", "bot")
        self.add()
        cfg = json.loads(path.read_text())
        cfg["routes"]["bot"]["verification"] = old
        path.write_text(json.dumps(cfg))
        self.assertFalse(self.command("route", "list")["routes"][0]["proven"])
        self.confirm(pending, expected=6)
        with FakeHTTPReceiver():
            newer = self.command("route", "verify", "bot", expected=6)
        self.assertNotEqual(newer["verification_id"], pending["verification_id"])

    def test_replacing_config_with_superseded_probe_cannot_reset_the_current_id(self):
        pending = self.pending()
        path = self.home / "notifier.json"
        old = json.loads(path.read_text())["routes"]["bot"]["verification"]
        with mock.patch.object(routes, "now_iso", return_value=pending["verification"]["expires_at"]), FakeHTTPReceiver():
            newer = self.command("route", "verify", "bot", "--retry", expected=6)
        cfg = json.loads(path.read_text())
        cfg["routes"]["bot"]["verification"] = old
        path.write_text(json.dumps(cfg))
        self.confirm(pending, expected=6)
        with FakeHTTPReceiver() as receiver:
            current = self.command("route", "verify", "bot", expected=6)
        self.assertEqual(current["verification_id"], newer["verification_id"])
        self.assertFalse(current["sent"])
        self.assertEqual(receiver.requests, [])
        self.confirm(newer)


class SchemaTwoRouteMigration(DirectRouteCase):
    def test_migration_registers_historical_managed_routes_without_importing_proofs(self):
        self.add("unverified")
        self.add()
        with FakeHTTPReceiver():
            self.command("route", "verify", "bot", expected=6)
        self.add("confirmed")
        self.prove("confirmed")
        cfg = notify.load_config(self.home)
        cfg["routes"]["friday"] = notify._route("friday", {
            "consumer": "friday", "chat": "friday-chat",
            "adapter": {"type": "command", "argv": ["/bin/true"]}})
        for route in cfg["routes"].values():
            if route.get("verification"):
                route["verification"].pop("async_accepted", None)
        routes._save(self.home, cfg)
        with self.ledger.tx() as c:
            for table in ("managed_routes", "route_verifications", "notification_fallbacks"):
                c.execute(f"DROP TABLE {table}")
            c.execute("UPDATE meta SET v='2' WHERE k='notifier_schema'")
        path = self.home / notify.CONFIG_NAME
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        cutoff = self.ledger.notifier_cutoff()
        with mock.patch.object(notify, "_secret", side_effect=AssertionError("migration resolved a secret")), \
                mock.patch.object(notify, "send") as send:
            migrated = self.command("migrate")
        send.assert_not_called()
        self.assertTrue(migrated["migrated"])
        self.assertEqual(self.ledger.notifier_cutoff(), cutoff)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        managed = dict(self.ledger.conn.execute("SELECT route, verification_id FROM managed_routes").fetchall())
        self.assertEqual(managed, {"unverified": None, "bot": None, "confirmed": None})
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM route_verifications").fetchone()[0], 0)
        cfg = notify.load_config(self.home)
        self.assertTrue(cfg["routes"]["friday"]["proven"])
        for name in managed:
            self.assertFalse(cfg["routes"][name]["proven"])
            cfg["routes"][name].pop("verification")
        routes._save(self.home, cfg)
        cfg = notify.load_config(self.home)
        for name in managed:
            self.assertFalse(cfg["routes"][name]["proven"])
            self.command("route", "set-default", name, expected=6)
        self.assertTrue(cfg["routes"]["friday"]["proven"])
        self.assertFalse(self.command("migrate")["migrated"])
        self.prove()
        self.assertFalse(self.command("migrate")["migrated"])
        self.assertTrue(notify.load_config(self.home)["routes"]["bot"]["proven"])
        self.assert_no_secret()


if __name__ == "__main__":
    unittest.main()

"""Issue #4: mock transports only, temporary homes, no real URL or credential access."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from helpers import RelayTestCase, db_rows, side_effects
from notify_helpers import http_config, write_config
from cli_relay import cli, notify, routes
from cli_relay.ledger import Ledger, init_ledger, now_iso, payload_hash

TOKEN = "SYNTHETIC_SECRET_NEVER_PERSIST_77"
URL = "http://127.0.0.1:1/SYNTHETIC_PRIVATE_URL_88"
ENV = {"ROUTE_URL": URL, "ROUTE_TOKEN": TOKEN}
ORIGIN = {"bot": "github-bot", "chat": "chat/issue-4"}


class RouteAndOriginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="relay-origin-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "ledger"
        init_ledger(self.home)
        self.ledger = Ledger(self.home)
        self.addCleanup(self.ledger.close)
        self.outputs, self.requests, self.behaviours, self.connections = [], [], [], []
        for target, value in (("cli_relay.cli.spawn_keeper", None),
                               ("cli_relay.cli.spawn_notifier_pass", None),
                               ("cli_relay.cli.reconcile_task", [])):
            patcher = mock.patch(target, return_value=value)
            obj = patcher.start()
            self.addCleanup(patcher.stop)
            if target.endswith("spawn_keeper"):
                self.spawn = obj
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.root), "PATH": "/usr/bin:/bin", **ENV}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Every HTTP/HTTPS connection is replaced, even if the implementation chooses the wrong one.
        for cls in ("HTTPConnection", "HTTPSConnection"):
            patcher = mock.patch.object(notify.http.client, cls, side_effect=self.connection)
            patcher.start()
            self.addCleanup(patcher.stop)

    def connection(self, *args, **kwargs):
        behaviour = self.behaviours.pop(0) if self.behaviours else "ok"
        conn = mock.Mock()
        self.connections.append(conn)
        if behaviour == "connect":
            conn.connect.side_effect = OSError(TOKEN + URL)
        request = {}

        def send(method, path, *, body, headers):
            request.update(json.loads(body))
            self.requests.append(request)
            self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)

        def response():
            if behaviour == "timeout":
                raise TimeoutError(TOKEN + URL)
            record = request["cli_relay"]
            if callable(behaviour):
                body = behaviour(record)
            elif behaviour == "ok" and record["schema"] == "cli-relay.route-verify.v1":
                # Sending is durable, but the ACK and proof must not yet exist.
                saved = self.raw()["routes"][record["route"]]["verification"]
                self.assertEqual(saved["state"], "sending")
                self.assertIsNone(saved["acked_at"])
                body = {**record, "visible": True, "message_id": "msg-123", "ignored": TOKEN + URL}
            else:
                body = {}
            resp = mock.Mock(status=behaviour if type(behaviour) is int else 200)
            resp.getheader.return_value = None
            resp.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
            return resp

        conn.request.side_effect = send
        conn.getresponse.side_effect = response
        return conn

    def command(self, *args, expected=0):
        output = io.StringIO()
        opened = []

        def open_ledger(*args, **kwargs):
            obj = Ledger(*args, **kwargs)
            opened.append(obj)
            return obj

        try:
            with contextlib.redirect_stdout(output), mock.patch.object(cli, "Ledger", side_effect=open_ledger):
                code = cli.main(["--home", str(self.home), *args])
        finally:
            for obj in opened:
                obj.close()
        self.outputs.append(output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(code, expected, result)
        return result

    def add(self, name="bot", **kwargs):
        return self.command("route", "add", name, "--source-id", "mac-test", "--consumer", name,
                            "--chat", "target-chat", "--url-env", "ROUTE_URL", "--token-env", "ROUTE_TOKEN", **kwargs)

    def submit(self, key="k", origin=None, route=None, **kwargs):
        args = ["submit", "--key", key, "--engine", "fixture", "--cwd", str(self.root), "--prompt", "p"]
        if origin is not None:
            args += ["--origin", origin if isinstance(origin, str) else json.dumps(origin)]
        if route is not None:
            args += ["--notify-route", route]
        return self.command(*args, **kwargs)

    def raw(self):
        return json.loads((self.home / "notifier.json").read_text())

    def save(self, raw):
        (self.home / "notifier.json").write_text(json.dumps(raw))

    def due(self):
        raw = self.raw()
        raw["routes"]["bot"]["verification"]["next_at"] = "2000-01-01T00:00:00.000Z"
        self.save(raw)

    def finish(self, sub):
        return self.ledger.finish(sub["attempt_id"], from_states=("pending",), outcome="succeeded")

    def no_secret_persisted(self):
        for text in self.outputs:
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(URL, text)
        # Includes config, temporary files, locks, database/WAL and any logs or receipts.
        for path in self.home.rglob("*"):
            if path.is_file():
                contents = path.read_bytes()
                self.assertNotIn(TOKEN.encode(), contents, path.name)
                self.assertNotIn(URL.encode(), contents, path.name)

    def test_registration_is_idempotent_private_and_does_not_resolve_secrets(self):
        with mock.patch.object(notify, "_secret", side_effect=AssertionError("registration read a secret")):
            first = self.add()
            before = (self.home / "notifier.json").read_bytes()
            again = self.add()
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertFalse(first["route"]["proven"])
        self.assertIn("verification", self.raw()["routes"]["bot"])
        self.assertIsNone(self.raw()["routes"]["bot"]["verification"])
        self.assertEqual((self.home / "notifier.json").read_bytes(), before)
        self.assertEqual(stat.S_IMODE((self.home / "notifier.json").stat().st_mode), 0o600)
        self.assertIsNone(self.raw()["default_route"])
        self.assertEqual(self.requests, [])
        self.no_secret_persisted()

    def test_same_registration_preserves_proof_and_conflict_does_not_edit(self):
        self.add()
        self.command("route", "verify", "bot")
        before = (self.home / "notifier.json").read_bytes()
        self.assertTrue(self.add()["route"]["proven"])
        result = self.command("route", "add", "bot", "--consumer", "different", "--chat", "target-chat",
                              "--url-env", "ROUTE_URL", "--token-env", "ROUTE_TOKEN", expected=6)
        self.assertEqual(result["error"], "route_conflict")
        self.assertEqual((self.home / "notifier.json").read_bytes(), before)

    def test_first_registration_requires_source_and_valid_reference_names(self):
        base = ["route", "add", "bot", "--consumer", "bot", "--chat", "target-chat"]
        self.command(*base, "--url-env", "ROUTE_URL", "--token-env", "ROUTE_TOKEN", expected=2)
        for env_name in ("", "bad name", "1BAD", "x\n", "A" * 129):
            self.command(*base, "--source-id", "mac-test", "--url-env", env_name,
                         "--token-env", "ROUTE_TOKEN", expected=2)
        self.assertFalse((self.home / "notifier.json").exists())

    def test_file_references_are_only_references_and_require_private_files(self):
        # These are pre-existing synthetic inputs, outside the directory of generated outputs.
        token_file, url_file = self.root / "private-token", self.root / "private-url"
        token_file.write_text(TOKEN)
        url_file.write_text(URL)
        for path in (token_file, url_file):
            path.chmod(0o600)
        base = ["route", "add", "bot", "--source-id", "mac-test", "--consumer", "bot", "--chat", "target-chat",
                "--url-file", str(url_file), "--token-file", str(token_file)]
        with mock.patch.object(notify, "_secret", side_effect=AssertionError("registration read a secret")):
            self.command(*base)
        self.assertEqual(self.raw()["routes"]["bot"]["adapter"]["token_file"], str(token_file))
        self.command("doctor")
        self.command("route", "verify", "bot")
        self.no_secret_persisted()
        token_file.unlink()
        self.assertFalse(self.command(*base)["created"], "a duplicate registration does not re-resolve a missing file")
        token_file.write_text(TOKEN)
        self.command("route", "remove", "bot")
        token_file.chmod(0o644)
        self.command(*base, expected=2)

    def test_doctor_reports_names_proof_and_only_boolean_secret_resolution(self):
        self.add()
        row = self.command("doctor")["notifier"]["routes"][0]
        self.assertEqual(row, {"name": "bot", "proven": False, "proof": "unproven",
                               "secret_resolved": {"url": True, "token": True}})
        with mock.patch.dict(os.environ, {"ROUTE_TOKEN": ""}):
            self.assertFalse(self.command("doctor", expected=6)["notifier"]["routes"][0]["secret_resolved"]["token"])
        self.command("route", "verify", "bot")
        row = self.command("doctor")["notifier"]["routes"][0]
        self.assertTrue(row["proven"])
        self.assertEqual(row["proof"], "receipt")
        self.no_secret_persisted()

    def legacy_config(self, *, http=False):
        # Installer layout: one Friday route, no verification key. All identifiers and
        # references are synthetic; never load either Mac's live configuration.
        adapter = {"type": "http", "url_env": "ROUTE_URL", "token_env": "ROUTE_TOKEN"} if http else {
            "type": "command", "argv": ["/bin/true"], "job": "CLI_RELAY"}
        raw = {"source_id": "mac-test", "default_route": "friday",
               "routes": {"friday": {"consumer": "friday", "chat": "target-chat", "adapter": adapter}},
               "notify_outcomes": ["succeeded", "failed", "blocked", "needs_input", "unknown"],
               "retry": {"max_attempts": 6, "base_delay_s": 30, "max_delay_s": 1800},
               "reminder": {"ack_timeout_s": 1800, "max_wakes": 2}}
        (self.home / "notifier.json").write_text(json.dumps(raw, indent=2) + "\n")
        return raw

    def test_installer_template_friday_submits_delivers_and_keeps_config_bytes(self):
        self.legacy_config()
        path = self.home / "notifier.json"
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        self.assertTrue(notify.load_config(self.home)["routes"]["friday"]["proven"])
        row = self.command("doctor")["notifier"]["routes"][0]
        self.assertEqual(row, {"name": "friday", "proven": True, "proof": "legacy_config", "secret_resolved": {}})
        self.assertTrue(self.command("route", "list")["routes"][0]["proven"])
        self.assertFalse(self.command("route", "set-default", "friday")["changed"])
        self.assertFalse(self.command("route", "verify", "friday")["sent"])

        tasks = [self.submit("explicit", route="friday"), self.submit("default")]
        expected = {"engine": "fixture", "cwd": os.path.realpath(self.root), "prompt": "p", "options": {}}
        for sub in tasks:
            self.assertEqual((sub["notify_route"], sub["notify_consumer"], sub["notify_chat"]),
                             ("friday", "friday", "target-chat"))
            self.assertNotIn("origin", sub)
            self.assertEqual(sub["payload_sha256"], payload_hash(expected))
            self.assertEqual(json.loads(self.ledger.get_task(sub["task_id"])["payload_json"]), expected)
        events = [self.finish(sub) for sub in tasks]
        with mock.patch.object(notify.subprocess, "run", return_value=mock.Mock(returncode=0)) as sender, \
                mock.patch.object(notify, "build_payload", wraps=notify.build_payload) as build:
            first = self.command("notify", "flush")
            self.assertEqual(first["blocked"], [])
            self.assertEqual(first["sends"][0]["outcome"], "handed_off")
            self.assertEqual(set(first["sends"][0]["events"]), set(events))
            refs = build.call_args.args[3]
            self.assertTrue(all("origin" not in ref for ref in refs))
            self.assertEqual(sender.call_args.args[0][:3], ["/bin/true", "CLI_RELAY", "mac-test/friday"])
            self.assertNotIn("input", sender.call_args.kwargs, "legacy task sends keep the five-field interface")
            self.assertEqual(self.command("notify", "flush")["sends"], [])
            # An un-ACKed event still gets its due reminder after a fresh config load.
            with self.ledger.tx() as c:
                c.execute("UPDATE notifications SET next_at='2000-01-01T00:00:00.000Z'")
            self.assertEqual(len(self.command("notify", "flush")["sends"]), 1)
            self.assertTrue(build.call_args.kwargs["reminder"])
            for sub, event in zip(tasks, events):
                self.command("ack", "--consumer", "friday", "--task", sub["task_id"], "--attempt", "1",
                             "--event", event)
            self.assertEqual(self.command("notify", "flush")["sends"], [])
            self.assertEqual(sender.call_count, 2)
        self.assertEqual({row["state"] for row in notify.status(self.home)}, {"acked"})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.requests, [])
        self.no_secret_persisted()

    def test_legacy_with_explicit_null_or_pending_verification_is_not_exempt(self):
        raw = self.legacy_config()
        legacy_route = notify.validate_config(raw)["routes"]["friday"]
        pending = {"verification_id": "v-" + "a" * 32,
                   "route_sha256": notify.route_fingerprint(raw["source_id"], legacy_route),
                   "ledger_id": self.ledger.ledger_id, "state": "pending", "attempts": 0, "wakes": 0,
                   "next_at": now_iso(), "created_at": now_iso(), "message_id": None,
                   "acked_at": None, "last_result": None}
        # An existing main-era task/event must also stay unsent while proof is pending.
        payload = {"engine": "fixture", "cwd": str(self.root), "prompt": "p", "options": {}}
        _, task, _ = self.ledger.submit(scope="default", key="existing", payload=payload, prompt="p",
                                      native_session_id=None, notify_route="friday", notify_consumer="friday",
                                      notify_chat="target-chat", notify_bound=True)
        attempt = self.ledger.current_attempt(task["task_id"])
        self.ledger.finish(attempt["attempt_id"], from_states=("pending",), outcome="succeeded")
        for verification in (None, pending):
            with self.subTest(verification="null" if verification is None else "pending"):
                raw["routes"]["friday"]["verification"] = verification
                self.save(raw)
                before = (self.home / "notifier.json").read_bytes()
                self.assertFalse(notify.load_config(self.home)["routes"]["friday"]["proven"])
                self.assertEqual(self.submit(route="friday", expected=6)["error"], "route_unproven")
                self.assertEqual(self.submit(expected=6)["error"], "route_unproven")
                self.command("route", "set-default", "friday", expected=6)
                self.assertEqual(self.command("doctor", expected=6)["notifier"]["routes"][0]["proof"], "unproven")
                with self.ledger.tx() as c:
                    c.execute("UPDATE notifications SET next_at='2000-01-01T00:00:00.000Z'")
                with mock.patch.object(notify, "send") as sender:
                    result = self.command("notify", "flush")
                sender.assert_not_called()
                self.assertEqual(result["sends"], [])
                self.assertEqual(result["blocked"][0]["why"], "route_unproven")
                self.assertEqual((self.home / "notifier.json").read_bytes(), before)
        self.spawn.assert_not_called()
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_readding_or_replacing_legacy_route_requires_verification(self):
        self.legacy_config(http=True)
        self.assertTrue(notify.load_config(self.home)["routes"]["friday"]["proven"])
        result = self.add("friday")
        self.assertFalse(result["created"])
        self.assertFalse(result["route"]["proven"])
        self.assertIn("verification", self.raw()["routes"]["friday"])
        self.assertIsNone(self.raw()["routes"]["friday"]["verification"])
        self.assertEqual(self.submit(route="friday", expected=6)["error"], "route_unproven")
        self.assertEqual(self.submit(expected=6)["error"], "route_unproven")
        self.command("route", "verify", "friday")
        self.assertTrue(notify.load_config(self.home)["routes"]["friday"]["proven"])

        self.legacy_config(http=True)
        self.command("route", "remove", "friday")
        changed = self.command("route", "add", "friday", "--consumer", "friday", "--chat", "new-chat",
                               "--url-env", "ROUTE_URL", "--token-env", "ROUTE_TOKEN")
        self.assertFalse(changed["route"]["proven"])
        self.assertEqual(self.submit(route="friday", expected=6)["error"], "route_unproven")
        self.no_secret_persisted()

    def test_managing_another_route_preserves_legacy_exemption(self):
        self.legacy_config(http=True)
        self.add()
        self.assertNotIn("verification", self.raw()["routes"]["friday"])
        self.assertTrue(notify.load_config(self.home)["routes"]["friday"]["proven"])
        self.assertFalse(notify.load_config(self.home)["routes"]["bot"]["proven"])
        self.command("route", "verify", "bot")
        self.command("route", "set-default", "bot")
        self.command("route", "remove", "bot")
        self.command("route", "set-default", "friday")
        self.assertNotIn("verification", self.raw()["routes"]["friday"])
        sub = self.submit()
        self.finish(sub)
        self.assertEqual(len(self.command("notify", "flush")["sends"]), 1)
        self.assertEqual(self.requests[-1]["cli_relay"]["route"], "friday")
        self.no_secret_persisted()

    def test_unproven_default_and_explicit_route_are_rejected_before_task_commit(self):
        self.add()
        self.assertEqual(self.submit(route="bot", expected=6)["error"], "route_unproven")
        self.command("route", "set-default", "bot", expected=6)
        raw = self.raw()
        raw["default_route"] = "bot"
        self.save(raw)
        self.assertEqual(self.submit(expected=6)["error"], "route_unproven")
        self.assertFalse(self.command("doctor", expected=6)["notifier"]["default_usable"])
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.spawn.assert_not_called()

    def test_unregistered_route_is_rejected_without_config(self):
        self.assertEqual(self.submit(route="bot", expected=6)["error"], "route_not_configured")
        self.spawn.assert_not_called()

    def test_verify_success_is_durable_before_default_use_and_idempotent(self):
        self.add()
        proof = self.command("route", "verify", "bot")
        self.assertTrue(proof["proven"])
        self.assertEqual(proof["verification"]["message_id"], "msg-123")
        self.assertEqual(proof["verification"]["state"], "acked")
        self.assertTrue(proof["verification"]["acked_at"])
        self.assertFalse(self.command("route", "verify", "bot")["sent"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.command("route", "set-default", "bot")
        self.assertEqual(self.submit()["notify_route"], "bot")
        self.no_secret_persisted()

    def test_2xx_requires_visible_message_id_and_exact_probe_destination(self):
        changes = [{"message_id": value} for value in (None, "", 123, "x" * 257, "id\n", TOKEN)]
        changes += [{key: "wrong"} for key in ("verification_id", "source_id", "ledger_id", "route", "consumer", "chat")]
        changes += [{"visible": value} for value in (False, 1, "true")]
        for change in changes:
            with self.subTest(change=change):
                self.add()
                self.behaviours.append(lambda req, change=change: {**req, "visible": True, "message_id": "m1", **change})
                result = self.command("route", "verify", "bot", expected=6)
                self.assertFalse(result["proven"])
                self.assertEqual(result["verification"]["state"], "unconfirmed")
                self.assertIsNone(result["verification"]["acked_at"])
                self.assertIsNone(result["verification"]["message_id"])
                self.no_secret_persisted()
                self.command("route", "remove", "bot")

    def test_malformed_receipt_and_http_rejection_never_prove(self):
        for response in (lambda req: b"not json " + TOKEN.encode(), lambda req: {}, lambda req: [TOKEN], 401, 500):
            self.add()
            self.behaviours.append(response)
            self.assertFalse(self.command("route", "verify", "bot", expected=6)["proven"])
            self.command("route", "remove", "bot")
        self.no_secret_persisted()

    def test_verify_timeout_is_bounded_keeps_receipt_and_requires_explicit_new_budget(self):
        self.add()
        self.behaviours.extend(["timeout", "timeout"])
        first = self.command("route", "verify", "bot", expected=6)
        self.assertFalse(self.command("route", "verify", "bot", expected=6)["sent"])
        self.command("route", "verify", "bot", "--retry", expected=6)
        self.due()
        second = self.command("route", "verify", "bot", expected=6)
        self.assertEqual(first["verification"]["verification_id"], second["verification"]["verification_id"])
        self.due()
        for _ in range(8):
            last = self.command("route", "verify", "bot", expected=6)
        self.assertEqual((last["verification"]["state"], last["verification"]["attempts"], len(self.requests)),
                         ("unacknowledged", 2, 2))
        recovered = self.command("route", "verify", "bot", "--retry")
        self.assertNotEqual(first["verification"]["verification_id"], recovered["verification"]["verification_id"])
        self.no_secret_persisted()

    def test_verify_connect_failures_use_exponential_backoff_and_stop_at_six(self):
        self.add()
        self.behaviours.extend(["connect"] * 6)
        for i in range(6):
            with mock.patch.object(notify, "_later", wraps=notify._later) as later:
                result = self.command("route", "verify", "bot", expected=6)
                if i < 5:
                    self.assertEqual(later.call_args.args[0], 30 * 2 ** i)
            if i < 5:
                self.assertFalse(self.command("route", "verify", "bot", expected=6)["sent"])
                self.due()
        self.assertEqual((result["verification"]["state"], result["verification"]["attempts"]), ("failed", 6))
        self.assertEqual(result["verification"]["wakes"], 0)
        for _ in range(4):
            self.command("route", "verify", "bot", expected=6)
        self.assertEqual(len(self.connections), 6)
        self.no_secret_persisted()

    def test_crashed_verifier_consumes_one_possible_wake_and_is_not_resent_at_once(self):
        self.add()
        with mock.patch.object(notify, "send", side_effect=SystemExit(91)):
            with self.assertRaises(SystemExit):
                routes.verify(self.home, "bot", env=ENV)
        state = self.raw()["routes"]["bot"]["verification"]
        self.assertEqual((state["state"], state["attempts"], state["wakes"]), ("sending", 1, 1))
        self.assertFalse(self.command("route", "verify", "bot", expected=6)["sent"])
        self.due()
        proof = self.command("route", "verify", "bot")
        self.assertEqual(proof["verification"]["verification_id"], state["verification_id"])
        self.assertEqual(proof["verification"]["attempts"], 2)

    def test_proof_is_invalidated_by_identity_or_reference_edits(self):
        self.add()
        first = self.command("route", "verify", "bot")
        raw = self.raw()
        raw["routes"]["bot"]["chat"] = "new-chat"
        self.save(raw)
        self.assertFalse(self.command("route", "list")["routes"][0]["proven"])
        self.submit(route="bot", expected=6)
        second = self.command("route", "verify", "bot")
        self.assertNotEqual(first["verification"]["verification_id"], second["verification"]["verification_id"])
        raw = self.raw()
        raw["routes"]["bot"]["adapter"]["token_env"] = "NEW_TOKEN"
        self.save(raw)
        self.assertFalse(self.command("route", "list")["routes"][0]["proven"])

    def test_proof_copied_to_another_ledger_is_not_trusted(self):
        self.add()
        first = self.command("route", "verify", "bot")
        copied = self.raw()
        self.home = self.root / "other-ledger"
        init_ledger(self.home)
        self.save(copied)
        self.assertFalse(self.command("route", "list")["routes"][0]["proven"])
        self.submit(route="bot", expected=6)
        second = self.command("route", "verify", "bot")
        self.assertNotEqual(first["verification"]["verification_id"], second["verification"]["verification_id"])
        self.assertNotEqual(first["verification"]["ledger_id"], second["verification"]["ledger_id"])

    def test_explicitly_unproven_command_adapter_can_return_a_probe_receipt(self):
        self.save({"source_id": "mac-test", "routes": {"bot": {"consumer": "bot", "chat": "target-chat", "verification": None,
                   "adapter": {"type": "command", "argv": ["/bin/false"]}}}})
        self.assertFalse(self.command("route", "list")["routes"][0]["proven"])

        def run(*args, **kwargs):
            request = json.loads(kwargs["input"])["cli_relay"]
            return mock.Mock(returncode=0, stdout=json.dumps({**request, "visible": True, "message_id": "m-command"}).encode())

        with mock.patch.object(notify.subprocess, "run", side_effect=run):
            proof = self.command("route", "verify", "bot")
        self.assertEqual(proof["verification"]["message_id"], "m-command")

    def test_remove_is_idempotent_clears_default_and_keeps_task_and_events(self):
        self.add()
        self.command("route", "verify", "bot")
        self.command("route", "set-default", "bot")
        sub = self.submit()
        event = self.finish(sub)
        self.assertTrue(self.command("route", "remove", "bot")["removed"])
        self.assertFalse(self.command("route", "remove", "bot")["removed"])
        self.assertEqual(self.command("route", "list")["routes"], [])
        self.assertIsNone(self.raw()["default_route"])
        notify.flush(self.home, env=ENV)
        self.assertEqual(self.ledger.events_for("bot")[0]["event_id"], event)
        self.assertEqual(notify.status(self.home)[0]["state"], "failed")
        self.assertEqual(self.submit()["status"], "duplicate")
        self.assertEqual(self.spawn.call_count, 1)

    def test_symlink_config_is_refused_without_replacing_or_modifying_target(self):
        outside = self.root / "outside-config"
        outside.write_text("unchanged")
        (self.home / "notifier.json").symlink_to(outside)
        self.assertEqual(self.add(expected=6)["error"], "notifier_symlink")
        self.assertEqual(outside.read_text(), "unchanged")
        self.assertTrue((self.home / "notifier.json").is_symlink())

    def test_concurrent_registrations_and_verifications_do_not_lose_updates_or_double_send(self):
        def register(i):
            return routes.add(self.home, f"bot{i}", consumer="bot", chat="target-chat", source_id="mac-test",
                              adapter={"type": "http", "url_env": "ROUTE_URL", "token_env": "ROUTE_TOKEN"})
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(result["created"] for result in pool.map(register, range(4))))
        self.assertEqual(len(self.raw()["routes"]), 4)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: routes.verify(self.home, "bot0", env=ENV), range(2)))
        self.assertEqual(sum(r["sent"] for r in receipts), 1)
        self.assertTrue(all(r["proven"] for r in receipts))

    def test_origin_reaches_payload_hash_receipt_status_terminal_event_and_notification(self):
        self.add()
        self.command("route", "verify", "bot")
        sub = self.submit(origin=ORIGIN, route="bot")
        self.assertEqual(sub["origin"], ORIGIN)
        payload = json.loads(self.ledger.get_task(sub["task_id"])["payload_json"])
        self.assertEqual(payload["origin"], ORIGIN)
        self.assertEqual(sub["payload_sha256"], payload_hash(payload))
        without = {k: v for k, v in payload.items() if k != "origin"}
        self.assertNotEqual(sub["payload_sha256"], payload_hash(without))
        event = self.finish(sub)
        status = self.command("status", sub["task_id"], "--no-reconcile")
        self.assertEqual(status["task"]["origin"], ORIGIN)
        self.assertEqual(status["events"][0]["body"]["origin"], ORIGIN)
        events = self.command("events", "--consumer", "bot", "--no-reconcile")["events"]
        self.assertEqual(events[0]["body"]["origin"], ORIGIN)
        notify.flush(self.home, env=ENV)
        ref = self.requests[-1]["cli_relay"]["events"][0]
        self.assertEqual((ref["event_id"], ref["origin"]), (event, ORIGIN))
        self.assertEqual(self.requests[-1]["cli_relay"]["chat"], "target-chat", "origin is metadata, not routing authority")

    def test_origin_duplicates_are_canonical_changes_conflict_and_do_not_restart(self):
        sub = self.submit(origin=ORIGIN)
        self.finish(sub)
        again = self.submit(origin=' { "chat":"chat/issue-4", "bot":"github-bot" } ')
        self.assertEqual((again["status"], again["task_id"], again["origin"]), ("duplicate", sub["task_id"], ORIGIN))
        self.submit(origin={**ORIGIN, "chat": "other"}, expected=3)
        self.submit(expected=3)
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(len(self.ledger.attempts(sub["task_id"])), 1)

    def test_absent_origin_preserves_old_payload_hash_and_output_shape(self):
        sub = self.submit()
        self.assertNotIn("origin", sub)
        expected = {"engine": "fixture", "cwd": os.path.realpath(self.root), "prompt": "p", "options": {}}
        self.assertEqual(sub["payload_sha256"], payload_hash(expected))
        self.assertEqual(json.loads(self.ledger.get_task(sub["task_id"])["payload_json"]), expected)
        self.finish(sub)
        result = self.command("status", sub["task_id"], "--no-reconcile")
        self.assertNotIn("origin", result["task"])
        self.assertNotIn("origin", result["events"][0]["body"])

    def test_origin_survives_unknown_and_new_attempt(self):
        sub = self.submit(origin=ORIGIN)
        self.ledger.mark_unknown(sub["attempt_id"], from_states=("pending",), evidence={})
        att = self.ledger.new_attempt(sub["task_id"], expect_attempt_no=1, reason="resume", prompt="continue",
                                      resume_session_id="s1", native_session_id="s1")
        self.ledger.finish(att["attempt_id"], from_states=("pending",), outcome="failed")
        self.assertEqual([json.loads(e["body_json"])["origin"] for e in self.ledger.events_of_task(sub["task_id"])],
                         [ORIGIN, ORIGIN])

    def test_resume_receipt_keeps_original_origin(self):
        sub = self.submit(origin=ORIGIN)
        self.finish(sub)
        with mock.patch.object(cli, "old_run_stopped", return_value={"stopped": True}):
            resumed = self.command("resume", sub["task_id"], "--prompt", "continue")
        self.assertEqual(resumed["origin"], ORIGIN)

    def test_origin_validation_rejects_bad_types_unknown_keys_duplicates_and_length(self):
        invalid = ["not json", "null", "[]", "{}", {"bot": "bot"}, {**ORIGIN, "secret": "extra"},
                   {**ORIGIN, "bot": 1}, {**ORIGIN, "chat": []}, {**ORIGIN, "bot": "x" * 65},
                   {**ORIGIN, "chat": "x" * 257}, {**ORIGIN, "chat": " leading"}, {**ORIGIN, "chat": "a\nb"},
                   {**ORIGIN, "bot": "a\n"}, {**ORIGIN, "chat": ""}, ' {"bot":"x","bot":"y","chat":"z"}', " " * 4097]
        for value in invalid:
            with self.subTest(value=value):
                self.submit(origin=value, expected=2)
        self.spawn.assert_not_called()
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_unproven_events_are_retained_with_bounded_checks_and_no_task_restart(self):
        self.add()
        self.command("route", "verify", "bot")
        sub = self.submit(route="bot")
        event = self.finish(sub)
        raw = self.raw()
        raw["routes"]["bot"]["verification"] = None
        self.save(raw)
        sent_before = len(self.requests)
        notify.flush(self.home, env=ENV)
        notify.flush(self.home, env=ENV)
        self.assertEqual(notify.status(self.home)[0]["attempts"], 1, "readiness backoff cannot be bypassed by polling")
        for i in range(10):
            notify.flush(self.home, env=ENV)
            with self.ledger.tx() as c:
                c.execute("UPDATE notifications SET next_at='2000-01-01T00:00:00.000Z' WHERE next_at IS NOT NULL")
        row = notify.status(self.home)[0]
        self.assertEqual((row["state"], row["attempts"], row["wakes"]), ("failed", 6, 0))
        self.assertIsNone(row["first_sent_at"])
        self.assertEqual(len(self.requests), sent_before)
        self.assertEqual(self.ledger.events_for("bot")[0]["event_id"], event)
        self.assertEqual(self.ledger.get_task(sub["task_id"])["state"], "succeeded")
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(len(self.ledger.attempts(sub["task_id"])), 1)
        self.command("route", "verify", "bot")
        self.assertEqual(notify.flush(self.home, env=ENV)["sends"], [])
        self.command("notify", "retry", "--event", event)
        self.assertEqual(len(notify.flush(self.home, env=ENV)["sends"]), 1)

    def test_task_ack_timeout_keeps_event_and_bounded_reminders_without_rerun(self):
        self.add()
        self.command("route", "verify", "bot")
        sub = self.submit(route="bot")
        event = self.finish(sub)
        before = len(self.requests)
        notify.flush(self.home, env=ENV)
        notify.flush(self.home, env=ENV)
        self.assertEqual(len(self.requests) - before, 1, "ACK timeout gates the reminder")
        for _ in range(10):
            notify.flush(self.home, env=ENV)
            with self.ledger.tx() as c:
                c.execute("UPDATE notifications SET next_at='2000-01-01T00:00:00.000Z' WHERE next_at IS NOT NULL")
        row = notify.status(self.home)[0]
        self.assertEqual((row["state"], row["attempts"], row["wakes"]), ("unacknowledged", 2, 2))
        self.assertEqual(len(self.requests) - before, 2)
        self.assertEqual(self.ledger.events_for("bot")[0]["event_id"], event)
        self.assertEqual(self.spawn.call_count, 1)
        self.assertEqual(len(self.ledger.attempts(sub["task_id"])), 1)
        self.ledger.ack(task_id=sub["task_id"], attempt_no=1, event_id=event, consumer="bot")
        notify.flush(self.home, env=ENV)
        self.assertEqual(notify.status(self.home)[0]["state"], "acked")
        self.assertEqual(len(self.requests) - before, 2)


class CompletedFixtureIsNeverRerun(RelayTestCase):
    def test_actual_fixture_side_effect_happens_once_during_all_notification_retries(self):
        sub = self.submit("origin-no-rerun", duration=0.05)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(len(side_effects(self.effects)), 1)
        write_config(self.home, http_config(self.tmp, "http://127.0.0.1:1/unused"))
        with mock.patch.object(notify, "send", return_value={"outcome": "accepted", "why": "http_2xx"}) as send:
            for _ in range(10):
                notify.flush(self.home, env={})
                ledger = Ledger(self.home)
                with ledger.tx() as c:
                    c.execute("UPDATE notifications SET next_at='2000-01-01T00:00:00.000Z' WHERE next_at IS NOT NULL")
                ledger.close()
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(side_effects(self.effects)), 1)
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM attempts")), 1)
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM events")), 1)
        self.assertEqual(notify.status(self.home)[0]["state"], "unacknowledged")


if __name__ == "__main__":
    unittest.main()

"""Direct-route tests use temporary ledgers, fixture tasks and fake HTTP connections."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import base_env
from cli_relay import cli, notify
from cli_relay.ledger import Ledger, init_ledger

TOKEN = "SYNTHETIC_ROUTE_SECRET_901"
URL = "http://127.0.0.1:1/SYNTHETIC_PRIVATE_ROUTE_902"


class DirectRouteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="relay-direct-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home, self.work = self.root / "ledger", self.root / "work"
        self.work.mkdir()
        init_ledger(self.home)
        self.ledger = Ledger(self.home)
        self.addCleanup(self.ledger.close)
        patcher = mock.patch.dict(os.environ, {**base_env(), "ROUTE_URL": URL, "ROUTE_TOKEN": TOKEN}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, result in (("spawn_keeper", None), ("spawn_notifier_pass", None),
                             ("reconcile_task", []), ("reconcile_all", [])):
            patcher = mock.patch.object(cli, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = []

    def command(self, *args, expected=0, text=False):
        out = io.StringIO()
        opened = []

        def ledger(*positional, **kwargs):
            obj = Ledger(*positional, **kwargs)
            opened.append(obj)
            return obj

        try:
            with contextlib.redirect_stdout(out), mock.patch.object(cli, "Ledger", side_effect=ledger):
                code = cli.main(["--home", str(self.home), *args])
        finally:
            for obj in opened:
                obj.close()
        value = out.getvalue()
        self.output.append(value)
        self.assertEqual(code, expected, value)
        return value if text else json.loads(value)

    def add(self, name="bot", *, fallback=None):
        args = ["route", "add", name, "--source-id", "test-mac", "--consumer", name,
                "--chat", name + "-chat", "--url-env", "ROUTE_URL", "--token-env", "ROUTE_TOKEN"]
        if fallback:
            args += ["--fallback-route", fallback]
        return self.command(*args)

    def prove(self, name="bot"):
        with mock.patch.object(notify, "send", return_value={"outcome": "accepted", "why": "visible_message_confirmed",
                                                           "message_id": "test-message-" + name}):
            return self.command("route", "verify", name)

    def finish(self, key="k", *, route=None, options=None, outcome="succeeded", result="status=ok", origin=None):
        args = ["submit", "--key", key, "--engine", "fixture", "--cwd", str(self.work), "--prompt", "p"]
        if route:
            args += ["--notify-route", route]
        if origin:
            args += ["--origin", json.dumps(origin)]
        for name, value in (options or {}).items():
            args += ["--option", name + "=" + json.dumps(value)]
        task = self.command(*args)
        event = self.ledger.finish(task["attempt_id"], from_states=("pending",), outcome=outcome,
                                   native={"ok": True, "result": result})
        return task, event

    def assert_no_secret(self):
        for value in self.output:
            self.assertNotIn(TOKEN, value)
            self.assertNotIn(URL, value)
        for path in self.home.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(TOKEN.encode(), data, path.name)
                self.assertNotIn(URL.encode(), data, path.name)

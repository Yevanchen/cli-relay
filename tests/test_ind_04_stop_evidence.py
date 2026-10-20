"""A cancelled direct child does not prove its group stopped. No real child or signal is used."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack
from itertools import count
from pathlib import Path
from unittest.mock import patch

from helpers import isolated_env

from cli_relay import keeper, procs, reconcile
from cli_relay.ledger import Ledger, init_ledger


class FakeChild:
    pid = 42002

    def __init__(self, wait_once):
        self.wait_once = wait_once

    def wait(self, timeout=None):
        if self.wait_once:
            self.wait_once = False
            raise subprocess.TimeoutExpired(["fake-child"], timeout)
        return -signal.SIGTERM


class KeeperStopEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cli-relay-stop-proof-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "ledger"
        init_ledger(self.home)
        self.ledger = Ledger(self.home)
        self.addCleanup(self.ledger.close)
        _, task, _ = self.ledger.submit(
            scope="test", key="stop-proof", prompt="p", native_session_id=None,
            payload={"engine": "fixture", "cwd": self.tmp.name, "prompt": "p", "options": {}})
        self.task_id = task["task_id"]
        self.attempt_id = self.ledger.current_attempt(self.task_id)["attempt_id"]

    def run_keeper(self, members, *, wait_once=True, guard=False):
        def hook(point):
            if point == "after_record" and not guard:
                self.ledger.request_cancel(self.task_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(keeper, "Ledger", return_value=self.ledger))
            stack.enter_context(patch.dict(os.environ, isolated_env(Path(self.tmp.name),
                                {"CLI_RELAY_KEEPER_DETACHED": "1"}), clear=True))
            stack.enter_context(patch.object(keeper.signal, "signal"))
            for name in ("getpid", "getpgid", "getsid"):
                stack.enter_context(patch.object(keeper.os, name, return_value=42001))
            stack.enter_context(patch.object(procs, "start_token", return_value="fixture-token"))
            stack.enter_context(patch.object(procs, "group_members", return_value=members))
            stack.enter_context(patch.object(keeper.subprocess, "Popen", return_value=FakeChild(wait_once)))
            stack.enter_context(patch.object(keeper.os, "killpg"))
            stack.enter_context(patch.object(keeper.os, "kill", side_effect=PermissionError("fixture")))
            stack.enter_context(patch.object(keeper.time, "monotonic", side_effect=count(1000, 100)))
            stack.enter_context(patch.object(keeper.time, "sleep"))
            stack.enter_context(patch.object(keeper, "_hook", side_effect=hook))
            if guard:
                stack.enter_context(patch.object(keeper.engines, "decide", return_value=("blocked", {})))
            self.assertEqual(keeper.run(self.home, self.task_id, self.attempt_id), 0)
        return self.ledger.get_attempt(self.attempt_id)

    def assert_unknown_then_cancelled(self):
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "unknown")
        with patch.object(procs, "same_process_alive", return_value=False), \
             patch.object(procs, "group_alive", return_value=True), patch.object(os, "killpg") as killpg:
            for _ in range(2):
                reconcile.reconcile_task(self.ledger, self.task_id, kick=False, spawn=None)
        killpg.assert_not_called()
        self.assertEqual([e["kind"] for e in self.ledger.events_of_task(self.task_id)], ["unknown"])
        # Events are listed by millisecond timestamp, then by their random id. In-process, both
        # events can fall into one millisecond, so let the real clock move on before the close.
        time.sleep(0.002)
        with patch.object(procs, "same_process_alive", return_value=False), \
             patch.object(procs, "group_alive", return_value=False):
            reconcile.reconcile_task(self.ledger, self.task_id, kick=False, spawn=None)
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "cancelled")
        self.assertEqual([e["kind"] for e in self.ledger.events_of_task(self.task_id)], ["unknown", "terminal"])

    def test_unavailable_group_enumeration_is_unknown_and_later_converges(self):
        att = self.run_keeper(None)
        self.assertEqual(json.loads(att["evidence_json"])["process_group_cleanup"]["enumeration"], "unavailable")
        self.assert_unknown_then_cancelled()

    def test_remaining_member_is_unknown_and_later_converges(self):
        att = self.run_keeper([42001, 42003])
        self.assertEqual(json.loads(att["evidence_json"])["process_group_cleanup"]["remaining"], 1)
        self.assert_unknown_then_cancelled()

    def test_cancel_read_after_child_exit_still_requires_group_cleanup(self):
        self.run_keeper([42001, 42003], wait_once=False)
        self.assert_unknown_then_cancelled()

    def test_guard_discovered_after_child_exit_requires_group_cleanup(self):
        self.run_keeper([42001, 42003], wait_once=False, guard=True)
        self.assertEqual(self.ledger.get_task(self.task_id)["state"], "unknown")

    def test_empty_group_proves_cancellation_finished(self):
        att = self.run_keeper([42001])
        self.assertEqual((att["state"], att["outcome"]), ("finished", "cancelled"))
        self.assertEqual(json.loads(att["evidence_json"])["process_group_cleanup"]["remaining"], 0)
        self.assertEqual([e["kind"] for e in self.ledger.events_of_task(self.task_id)], ["terminal"])


if __name__ == "__main__":
    unittest.main()

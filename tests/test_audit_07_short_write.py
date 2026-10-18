"""Audit #7: atomic file writes must write every byte (short writes happen under quota/full disk)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import RelayTestCase, base_env, side_effects, wait_until

from cli_relay import ledger


class ShortWrites(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-a07-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_short_writes_are_completed(self) -> None:
        real_write = os.write

        def three_bytes_at_a_time(fd, data):
            return real_write(fd, bytes(data[:3]))

        text = "the full prompt that must reach the CLI intact"
        with mock.patch.object(ledger.os, "write", side_effect=three_bytes_at_a_time):
            ledger.atomic_write_text(self.tmp / "prompt.txt", text)
        self.assertEqual((self.tmp / "prompt.txt").read_text(), text)

    def test_a_write_that_makes_no_progress_fails_before_rename(self) -> None:
        target = self.tmp / "prompt.txt"
        with mock.patch.object(ledger.os, "write", return_value=0):
            with self.assertRaises(OSError):
                ledger.atomic_write_text(target, "abcdef")
        self.assertFalse(target.exists(), "a partial file must never be renamed into place")


class PromptThatCannotBeStored(RelayTestCase):
    def test_the_attempt_is_blocked_and_the_cli_never_starts(self) -> None:
        sub = self.submit("k-a07", duration=0.1, env=base_env({"CLI_RELAY_TEST_PAUSE": "after_claim:2"}))
        run_dir = self.home / "runs" / sub["task_id"] / "1"
        run_dir.mkdir(mode=0o500)  # the keeper is paused before it creates this directory
        try:
            done = wait_until(lambda: (lambda s: s if s["task"]["state"] not in ("accepted", "running") else None)(
                self.status(sub["task_id"])), timeout=30, interval=0.3)
        finally:
            run_dir.chmod(0o700)
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["preflight"]["reason"], "prompt_write_failed")
        self.assertIsNone(done["attempts"][0]["spawn_started_at"])
        self.assertEqual(side_effects(self.effects), [])


if __name__ == "__main__":
    unittest.main()

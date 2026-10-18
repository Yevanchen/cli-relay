"""Audit #7: atomic file writes must write every byte (short writes happen under quota/full disk)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

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


if __name__ == "__main__":
    unittest.main()

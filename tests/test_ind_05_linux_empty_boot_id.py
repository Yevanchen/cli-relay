"""Independent audit 5: a Linux identity whose boot id could not be read is not proof of anything.
Two tokens that are both missing the boot id are "cannot verify", even if they look equal."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from cli_relay import procs


class MissingBootIdIsNotAMatch(unittest.TestCase):
    def test_both_boot_ids_missing(self) -> None:
        self.assertIsNone(procs._compare_tokens("linux::1234", "linux::1234"))

    def test_one_boot_id_missing(self) -> None:
        self.assertIsNone(procs._compare_tokens("linux:5e1f6c2a-0000-4000-8000-000000000001:1234", "linux::1234"))

    def test_complete_identities_still_compare(self) -> None:
        a = "linux:5e1f6c2a-0000-4000-8000-000000000001:1234"
        self.assertIs(procs._compare_tokens(a, a), True)
        self.assertIs(procs._compare_tokens(a, a[:-4] + "9999"), False)

    def test_empty_or_truncated_tokens_are_unverifiable(self) -> None:
        self.assertIsNone(procs._compare_tokens("darwin:", "darwin:"))
        self.assertIsNone(procs._compare_tokens("linux:", "linux:"))


if __name__ == "__main__":
    unittest.main()

"""Audit #10 / issue #12: denials need input unless native success and exit zero prove recovery."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from cli_relay import engines


class PermissionDenialPrecedence(unittest.TestCase):
    def test_denial_with_error_result_is_needs_input(self) -> None:
        native = {"ok": False, "is_error": True, "subtype": "error_during_execution",
                  "permission_denials": ["Bash"]}
        self.assertEqual(engines.classify(native, exit_code=1, exit_signal=None, exit_known=True,
                                          cancel_requested=False), "needs_input")

    def test_denial_with_success_result_and_exit_zero_succeeds(self) -> None:
        native = {"ok": True, "permission_denials": ["Monitor"]}
        self.assertEqual(engines.classify(native, exit_code=0, exit_signal=None, exit_known=True,
                                          cancel_requested=False), "succeeded")

    def test_denial_without_confirmed_zero_exit_still_needs_input(self) -> None:
        native = {"ok": True, "permission_denials": ["Monitor"]}
        for code, known, sig in ((1, True, None), (None, False, None), (None, True, 15)):
            with self.subTest(code=code, known=known, signal=sig):
                self.assertEqual(engines.classify(native, exit_code=code, exit_signal=sig, exit_known=known,
                                                  cancel_requested=False), "needs_input")

    def test_cancel_still_wins(self) -> None:
        for ok in (True, False):
            with self.subTest(ok=ok):
                native = {"ok": ok, "permission_denials": ["Monitor"]}
                self.assertEqual(engines.classify(native, exit_code=None, exit_signal=15, exit_known=True,
                                                  cancel_requested=True), "cancelled")

    def test_native_failure_and_cancel_still_wins_even_with_exit_zero(self) -> None:
        native = {"ok": False, "permission_denials": ["Monitor"]}
        self.assertEqual(engines.classify(native, exit_code=0, exit_signal=None, exit_known=True,
                                          cancel_requested=True), "cancelled")

    def test_cancel_after_native_success_and_exit_zero_loses_race(self) -> None:
        native = {"ok": True, "permission_denials": ["Monitor"]}
        self.assertEqual(engines.classify(native, exit_code=0, exit_signal=None, exit_known=True,
                                          cancel_requested=True), "succeeded")


if __name__ == "__main__":
    unittest.main()

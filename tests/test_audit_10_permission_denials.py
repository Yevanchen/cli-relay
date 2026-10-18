"""Audit #10: a structured permission denial means `needs_input`, even when the CLI also reports an error."""

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

    def test_denial_with_success_result_is_needs_input(self) -> None:
        native = {"ok": True, "permission_denials": ["Write"]}
        self.assertEqual(engines.classify(native, exit_code=0, exit_signal=None, exit_known=True,
                                          cancel_requested=False), "needs_input")

    def test_cancel_still_wins(self) -> None:
        native = {"ok": False, "permission_denials": ["Bash"]}
        self.assertEqual(engines.classify(native, exit_code=None, exit_signal=15, exit_known=True,
                                          cancel_requested=True), "cancelled")


if __name__ == "__main__":
    unittest.main()

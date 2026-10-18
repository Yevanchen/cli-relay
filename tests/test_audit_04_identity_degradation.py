"""Audit #4: an identity that cannot be compared must be "unknown", never "dead"."""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

from helpers import RelayTestCase, base_env, side_effects, wait_until

from cli_relay import procs


class IdentityComparison(unittest.TestCase):
    def test_token_from_another_source_is_unverifiable(self) -> None:
        # A token recorded by a different method (or precision) cannot prove the process is gone.
        self.assertIsNone(procs.same_process_alive(os.getpid(), "othersource:12345"))

    def test_linux_tokens_compare_boot_and_start(self) -> None:
        a = "linux:5e1f6c2a-0000-4000-8000-000000000001:1234"
        self.assertIs(procs._compare_tokens(a, a), True)
        self.assertIs(procs._compare_tokens(a, a[:-4] + "1235"), False)
        self.assertIs(procs._compare_tokens(a, "linux:5e1f6c2a-0000-4000-8000-000000000002:1234"), False,
                      "a different boot id means the recorded process is gone")
        self.assertIsNone(procs._compare_tokens(a, "linux::1234"), "an unreadable boot id is not comparable")

    @unittest.skipUnless(sys.platform == "darwin", "macOS kernel query")
    def test_failed_kernel_query_does_not_fall_back_to_a_different_token(self) -> None:
        token = procs.start_token(os.getpid())
        self.assertTrue(token.startswith("darwin:"))
        with mock.patch.object(procs, "_darwin_proc_info", return_value=None):
            self.assertIsNone(procs.same_process_alive(os.getpid(), token),
                              "a live process must not be reported dead because the probe degraded")


class ReconcileDoesNotReleaseUnverifiableKeepers(RelayTestCase):
    def test_live_keeper_with_an_incomparable_token_keeps_its_claim(self) -> None:
        env = base_env({"CLI_RELAY_TEST_PAUSE": "after_claim:4"})
        sub = self.submit("k-a04", duration=0.2, env=env)
        row = wait_until(lambda: (lambda r: r if r["state"] == "claimed" else None)(self.attempt_row(sub["task_id"])))
        conn = sqlite3.connect(str(self.home / "ledger.db"))
        conn.execute("UPDATE attempts SET keeper_token='othersource:1' WHERE attempt_id=?", (row["attempt_id"],))
        conn.commit()
        conn.close()
        st = self.status(sub["task_id"])
        self.assertNotIn(st["reconcile"][0]["action"], ("released_claim", "released_claim_and_kicked"),
                         "a keeper that may be alive must keep its claim")
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(len(side_effects(self.effects)), 1)


if __name__ == "__main__":
    unittest.main()

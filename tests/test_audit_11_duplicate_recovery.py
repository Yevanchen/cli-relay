"""Audit #11: re-sending a known key must return the existing task, even if new-task preconditions fail."""

from __future__ import annotations

import shutil
import unittest

from helpers import RelayTestCase, base_env


class DuplicateKeyRecovery(RelayTestCase):
    def test_duplicate_after_the_cwd_was_deleted(self) -> None:
        first = self.submit("k-a11-cwd", duration=0.1)
        self.cli("wait", first["task_id"], "--timeout", "30")
        shutil.rmtree(self.work)
        again = self.submit("k-a11-cwd", duration=0.1)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["task_id"], first["task_id"])
        self.assertEqual(again["state"], "succeeded")

    def test_duplicate_when_disk_space_is_low(self) -> None:
        first = self.submit("k-a11-disk", duration=0.1)
        self.cli("wait", first["task_id"], "--timeout", "30")
        again = self.submit("k-a11-disk", duration=0.1, env=base_env({"CLI_RELAY_MIN_FREE_BYTES": str(10 ** 18)}))
        self.assertEqual((again["status"], again["task_id"]), ("duplicate", first["task_id"]))

    def test_a_new_key_still_needs_an_existing_cwd_and_free_disk(self) -> None:
        missing = str(self.tmp / "missing")
        out = self.cli("submit", "--key", "k-a11-new", "--engine", "fixture", "--cwd", missing, "--prompt", "p",
                       expect=2)
        self.assertEqual(out["error"], "usage")
        out = self.cli("submit", "--key", "k-a11-new", "--engine", "fixture", "--cwd", str(self.work), "--prompt",
                       "p", env=base_env({"CLI_RELAY_MIN_FREE_BYTES": str(10 ** 18)}), expect=5)
        self.assertEqual(out["error"], "insufficient_disk")

    def test_same_key_with_a_different_cwd_is_a_conflict(self) -> None:
        self.submit("k-a11-other", duration=0.1)
        other = self.tmp / "elsewhere"
        other.mkdir()
        out = self.cli("submit", "--key", "k-a11-other", "--engine", "fixture", "--cwd", str(other), "--prompt",
                       "do the thing", "--option", "duration_s=0.1", "--option",
                       f'side_effect_file="{self.effects}"', expect=3)
        self.assertEqual(out["error"], "conflict")


if __name__ == "__main__":
    unittest.main()

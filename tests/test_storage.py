"""Storage failures: explicit errors, no silent empty ledger, no 'accepted' without a commit."""

from __future__ import annotations

import os
import stat
import unittest
import uuid

from helpers import RelayTestCase, base_env, db_rows, run_cli, side_effects

from cli_relay.ledger import Ledger


class StorageFailures(RelayTestCase):
    def test_uninitialized_home_is_an_explicit_error(self) -> None:
        nowhere = self.tmp / "nowhere"
        code, out = run_cli(nowhere, "status", "--key", "x")
        self.assertEqual((code, out["error"]), (5, "ledger_not_initialized"))
        self.assertFalse((nowhere / "ledger.db").exists())

    def test_missing_database_is_never_recreated_silently(self) -> None:
        sub = self.submit("k-missing", duration=0.1)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        for name in ("ledger.db", "ledger.db-wal", "ledger.db-shm"):
            try:
                (self.home / name).unlink()
            except FileNotFoundError:
                pass
        code, out = run_cli(self.home, "status", sub["task_id"])
        self.assertEqual((code, out["error"]), (5, "ledger_missing"))
        code, out = run_cli(self.home, "init")
        self.assertEqual((code, out["error"]), (5, "ledger_missing"))
        self.assertFalse((self.home / "ledger.db").exists())

    def test_corrupt_database_stops_new_work(self) -> None:
        sub = self.submit("k-corrupt", duration=0.1)
        self.cli("wait", sub["task_id"], "--timeout", "30")
        db = self.home / "ledger.db"
        for name in ("ledger.db-wal", "ledger.db-shm"):
            try:
                (self.home / name).unlink()
            except FileNotFoundError:
                pass
        with open(db, "r+b") as f:
            f.write(b"\x00garbage, not sqlite\x00" * 8)
        size = db.stat().st_size
        code, out = run_cli(self.home, "submit", "--key", "k-new", "--engine", "fixture", "--cwd", str(self.work),
                            "--prompt", "p")
        self.assertEqual((code, out["error"]), (5, "ledger_corrupt"))
        self.assertNotIn("accepted", out)
        code, out = run_cli(self.home, "status", sub["task_id"])
        self.assertEqual((code, out["error"]), (5, "ledger_corrupt"))
        code, out = run_cli(self.home, "doctor")
        self.assertEqual(code, 6)
        self.assertEqual(out["ledger"]["error"], "ledger_corrupt")
        self.assertEqual(db.stat().st_size, size, "the damaged file is left for forensics, not replaced")

    def test_low_disk_space_refuses_new_work(self) -> None:
        env = base_env({"CLI_RELAY_MIN_FREE_BYTES": str(10 ** 18)})
        out = self.submit("k-disk", env=env, expect=5)
        self.assertEqual(out["error"], "insufficient_disk")
        self.assertEqual(db_rows(self.home, "SELECT COUNT(*) FROM tasks")[0][0], 0)

    def test_low_disk_space_at_spawn_time_blocks_without_running(self) -> None:
        ledger = Ledger(self.home)
        payload = {"engine": "fixture", "cwd": str(self.work.resolve()), "prompt": "p",
                   "options": {"duration_s": 0.1, "side_effect_file": str(self.effects)}}
        _, task, _ = ledger.submit(scope="default", key="k-disk-late", payload=payload, prompt="p",
                                   native_session_id=str(uuid.uuid4()))
        env = base_env({"CLI_RELAY_MIN_FREE_BYTES": str(10 ** 18), "CLI_RELAY_KICK_GRACE_S": "0"})
        self.cli("reconcile", task["task_id"], env=env)  # starts the keeper, which re-checks the disk
        done = self.cli("wait", task["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["preflight"]["reason"], "insufficient_disk")
        self.assertEqual(len(side_effects(self.effects)), 0)
        self.assertEqual(self.cli("events", "--consumer", "c")["events"][0]["body"]["outcome"], "blocked")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_read_only_ledger_fails_without_reporting_accepted(self) -> None:
        db = self.home / "ledger.db"
        old_db, old_dir = stat.S_IMODE(db.stat().st_mode), stat.S_IMODE(self.home.stat().st_mode)
        os.chmod(db, 0o400)
        os.chmod(self.home, 0o500)
        try:
            out = self.submit("k-ro", expect=5)
            self.assertFalse(out["ok"])
            self.assertNotIn("accepted", out)
        finally:
            os.chmod(self.home, old_dir)
            os.chmod(db, old_db)
        self.assertEqual(db_rows(self.home, "SELECT COUNT(*) FROM tasks")[0][0], 0)


if __name__ == "__main__":
    unittest.main()

"""Audit #8: a billing violation must stay `blocked` even if the keeper dies before committing."""

from __future__ import annotations

import unittest

from helpers import RelayTestCase, base_env, needs_clean_cli_config, wait_until
from test_audit_03_provider_bypass import fake_codex

from cli_relay import procs

API_KEY_INIT = 'api_key_source="ANTHROPIC_API_KEY"'


class BillingViolationSurvivesKeeperCrash(RelayTestCase):
    def keeper_dead(self, task_id: str) -> bool:
        r = self.attempt_row(task_id)
        return bool(r["keeper_pid"]) and procs.same_process_alive(r["keeper_pid"], r["keeper_token"]) is False

    def finished(self, task_id: str) -> dict:
        # A detected guard stays unknown while the orphan CLI/group is still alive.
        return wait_until(lambda: (lambda s: s if s["attempts"][0]["state"] == "finished" else None)(
            self.status(task_id)), timeout=30, interval=0.3)

    def test_keeper_dies_after_exit_record(self) -> None:
        # The CLI reported an API-key source and finished "successfully" before the keeper could stop it.
        sub = self.submit("k-a08-w7", duration=0.0, extra_options=[API_KEY_INIT],
                          env=base_env({"CLI_RELAY_TEST_CRASH": "after_exit_before_commit"}))
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        done = self.finished(sub["task_id"])
        self.assertEqual(done["task"]["state"], "blocked")

    def test_keeper_dies_while_the_cli_runs(self) -> None:
        sub = self.submit("k-a08-w5", duration=1.0, extra_options=[API_KEY_INIT],
                          env=base_env({"CLI_RELAY_TEST_CRASH": "after_record"}))
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        done = self.finished(sub["task_id"])
        self.assertEqual(done["task"]["state"], "blocked")

    @needs_clean_cli_config
    def test_codex_provider_record_is_checked_after_a_keeper_crash(self) -> None:
        # The poller has neither CODEX_HOME nor the fake binary: reconciliation must still read the
        # thread record where the keeper's run wrote it.
        codex_home = self.tmp / "codex-home"
        codex_home.mkdir()
        fake = fake_codex(self.tmp, self.tmp / "spawned", rollout_provider="team")
        env = base_env({"CODEX_HOME": str(codex_home), "CLI_RELAY_CODEX_BIN": str(fake),
                        "CLI_RELAY_TEST_CRASH": "after_exit_before_commit"})
        sub = self.cli("submit", "--key", "k-a08-codex", "--engine", "codex", "--cwd", str(self.work),
                       "--prompt", "hi", env=env)
        wait_until(lambda: self.keeper_dead(sub["task_id"]))
        done = self.finished(sub["task_id"])
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["provider_check"]["reason"], "provider_not_subscription")


if __name__ == "__main__":
    unittest.main()

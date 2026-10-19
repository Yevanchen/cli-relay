"""A resumed Codex attempt needs its own appended permission evidence, including after a crash."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, needs_clean_cli_config
from cli_relay import engines

THREAD = "22222222-3333-4444-8555-666666666666"


def turn(kind="read-only") -> dict:
    return {"type": "turn_context", "payload": {"sandbox_policy": {"type": kind},
                                                "active_permission_profile": None}}


class CurrentAttemptPermissionEvidence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-checkpoint-"))
        self.env = {"CODEX_HOME": str(self.tmp)}
        self.path = self.tmp / "sessions/2026/10/19" / f"rollout-test-{THREAD}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n")
        self.append(turn())
        self.checkpoint = engines.codex_permission_checkpoint(self.env, THREAD)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def append(self, record: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def check(self, checkpoint=None):
        return engines.permission_violation("codex", self.env, THREAD, "read-only",
                                            permission_checkpoint=self.checkpoint if checkpoint is None else checkpoint,
                                            resume_session_id=THREAD)

    def test_old_context_does_not_certify_current_attempt(self) -> None:
        self.assertEqual(self.check(), {"reason": "permissions_unverified", "missing": "current_turn_context"})

    def test_new_safe_context_passes_and_wider_context_blocks(self) -> None:
        self.append(turn())
        self.assertIsNone(self.check())
        self.append(turn("workspace-write"))
        self.assertEqual(self.check()["reason"], "sandbox_not_as_requested")

    def test_missing_or_malformed_checkpoint_blocks(self) -> None:
        for checkpoint in ({}, {**self.checkpoint, "offset": True}, {**self.checkpoint, "offset": -1}):
            with self.subTest(checkpoint=checkpoint):
                self.assertEqual(self.check(checkpoint)["missing"], "permission_checkpoint")

    def test_replaced_or_truncated_rollout_blocks(self) -> None:
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.append(turn())
        self.assertEqual(self.check()["missing"], "unchanged_rollout")
        self.checkpoint = engines.codex_permission_checkpoint(self.env, THREAD)
        self.path.write_text("")
        self.assertEqual(self.check()["missing"], "unchanged_rollout")

    def test_initial_attempt_does_not_need_checkpoint(self) -> None:
        self.assertIsNone(engines.permission_violation("codex", self.env, THREAD, "read-only"))

    def test_malformed_sandbox_is_blocked_without_raising(self) -> None:
        self.append(turn(["read-only"]))
        self.assertEqual(self.check()["reason"], "sandbox_not_as_requested")

    def test_malformed_provider_is_blocked_without_raising(self) -> None:
        self.path.write_text(json.dumps({"type": "session_meta", "payload": ["openai"]}) + "\n")
        self.assertEqual(engines.provider_violation("codex", self.env, THREAD)["reason"],
                         "provider_not_subscription")

    def test_decide_requires_checkpoint_for_resume(self) -> None:
        state, evidence = engines.decide("codex", {"native": {"ok": True}}, env=self.env, session_id=THREAD,
                                          exit_code=0, exit_signal=None, exit_known=True, cancel_requested=False,
                                          sandbox="read-only", resume_session_id=THREAD)
        self.assertEqual(state, "blocked")
        self.assertEqual(evidence["permission_check"]["missing"], "permission_checkpoint")


@needs_clean_cli_config
class CheckpointSurvivesKeeperCrash(RelayTestCase):
    def run_resume(self, append: bool, crash: bool, remove_rollout: bool = False) -> dict:
        codex_home = self.tmp / "codex-home"
        codex_home.mkdir()
        fake = self.tmp / "codex"
        context = json.dumps(turn())
        meta = json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}})
        fake.write_text(
            "#!/bin/sh\n"
            'case "$1" in login) echo "Logged in using ChatGPT"; exit 0;; esac\n'
            f"printf 'spawned\\n' >> '{self.tmp / 'model-runs'}'\n"
            'mkdir -p "$CODEX_HOME/sessions/2026/10/19"\n'
            f'rollout="$CODEX_HOME/sessions/2026/10/19/rollout-test-{THREAD}.jsonl"\n'
            f'if [ ! -f "$rollout" ]; then printf \'%s\\n\' \'{meta}\' \'{context}\' > "$rollout"; '
            + (f'else printf \'%s\\n\' \'{context}\' >> "$rollout"; ' if append else "") + "fi\n"
            f"printf '%s\\n' '{{\"type\":\"thread.started\",\"thread_id\":\"{THREAD}\"}}' "
            "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"hi\"}}' "
            "'{\"type\":\"turn.completed\",\"usage\":{}}'\n")
        fake.chmod(0o755)
        env = base_env({"CODEX_HOME": str(codex_home), "CLI_RELAY_CODEX_BIN": str(fake)})
        sub = self.cli("submit", "--key", "checkpoint", "--engine", "codex", "--cwd", str(self.work),
                       "--prompt", "hi", env=env)
        initial = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(initial["task"]["state"], "succeeded")
        if remove_rollout:
            (codex_home / "sessions/2026/10/19" / f"rollout-test-{THREAD}.jsonl").unlink()
        if crash:
            env["CLI_RELAY_TEST_CRASH"] = "after_exit_before_commit"
        self.cli("resume", sub["task_id"], "--prompt", "hi", env=env)
        return self.cli("wait", sub["task_id"], "--timeout", "30")

    def test_old_only_context_is_blocked_by_keeper_and_reconciliation(self) -> None:
        done = self.run_resume(append=False, crash=True)
        self.assertEqual(done["task"]["state"], "blocked")
        last = done["attempts"][-1]
        self.assertIn("codex_permission_checkpoint", last["evidence"])
        self.assertEqual(last["evidence"]["permission_check"]["missing"], "current_turn_context")

    def test_new_context_is_accepted_after_keeper_crash(self) -> None:
        done = self.run_resume(append=True, crash=True)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(done["attempts"][-1]["resume_session_id"], THREAD)

    def test_old_only_context_is_blocked_without_keeper_crash(self) -> None:
        done = self.run_resume(append=False, crash=False)
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][-1]["evidence"]["permission_check"]["missing"], "current_turn_context")

    def test_missing_rollout_blocks_before_resumed_cli_spawns(self) -> None:
        done = self.run_resume(append=True, crash=False, remove_rollout=True)
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual((self.tmp / "model-runs").read_text().splitlines(), ["spawned"])

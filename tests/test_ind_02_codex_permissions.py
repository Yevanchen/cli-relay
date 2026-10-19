"""Independent audit 2: the legacy ``--sandbox`` flag is not the only thing that decides a Codex
run's permissions. Named permission profiles, requirements and config-profile files can change
them, so (a) a configuration that uses any of these is blocked before the run, and (b) after the
run the thread's own record must show every turn in the requested sandbox with no named profile.
Nothing here widens a permission or changes the user's configuration."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, isolated_env, needs_clean_cli_config

from cli_relay import engines

THREAD = "22222222-3333-4444-8555-666666666666"


def fake_codex(dirpath: Path, marker: Path, *, turns=({"type": "read-only"},), active_profile=None) -> Path:
    """A codex stand-in that writes a rollout like 0.163.0: session_meta, then one turn_context per turn."""
    lines = [json.dumps({"type": "session_meta", "payload": {"session_id": THREAD, "model_provider": "openai"}})]
    for policy in turns:
        lines.append(json.dumps({"type": "turn_context", "payload": {"sandbox_policy": policy,
                                                                     "active_permission_profile": active_profile}}))
    path = dirpath / "codex"
    rollout = "\n".join(lines).replace("'", "'\\''")
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in login) echo "Logged in using ChatGPT"; exit 0;; esac\n'
        f"echo spawned >> '{marker}'\n"
        'mkdir -p "$CODEX_HOME/sessions/2026/10/19"\n'
        f"printf '%s\\n' '{rollout}' > \"$CODEX_HOME/sessions/2026/10/19/rollout-2026-10-19T00-00-00-{THREAD}.jsonl\"\n"
        f"printf '%s\\n' '{{\"type\":\"thread.started\",\"thread_id\":\"{THREAD}\"}}' "
        "'{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"hi\"}}' "
        "'{\"type\":\"turn.completed\",\"usage\":{}}'\n"
        "exit 0\n")
    path.chmod(0o755)
    return path


@needs_clean_cli_config
class PermissionConfigurationIsBlockedBeforeTheRun(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-ind02-"))
        self.env = isolated_env(self.tmp, {"CLI_RELAY_CODEX_BIN": str(fake_codex(self.tmp, self.tmp / "m"))})
        self.codex_home = Path(self.env["CODEX_HOME"])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def blocked(self, config: str, extra_files=()) -> bool:
        (self.codex_home / "config.toml").write_text(config)
        for name, text in extra_files:
            (self.codex_home / name).write_text(text)
        return not engines.preflight("codex", self.env).ok

    def test_plain_configuration_still_runs(self) -> None:
        self.assertFalse(self.blocked('model = "gpt-test"\n'))

    def test_named_default_permissions_profile(self) -> None:
        self.assertTrue(self.blocked('default_permissions = "workspace"\n'))

    def test_permission_profile_tables(self) -> None:
        self.assertTrue(self.blocked('[permissions.workspace]\nfilesystem = "write"\n'))

    def test_sandbox_permission_overrides(self) -> None:
        self.assertTrue(self.blocked('sandbox_permissions = ["disk-full-read-access"]\n'))

    def test_requirements_file_cannot_be_evaluated(self) -> None:
        self.assertTrue(self.blocked('model = "gpt-test"\n', [("requirements.toml", 'allowed_sandbox_modes = ["read-only"]\n')]))

    def test_selected_config_profile_file_is_read(self) -> None:
        # Codex 0.163.0: `profile = "work"` layers $CODEX_HOME/work.config.toml on top of config.toml.
        self.assertTrue(self.blocked('profile = "work"\n\n[profiles.work]\nmodel = "gpt-test"\n',
                                     [("work.config.toml", 'model_provider = "team"\n')]))


@needs_clean_cli_config
class EffectivePermissionsAreVerifiedAfterTheRun(RelayTestCase):
    def run_codex(self, key: str, **fake) -> dict:
        codex_home = self.tmp / f"codex-home-{key}"
        codex_home.mkdir()
        env = base_env({"CODEX_HOME": str(codex_home),
                        "CLI_RELAY_CODEX_BIN": str(fake_codex(self.tmp, self.tmp / f"m-{key}", **fake))})
        sub = self.cli("submit", "--key", key, "--engine", "codex", "--cwd", str(self.work), "--prompt", "hi", env=env)
        return self.cli("wait", sub["task_id"], "--timeout", "30")

    def test_requested_sandbox_and_no_named_profile_succeeds(self) -> None:
        done = self.run_codex("k-ind02-ok")
        self.assertEqual(done["task"]["state"], "succeeded", done["attempts"][0]["evidence"])

    def test_a_turn_with_a_wider_sandbox_is_blocked(self) -> None:
        done = self.run_codex("k-ind02-wide", turns=({"type": "read-only"}, {"type": "workspace-write"}))
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["permission_check"]["reason"], "sandbox_not_as_requested")

    def test_an_active_named_permission_profile_is_blocked(self) -> None:
        done = self.run_codex("k-ind02-profile", active_profile="workspace")
        self.assertEqual(done["task"]["state"], "blocked")

    def test_a_run_without_a_recorded_sandbox_is_blocked(self) -> None:
        done = self.run_codex("k-ind02-none", turns=())
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["permission_check"]["reason"], "permissions_unverified")


if __name__ == "__main__":
    unittest.main()

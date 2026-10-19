"""Audit #3: "subscription only" must not be bypassable through provider configuration."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, needs_clean_cli_config

from cli_relay import engines

THREAD = "11111111-2222-4333-8444-555555555555"


def fake_codex(dirpath: Path, marker: Path, rollout_provider: str = "openai") -> Path:
    path = dirpath / "codex"
    # Like Codex 0.163.0: session_meta, then a turn_context with the turn's effective sandbox.
    rollout = (f'{{"type":"session_meta","payload":{{"session_id":"{THREAD}","model_provider":"{rollout_provider}"}}}}'
               '\n{"type":"turn_context","payload":{"sandbox_policy":{"type":"read-only"},'
               '"active_permission_profile":null}}')
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


def fake_claude(dirpath: Path) -> Path:
    path = dirpath / "claude"
    path.write_text("#!/bin/sh\n"
                    "case \"$1\" in auth) echo '{\"loggedIn\": true, \"authMethod\": \"claude.ai\", "
                    "\"apiProvider\": \"firstParty\"}'; exit 0;; esac\nexit 0\n")
    path.chmod(0o755)
    return path


@needs_clean_cli_config
class ProviderConfigPreflight(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-a03-"))
        self.codex_home = self.tmp / "codex-home"
        self.codex_home.mkdir()
        self.claude_dir = self.tmp / "claude-config"
        self.claude_dir.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.marker = self.tmp / "spawned"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "CODEX_HOME": str(self.codex_home),
                    "CLAUDE_CONFIG_DIR": str(self.claude_dir),
                    "CLI_RELAY_CODEX_BIN": str(fake_codex(self.tmp, self.marker)),
                    "CLI_RELAY_CLAUDE_BIN": str(fake_claude(self.tmp))}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_configuration_passes(self) -> None:
        (self.codex_home / "config.toml").write_text('model = "gpt-test"\n')
        self.assertTrue(engines.preflight("codex", self.env).ok)
        self.assertTrue(engines.preflight("claude", self.env).ok)

    def test_codex_custom_provider_with_its_own_key_is_blocked(self) -> None:
        (self.codex_home / "config.toml").write_text(
            'model_provider = "team"\n\n[model_providers.team]\nname = "team"\n'
            'base_url = "https://gateway.example/v1"\nenv_key = "TEAM_PROVIDER_TOKEN"\n')
        env = dict(self.env, TEAM_PROVIDER_TOKEN="team-secret")
        pf = engines.preflight("codex", env)
        self.assertFalse(pf.ok, "a ChatGPT login does not prove which provider serves the run")
        self.assertNotIn("team-secret", json.dumps(pf.detail))

    def test_codex_profile_selecting_a_custom_provider_is_blocked(self) -> None:
        (self.codex_home / "config.toml").write_text(
            'profile = "work"\n\n[profiles.work]\nmodel_provider = "team"\n')
        self.assertFalse(engines.preflight("codex", self.env).ok)

    def test_codex_project_configuration_in_the_task_cwd_is_checked(self) -> None:
        (self.work / ".codex").mkdir()
        (self.work / ".codex" / "config.toml").write_text('model_provider = "team"\n')
        self.assertFalse(engines.preflight("codex", self.env, cwd=str(self.work)).ok)

    def test_claude_api_key_helper_in_settings_is_blocked(self) -> None:
        (self.claude_dir / "settings.json").write_text(json.dumps({"apiKeyHelper": "/bin/echo key"}))
        self.assertFalse(engines.preflight("claude", self.env).ok)

    def test_claude_gateway_in_settings_env_is_blocked(self) -> None:
        (self.work / ".claude").mkdir()
        (self.work / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}}))
        self.assertFalse(engines.preflight("claude", self.env, cwd=str(self.work)).ok)


@needs_clean_cli_config
class ProviderGuardsAtRunTime(RelayTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.codex_home = self.tmp / "codex-home"
        self.codex_home.mkdir()
        self.marker = self.tmp / "spawned"

    def run_codex(self, key: str, fake: Path) -> dict:
        env = base_env({"CODEX_HOME": str(self.codex_home), "CLI_RELAY_CODEX_BIN": str(fake)})
        sub = self.cli("submit", "--key", key, "--engine", "codex", "--cwd", str(self.work), "--prompt", "hi",
                       env=env)
        return self.cli("wait", sub["task_id"], "--timeout", "30")

    def test_custom_provider_blocks_before_the_cli_starts(self) -> None:
        (self.codex_home / "config.toml").write_text('model_provider = "team"\n')
        done = self.run_codex("k-a03-pre", fake_codex(self.tmp, self.marker))
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertFalse(self.marker.exists(), "the CLI must not be started")

    def test_provider_recorded_by_the_run_is_verified(self) -> None:
        # A configuration layer the preflight did not see: the run itself records a non-default provider.
        done = self.run_codex("k-a03-post", fake_codex(self.tmp, self.marker, rollout_provider="team"))
        self.assertTrue(self.marker.exists())
        self.assertEqual(done["task"]["state"], "blocked", "a run served by another provider is not a success")


if __name__ == "__main__":
    unittest.main()

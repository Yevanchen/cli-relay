"""Independent audit 6: a configuration file with an unexpected structure (e.g. ``model_providers = 1``)
made the preflight raise. The keeper died before its spawn record, reconciliation re-kicked it, and
the task stayed `accepted` with no event. Any such configuration must converge to `blocked`."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, isolated_env, needs_clean_cli_config, wait_until
from test_audit_03_provider_bypass import fake_claude, fake_codex

from cli_relay import engines

CODEX_ANOMALIES = [
    "model_providers = 1\n",
    "model_providers = [1, 2]\n",
    'profiles = 1\nprofile = "work"\n',
    "profile = 1\n",
    "model_provider = 1\n",
    'profile = "work"\n\n[profiles]\nwork = 3\n',
    "forced_login_method = 1\n",
    "openai_base_url = 1\n",
    '[model_providers]\nopenai = "x"\n',
]
CLAUDE_ANOMALIES = [
    {"permissions": "bypass"},
    {"env": ["ANTHROPIC_API_KEY"]},
    {"forceLoginMethod": 1},
    {"apiKeyHelper": ["x"]},
    {"permissions": {"defaultMode": ["bypassPermissions"]}},
]


@needs_clean_cli_config
class PreflightNeverRaises(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-ind06-"))
        self.env = isolated_env(self.tmp, {"CLI_RELAY_CODEX_BIN": str(fake_codex(self.tmp, self.tmp / "m")),
                                           "CLI_RELAY_CLAUDE_BIN": str(fake_claude(self.tmp))})

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_codex_configuration_with_unexpected_types_blocks(self) -> None:
        for text in CODEX_ANOMALIES:
            with self.subTest(config=text):
                (Path(self.env["CODEX_HOME"]) / "config.toml").write_text(text)
                pf = engines.preflight("codex", self.env)
                self.assertFalse(pf.ok)

    def test_claude_settings_with_unexpected_types_block(self) -> None:
        for data in CLAUDE_ANOMALIES:
            with self.subTest(settings=data):
                (Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(json.dumps(data))
                pf = engines.preflight("claude", self.env)
                self.assertFalse(pf.ok)


@needs_clean_cli_config
class AnomalousConfigurationConvergesToBlocked(RelayTestCase):
    def test_the_task_reaches_blocked_with_one_event_and_no_cli_start(self) -> None:
        codex_home = self.tmp / "codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text("model_providers = 1\n")
        marker = self.tmp / "spawned"
        env = base_env({"CODEX_HOME": str(codex_home), "CLI_RELAY_CODEX_BIN": str(fake_codex(self.tmp, marker))})
        sub = self.cli("submit", "--key", "k-ind06", "--engine", "codex", "--cwd", str(self.work), "--prompt", "hi",
                       env=env)
        done = wait_until(lambda: (lambda s: s if s["task"]["state"] not in ("accepted", "running") else None)(
            self.status(sub["task_id"])), timeout=30, interval=0.5)
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual([e["kind"] for e in done["events"]], ["terminal"])
        self.assertFalse(marker.exists(), "the CLI must not be started")
        self.assertEqual(len(done["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()

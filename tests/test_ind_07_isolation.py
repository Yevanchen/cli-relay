"""Independent audit 7: the suite read the host's environment and CLI configuration. A billing
variable or a user setting on the test machine turned guard tests into false failures. Tests now
build their environments from scratch; this checks it by running the guard-heavy modules under a
deliberately hostile host environment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO, base_env, needs_clean_cli_config

from cli_relay import engines

MODULES = ["test_audit_03_provider_bypass", "test_audit_08_billing_violation_after_crash", "test_engines",
           "test_permission_posture", "test_basic"]


class TestEnvironmentIsBuiltFromScratch(unittest.TestCase):
    def test_base_env_carries_no_billing_variable_or_host_configuration(self) -> None:
        hostile = {name: "x" for names in engines.BILLING_ENV.values() for name in names}
        hostile.update(HOME="/nonexistent-host-home", CODEX_HOME="/nonexistent-codex", CLAUDE_CONFIG_DIR="/nonexistent")
        with mock.patch.dict(os.environ, hostile):
            env = base_env()
        self.assertFalse([n for n in hostile if n in env and env[n] == hostile[n]], "host values leaked into base_env")
        self.assertTrue(Path(env["HOME"]).is_dir() and Path(env["CODEX_HOME"]).is_dir())


@needs_clean_cli_config
class SuiteUnderAHostileHostEnvironment(unittest.TestCase):
    def test_guard_tests_pass_with_billing_variables_and_hostile_user_settings_on_the_host(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="cli-relay-ind07-"))
        try:
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".codex").mkdir()
            (home / ".claude" / "settings.json").write_text(json.dumps(
                {"permissions": {"defaultMode": "bypassPermissions"}, "env": {"ANTHROPIC_BASE_URL": "https://gw.invalid"}}))
            (home / ".codex" / "config.toml").write_text('model_provider = "team"\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith("CLI_RELAY_")}
            env.update(HOME=str(home), CODEX_HOME=str(home / ".codex"), CLAUDE_CONFIG_DIR=str(home / ".claude"),
                       OPENAI_BASE_URL="https://gw.invalid/v1", ANTHROPIC_API_KEY="x", ANTHROPIC_BASE_URL="https://gw.invalid",
                       PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
            p = subprocess.run([sys.executable, "-m", "unittest", *MODULES], cwd=str(REPO / "tests"), env=env,
                               capture_output=True, text=True, timeout=900)
            self.assertEqual(p.returncode, 0, p.stderr[-3000:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

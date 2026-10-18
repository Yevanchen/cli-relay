"""Permission posture does not depend on the CLIs' current configuration.

Codex always gets an explicit sandbox (read-only unless the task asks for workspace-write), on
the first run and on resume. A Claude configuration or init event that reports
bypassPermissions blocks the run, since nothing would ever be denied.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, side_effects, wait_until
from test_audit_03_provider_bypass import fake_claude

from cli_relay import engines, procs


class CodexSandboxIsAlwaysExplicit(unittest.TestCase):
    def build(self, *, resume=None, options=None):
        attempt = {"native_session_id": None, "resume_session_id": resume, "attempt_id": "a-test"}
        return engines.build("codex", payload={"cwd": "/tmp", "options": options or {}}, attempt=attempt,
                             run_dir=Path("/tmp"), fixture_state_dir="/tmp",
                             env={"PATH": "/usr/bin:/bin", "CLI_RELAY_CODEX_BIN": "/bin/echo"}).argv

    def test_no_sandbox_option_means_read_only_on_both_runs(self) -> None:
        first = self.build()
        self.assertEqual(first[first.index("--sandbox") + 1], "read-only")
        resumed = self.build(resume="44444444-4444-4444-8444-444444444444")
        self.assertEqual(resumed[1:5], ["exec", "--sandbox", "read-only", "resume"])
        self.assertEqual(resumed[resumed.index("-c") + 1], 'sandbox_mode="read-only"')

    def test_requested_sandbox_is_kept_on_both_runs(self) -> None:
        first = self.build(options={"sandbox": "workspace-write"})
        resumed = self.build(resume="44444444-4444-4444-8444-444444444444", options={"sandbox": "workspace-write"})
        self.assertEqual(first[first.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(resumed[resumed.index("--sandbox") + 1], "workspace-write")


class ClaudeBypassModeIsBlocked(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-perm-"))
        (self.tmp / "claude-config").mkdir()
        (self.tmp / "work" / ".claude").mkdir(parents=True)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude-config"),
                    "CLI_RELAY_CLAUDE_BIN": str(fake_claude(self.tmp))}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_settings(self) -> None:
        (self.tmp / "claude-config" / "settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}))
        self.assertFalse(engines.preflight("claude", self.env).ok)

    def test_project_settings_of_the_task_directory(self) -> None:
        (self.tmp / "work" / ".claude" / "settings.local.json").write_text(
            json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}))
        self.assertTrue(engines.preflight("claude", self.env).ok, "not visible without the task directory")
        self.assertFalse(engines.preflight("claude", self.env, cwd=str(self.tmp / "work")).ok)


class BypassModeReportedByTheCli(RelayTestCase):
    BYPASS = 'permission_mode="bypassPermissions"'

    def test_the_run_is_stopped_and_blocked(self) -> None:
        sub = self.submit("k-bypass", duration=10, extra_options=[self.BYPASS])
        done = self.cli("wait", sub["task_id"], "--timeout", "40")
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["terminated"]["reason"], "bypass_permissions_mode")
        self.assertEqual(side_effects(self.effects), [])

    def test_still_blocked_when_the_keeper_died_before_committing(self) -> None:
        sub = self.submit("k-bypass-w7", duration=0, extra_options=[self.BYPASS],
                          env=base_env({"CLI_RELAY_TEST_CRASH": "after_exit_before_commit"}))
        wait_until(lambda: (lambda r: bool(r["keeper_pid"]) and procs.same_process_alive(
            r["keeper_pid"], r["keeper_token"]) is False)(self.attempt_row(sub["task_id"])))
        done = wait_until(lambda: (lambda s: s if s["task"]["state"] not in ("accepted", "running") else None)(
            self.status(sub["task_id"])), timeout=30, interval=0.3)
        self.assertEqual(done["task"]["state"], "blocked")


if __name__ == "__main__":
    unittest.main()

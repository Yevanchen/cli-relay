"""Engine adapters: no permission relaxation, subscription-only auth, faithful result parsing."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, side_effects

from cli_relay import engines
from cli_relay.errors import UsageError


def _fake_cli(dirpath: Path, name: str, auth_output: str, marker: Path) -> Path:
    path = dirpath / name
    path.write_text("#!/bin/sh\n"
                    f"case \"$1\" in auth|login) printf '%s\\n' '{auth_output}'; exit 0;; esac\n"
                    f"echo spawned >> '{marker}'\nexit 0\n")
    path.chmod(0o755)
    return path


class ArgvSafety(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-eng-"))
        self.marker = self.tmp / "spawned"
        self.claude = _fake_cli(self.tmp, "claude", '{"loggedIn": true, "authMethod": "claude.ai"}', self.marker)
        self.codex = _fake_cli(self.tmp, "codex", "Logged in using ChatGPT", self.marker)
        self.env = {"PATH": "/usr/bin:/bin", "CLI_RELAY_CLAUDE_BIN": str(self.claude),
                    "CLI_RELAY_CODEX_BIN": str(self.codex)}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, engine: str, *, resume: str = None, options: dict = None) -> engines.Spec:
        attempt = {"native_session_id": "11111111-1111-4111-8111-111111111111", "resume_session_id": resume,
                   "attempt_id": "a-test"}
        return engines.build(engine, payload={"cwd": str(self.tmp), "options": options or {}}, attempt=attempt,
                             run_dir=self.tmp, fixture_state_dir=str(self.tmp), env=self.env)

    def test_claude_argv(self) -> None:
        new = self.build("claude").argv
        self.assertEqual(new[1:], ["-p", "--output-format", "stream-json", "--verbose", "--session-id",
                                   "11111111-1111-4111-8111-111111111111", "--permission-prompts", "none"])
        res = self.build("claude", resume="22222222-2222-4222-8222-222222222222").argv
        self.assertIn("--resume", res)
        self.assertNotIn("--session-id", res)
        for flag in ("--continue", "-c", "--dangerously-skip-permissions", "--bare", "--permission-mode"):
            self.assertNotIn(flag, new + res)

    def test_codex_argv(self) -> None:
        new = self.build("codex", options={"sandbox": "read-only"}).argv
        self.assertEqual(new[1:4], ["exec", "--json", "-C"])
        self.assertEqual(new[-1], "-")
        self.assertEqual(new[new.index("--sandbox") + 1], "read-only")
        res = self.build("codex", resume="33333333-3333-4333-8333-333333333333").argv
        self.assertEqual(res[1:4], ["exec", "resume", "--json"])
        self.assertEqual(res[-2:], ["33333333-3333-4333-8333-333333333333", "-"])
        for flag in ("--last", "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me", "--full-auto"):
            self.assertNotIn(flag, new + res)

    def test_forbidden_flags_and_full_access_are_rejected(self) -> None:
        for engine, argv in (("codex", ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]),
                             ("codex", ["codex", "exec", "--sandbox", "danger-full-access"]),
                             ("codex", ["codex", "exec", "resume", "--last"]),
                             ("claude", ["claude", "-p", "--dangerously-skip-permissions"]),
                             ("claude", ["claude", "-p", "--continue"])):
            with self.assertRaises(RuntimeError):
                engines.assert_safe(engine, argv)
        with self.assertRaises(UsageError):
            engines.validate_options("codex", {"sandbox": "danger-full-access"})
        with self.assertRaises(UsageError):
            engines.validate_options("claude", {"permission_mode": "bypassPermissions"})

    def test_billing_env_blocks_and_never_reports_values(self) -> None:
        env = dict(self.env, ANTHROPIC_API_KEY="sk-secret-value-123")
        pf = engines.preflight("claude", env)
        self.assertFalse(pf.ok)
        self.assertEqual(pf.reason, "billing_env_present")
        self.assertEqual(pf.detail, {"names": ["ANTHROPIC_API_KEY"]})
        self.assertNotIn("sk-secret", json.dumps(pf.detail))
        pf = engines.preflight("codex", dict(self.env, OPENAI_API_KEY="sk-other"))
        self.assertEqual((pf.ok, pf.reason), (False, "billing_env_present"))

    def test_auth_must_be_the_subscription_login(self) -> None:
        self.assertTrue(engines.preflight("claude", self.env).ok)
        self.assertTrue(engines.preflight("codex", self.env).ok)
        api_claude = _fake_cli(self.tmp, "claude-api", '{"loggedIn": true, "authMethod": "api_key"}', self.marker)
        pf = engines.preflight("claude", dict(self.env, CLI_RELAY_CLAUDE_BIN=str(api_claude)))
        self.assertEqual((pf.ok, pf.reason), (False, "auth_not_subscription"))
        api_codex = _fake_cli(self.tmp, "codex-api", "Logged in using an API key - sk-***", self.marker)
        pf = engines.preflight("codex", dict(self.env, CLI_RELAY_CODEX_BIN=str(api_codex)))
        self.assertEqual((pf.ok, pf.reason), (False, "auth_not_subscription"))
        self.assertFalse(self.marker.exists(), "auth checks must not start a run")

    def test_nested_session_markers_are_scrubbed(self) -> None:
        env = engines.child_env({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "CLI_RELAY_TEST_CRASH": "y",
                                 "PATH": "/bin"})
        self.assertEqual(env, {"PATH": "/bin"})


class OutputParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-parse-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, events: list) -> Path:
        path = self.tmp / "stdout.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in events) + "not json\n")
        return path

    def test_claude_stream(self) -> None:
        path = self.write([
            {"type": "system", "subtype": "init", "session_id": "S1", "apiKeySource": "none",
             "permissionMode": "default", "model": "m"},
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "S1",
             "num_turns": 2, "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm"}}]},
        ])
        parsed = engines.parse_output("claude", path)
        self.assertEqual(parsed["session_id"], "S1")
        self.assertEqual(parsed["native"]["permission_denials"], ["Bash"])
        self.assertEqual(engines.classify(parsed["native"], exit_code=0, exit_signal=None, exit_known=True,
                                          cancel_requested=False), "needs_input")

    def test_claude_error_and_missing_result(self) -> None:
        path = self.write([{"type": "result", "subtype": "error_during_execution", "is_error": True,
                            "result": "No conversation found", "session_id": "S"}])
        native = engines.parse_output("claude", path)["native"]
        self.assertFalse(native["ok"])
        self.assertEqual(engines.classify(native, exit_code=1, exit_signal=None, exit_known=True,
                                          cancel_requested=False), "failed")
        empty = self.write([{"type": "system", "subtype": "init", "session_id": "S"}])
        self.assertIsNone(engines.parse_output("claude", empty)["native"])

    def test_codex_stream(self) -> None:
        path = self.write([
            {"type": "thread.started", "thread_id": "T-1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "hello"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ])
        parsed = engines.parse_output("codex", path)
        self.assertEqual(parsed["session_id"], "T-1")
        self.assertEqual(parsed["native"], {"ok": True, "status": "completed", "thread_id": "T-1",
                                            "result": "hello", "error": None})
        failed = self.write([{"type": "thread.started", "thread_id": "T-2"},
                             {"type": "turn.failed", "error": {"message": "boom"}}])
        self.assertFalse(engines.parse_output("codex", failed)["native"]["ok"])

    def test_classification_rules(self) -> None:
        ok = {"ok": True}
        self.assertEqual(engines.classify(ok, exit_code=0, exit_signal=None, exit_known=True,
                                          cancel_requested=False), "succeeded")
        self.assertEqual(engines.classify(ok, exit_code=3, exit_signal=None, exit_known=True,
                                          cancel_requested=False), "failed")
        self.assertEqual(engines.classify(ok, exit_code=None, exit_signal=None, exit_known=False,
                                          cancel_requested=False), "succeeded")
        self.assertEqual(engines.classify(None, exit_code=None, exit_signal=15, exit_known=True,
                                          cancel_requested=True), "cancelled")
        self.assertEqual(engines.classify(ok, exit_code=0, exit_signal=None, exit_known=True,
                                          cancel_requested=True), "succeeded")


class ProcessGuards(RelayTestCase):
    def test_billing_env_blocks_a_real_run_before_spawn(self) -> None:
        marker = self.tmp / "spawned"
        fake = _fake_cli(self.tmp, "claude", '{"loggedIn": true, "authMethod": "claude.ai"}', marker)
        env = base_env({"CLI_RELAY_CLAUDE_BIN": str(fake), "ANTHROPIC_API_KEY": "sk-never-print-me"})
        sub = self.cli("submit", "--key", "k-billing", "--engine", "claude", "--cwd", str(self.work),
                       "--prompt", "hello", env=env)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["preflight"]["reason"], "billing_env_present")
        self.assertFalse(marker.exists(), "the CLI must not be started")
        self.assertNotIn("sk-never-print-me", json.dumps(done))

    def test_api_key_source_reported_by_the_cli_stops_the_run(self) -> None:
        sub = self.submit("k-keysource", duration=10,
                          extra_options=['api_key_source="ANTHROPIC_API_KEY"'])
        done = self.cli("wait", sub["task_id"], "--timeout", "40")
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["terminated"]["reason"], "api_key_source_detected")
        self.assertEqual(len(side_effects(self.effects)), 0)


if __name__ == "__main__":
    unittest.main()

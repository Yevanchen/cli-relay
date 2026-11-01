"""Scoped permission validation and the public flags/receipt/notification contract."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, isolated_env, needs_clean_cli_config
from test_task_local_model_config import recording_cli
from cli_relay import engines, notify
from cli_relay.errors import UsageError


class ScopedPermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name).resolve() / "work"
        self.cwd.mkdir()

    def validate(self, options):
        return engines.validate_options("claude", options, cwd=str(self.cwd))

    def build(self, options, resume=None):
        return engines.build("claude", payload={"cwd": str(self.cwd), "options": options},
                             attempt={"native_session_id": "sid", "resume_session_id": resume},
                             run_dir=self.cwd, fixture_state_dir=str(self.cwd),
                             env={"CLI_RELAY_CLAUDE_BIN": "/bin/echo"}).argv

    def test_canonical_rules_on_first_and_resumed_runs(self):
        options = {"allowed_write_dirs": ["out", str(self.cwd / "out"), "./out/**", "reports"],
                   "allowed_bash_commands": ["python3", "pip install --user"], "allowed_tools": ["WebFetch"]}
        clean = self.validate(options)
        self.assertEqual(clean["allowed_write_dirs"], ["./out/**", "./reports/**"])
        expected = ["Bash(pip install --user:*)", "Bash(python3:*)", "Edit(./out/**)",
                    "Edit(./reports/**)", "WebFetch", "Write(./out/**)", "Write(./reports/**)"]
        for resume in (None, "old-session"):
            argv = self.build(options, resume)
            self.assertEqual(argv[argv.index("--allowedTools") + 1], ",".join(expected))
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            engines.assert_safe("claude", argv, options=clean, cwd=str(self.cwd))
            with self.assertRaises(RuntimeError):
                engines.assert_safe("claude", argv, options={}, cwd=str(self.cwd))

    def test_paths_fail_closed(self):
        for path in ("../out", "out/../other", "/etc", str(self.cwd.parent / "work-other"),
                     ".", "./**", "out/*", "out/**/x", "out),Bash(*)", "out\n", "~/out", "", None, 1):
            with self.subTest(path=path), self.assertRaises(UsageError):
                self.validate({"allowed_write_dirs": [path]})
        (self.cwd / "file").write_text("x")
        with self.assertRaises(UsageError):
            self.validate({"allowed_write_dirs": ["file"]})
        with self.assertRaises(UsageError):
            engines.validate_options("claude", {"allowed_write_dirs": ["out"]})

    def test_symlink_escapes_and_replacement_before_resume(self):
        outside = self.cwd.parent / "outside"
        outside.mkdir()
        (self.cwd / "link").symlink_to(outside, target_is_directory=True)
        for path in ("link", "link/new", str(self.cwd / "link")):
            with self.subTest(path=path), self.assertRaises(UsageError):
                self.validate({"allowed_write_dirs": [path]})
        (self.cwd / "out").mkdir()
        options = self.validate({"allowed_write_dirs": ["out"]})
        (self.cwd / "out" / "escape").symlink_to(outside)
        with self.assertRaises(UsageError):
            self.build(options, "old-session")
        (self.cwd / "out" / "escape").unlink()
        (self.cwd / "out").rmdir()
        (self.cwd / "out").symlink_to(outside)
        with self.assertRaises(UsageError):
            self.build(options)

    def test_absolute_cwd_alias_is_stored_as_a_relative_rule(self):
        alias = self.cwd.parent / "alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.assertEqual(self.validate({"allowed_write_dirs": [str(alias / "out")]}),
                         {"allowed_write_dirs": ["./out/**"]})
        (self.cwd / "root-alias").symlink_to(self.cwd, target_is_directory=True)
        with self.assertRaises(UsageError):
            self.validate({"allowed_write_dirs": ["root-alias"]})

    def test_command_and_rule_injection_rejected(self):
        for command in ("*", "Bash(*)", "python3:*", "python3; curl x", "python3 && id", "$(id)",
                        "`id`", "python3\nsh", "python3,Write(*)", "python3 > /tmp/x", "python3 *",
                        "python3 | sh", "", " python3", "--help", None, 1,
                        "claude --permission-mode bypassPermissions", "claude --dangerously-skip-permissions"):
            with self.subTest(command=command), self.assertRaises(UsageError):
                self.validate({"allowed_bash_commands": [command]})
        for key in ("allowed_write_dirs", "allowed_bash_commands"):
            for value in ("out", True, {}, None):
                with self.subTest(key=key, value=value), self.assertRaises(UsageError):
                    self.validate({key: value})
            for engine in ("codex", "fixture"):
                with self.assertRaises(UsageError):
                    engines.validate_options(engine, {key: []}, cwd=str(self.cwd))

    def test_bypass_flags_and_settings_spellings_rejected(self):
        base = self.build({})
        for flags in (["--permission-mode=bypassPermissions"], ["--permission-mode", "bypassPermissions"],
                      ["--dangerously-skip-permissions=true"], ["--allow-dangerously-skip-permissions=false"],
                      ['--settings={"permissions":{"defaultMode":"bypassPermissions"}}'],
                      ["--settings", '{"permissions":{"defaultMode":"bypassPermissions"}}']):
            with self.subTest(flags=flags), self.assertRaises(RuntimeError):
                engines.assert_safe("claude", base + flags)

    def test_network_alias_preserves_existing_gate(self):
        self.assertEqual(engines.validate_options("codex", {"sandbox": "workspace-write", "network_access": True}),
                         {"sandbox": "workspace-write", "allow_network": True})
        for sandbox in (None, "read-only", "danger-full-access"):
            for flag in (True, False):
                opts = {"network_access": flag, "allow_full_access": True}
                if sandbox:
                    opts["sandbox"] = sandbox
                with self.subTest(opts=opts), self.assertRaises(UsageError):
                    engines.validate_options("codex", opts)
        for value in ("true", 1, None):
            with self.assertRaises(UsageError):
                engines.validate_options("codex", {"sandbox": "workspace-write", "network_access": value})
        with self.assertRaises(UsageError):
            engines.validate_options("codex", {"sandbox": "workspace-write", "network_access": True,
                                               "allow_network": False})
        with self.assertRaises(UsageError):
            engines.validate_options("codex", {"sandbox": "danger-full-access"})
        for resume in (None, "old-session"):
            argv = engines.build("codex", payload={"cwd": str(self.cwd), "options": {
                "sandbox": "workspace-write", "network_access": True}},
                attempt={"resume_session_id": resume}, run_dir=self.cwd, fixture_state_dir=str(self.cwd),
                env={"CLI_RELAY_CODEX_BIN": "/bin/echo"}).argv
            self.assertIn("sandbox_workspace_write.network_access=true", argv)
            self.assertNotIn("danger-full-access", argv)

    def test_notification_permissions_are_per_run_and_never_truncated(self):
        rules = [f"Edit(./output-{i}/**)" for i in range(40)]
        refs = [{"event_id": "e1", "task_id": "t1", "attempt_no": 1, "outcome": "succeeded",
                 "allowed_tools": rules, "result_excerpt": "x" * 500},
                {"event_id": "e2", "task_id": "t2", "attempt_no": 2, "outcome": "succeeded",
                 "allowed_tools": [], "result_excerpt": ""}]
        payload = notify.build_payload({"source_id": "test"},
                                       {"name": "bot", "chat": "chat", "consumer": "c", "adapter": {"job": "job"}},
                                       "ledger", refs, False)
        self.assertIn("t1#1 permissions: allowed_tools=" + ",".join(rules), payload["summary"])
        self.assertIn("t2#2 permissions: default minimal", payload["summary"])


@needs_clean_cli_config
class PermissionFlags(RelayTestCase):
    def test_claude_flags_json_and_resume_report_exact_rules(self):
        env = base_env(isolated_env(self.tmp, {"CLI_RELAY_CLAUDE_BIN": str(recording_cli(self.tmp, "claude"))}))
        args = ["submit", "--key", "scoped", "--engine", "claude", "--cwd", str(self.work), "--prompt", "work",
                "--option", "model=test-model"]
        sub = self.cli(*args, "--allow-write-dir", "./out/**", "--allow-write-dir", "reports",
                       "--allow-bash", "python3", "--allow-bash", "pip install --user", env=env)
        expected = ["Bash(pip install --user:*)", "Bash(python3:*)", "Edit(./out/**)", "Edit(./reports/**)",
                    "Write(./out/**)", "Write(./reports/**)"]
        self.assertEqual(sub["allowed_tools"], expected)
        self.assertEqual(sub["permissions"], "allowed_tools=" + ",".join(expected))
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        body = done["events"][0]["body"]
        self.assertEqual(body["permissions"], sub["permissions"])
        row = {"body_json": json.dumps(body), "event_id": "e", "task_id": sub["task_id"],
               "attempt_no": 1, "kind": "succeeded"}
        self.assertEqual(notify._event_ref(row)["allowed_tools"], expected)
        duplicate = self.cli(*args, "--option", 'allowed_write_dirs=["reports","out"]',
                             "--option", 'allowed_bash_commands=["pip install --user","python3"]', env=env)
        self.assertEqual(duplicate["status"], "duplicate")
        self.cli("resume", sub["task_id"], "--prompt", "again", env=env)
        resumed = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(resumed["task"]["state"], "succeeded")
        for call in map(json.loads, (self.tmp / "calls.jsonl").read_text().splitlines()):
            self.assertEqual(call[call.index("--allowedTools") + 1], ",".join(expected))

    def test_defaults_and_invalid_flags(self):
        sub = self.submit("default")
        self.assertEqual(sub["permissions"], "default minimal")
        args = ["submit", "--key", "invalid", "--cwd", str(self.work), "--prompt", "work"]
        for flags in (["--engine", "claude", "--network-access"],
                      ["--engine", "codex", "--network-access"],
                      ["--engine", "codex", "--allow-bash", "python3"],
                      ["--engine", "claude", "--allow-write-dir", "../out"],
                      ["--engine", "claude", "--allow-bash", "python3", "--option", 'allowed_bash_commands=["sh"]']):
            with self.subTest(flags=flags):
                self.cli(*args, *flags, expect=2)

    def test_network_flag_and_json_alias_are_the_same_payload(self):
        # No real CLI is launched: receipt validation and persistence precede preflight.
        args = ["submit", "--key", "network", "--engine", "codex", "--cwd", str(self.work), "--prompt", "work",
                "--option", "sandbox=workspace-write"]
        sub = self.cli(*args, "--network-access")
        self.assertEqual(sub["permissions"], "sandbox=workspace-write; network_access=true")
        for option in ("network_access=true", "allow_network=true"):
            other = self.cli(*args, "--option", option)
            self.assertEqual(other["status"], "duplicate")
            self.assertEqual(other["payload_sha256"], sub["payload_sha256"])

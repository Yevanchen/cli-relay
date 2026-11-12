"""Security regressions for the real hook program; no provider calls or host credentials."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cli_relay import claude_guard as guard, engines
from cli_relay.errors import UsageError
from helpers import isolated_env, needs_clean_cli_config


@needs_clean_cli_config
class SubagentCeiling(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "work"
        (self.cwd / "out").mkdir(parents=True)
        binary = self.root / "fake-claude"
        binary.write_text(f"#!{sys.executable}\nimport sys,json\n"
                          "if sys.argv[1]=='--version': print('2.1.301 (Claude Code)')\n"
                          "else: print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty'}))\n")
        binary.chmod(0o755)
        self.env = isolated_env(self.root, {"CLI_RELAY_CLAUDE_BIN": str(binary)})
        self.options = {"allowed_tools": ["Agent", "Task", "TodoWrite"], "allowed_bash_commands": ["ls"],
                        "allowed_write_dirs": ["./out/**"]}

    def event(self, name, args, **kwargs):
        return {"hook_event_name": "PreToolUse", "permission_mode": "dontAsk", "cwd": str(self.cwd),
                "tool_name": name, "tool_input": args, **kwargs}

    def denial(self, name, args, **kwargs):
        return guard.evaluate(self.event(name, args, **kwargs), guard.snapshot(self.options, str(self.cwd)))

    def build(self, resume=None, options=None, env=None):
        return engines.build("claude", payload={"cwd": str(self.cwd), "options": options or self.options},
                             attempt={"native_session_id": "11111111-1111-4111-8111-111111111111",
                                      "resume_session_id": resume}, run_dir=self.root,
                             fixture_state_dir=str(self.root), env=env or self.env)

    def test_simple_ls_allowed_and_shell_escapes_denied(self):
        for cmd in ("ls", "ls -la out", "ls 'out/file with spaces'"):
            self.assertIsNone(self.denial("Bash", {"command": cmd}), cmd)
        for cmd in ("pwd", "touch out/x", "ls; touch x", "ls && id", "ls | sh", "ls > outside", "ls $(id)",
                    "ls `id`", "ls\nid", "ls ${PATH}", "ls <(id)", "l\\s", "ls\x00", "ls 'unclosed"):
            self.assertIsNotNone(self.denial("Bash", {"command": cmd}), cmd)
        self.assertIsNotNone(self.denial("Bash", {"command": "ls", "dangerouslyDisableSandbox": True}))

    def test_no_bash_grant_and_interpreter_optins(self):
        options = {**self.options, "allowed_bash_commands": []}
        self.assertIsNotNone(guard.evaluate(self.event("Bash", {"command": "ls"}), guard.snapshot(options, str(self.cwd))))
        for command in ("python3", "sh", "/bin/ls", "ls -la", "git status", "cp"):
            with self.subTest(command=command), self.assertRaises(UsageError):
                engines.validate_options("claude", {**self.options, "allowed_bash_commands": [command]}, cwd=str(self.cwd))

    def test_write_edit_and_notebooks_share_the_ceiling(self):
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")):
            self.assertIsNone(self.denial(tool, {key: "out/allowed.txt"}))
            for path in ("outside.txt", "out/../outside.txt", str(self.root / "outside.txt"), "out", "", None):
                self.assertIsNotNone(self.denial(tool, {key: path}), (tool, path))

    def test_runtime_symlink_swap_and_hard_links_are_denied(self):
        policy = guard.snapshot(self.options, str(self.cwd))
        outside = self.root / "outside"
        outside.mkdir()
        (self.cwd / "out" / "link").symlink_to(outside)
        self.assertIsNotNone(guard.evaluate(self.event("Write", {"file_path": "out/link/x"}), policy))
        secret = outside / "original"
        secret.write_text("unchanged")
        (self.cwd / "out" / "hardlink").hardlink_to(secret)
        self.assertIsNotNone(self.denial("Edit", {"file_path": "out/hardlink"}))
        (self.cwd / "out" / "link").unlink()
        (self.cwd / "out" / "hardlink").unlink()
        (self.cwd / "out").rmdir()
        (self.cwd / "out").symlink_to(outside)
        self.assertIsNotNone(guard.evaluate(self.event("Write", {"file_path": "out/x"}), policy))
        self.assertEqual(secret.read_text(), "unchanged")

    def test_config_and_shell_startup_writes_are_denied_even_inside_a_grant(self):
        for path in ("out/.claude/settings.json", "out/.claude.json", "out/.git/config", "out/.mcp.json", "out/.bashrc", "out/.zshenv",
                     "out/.CLAUDE/settings.json", "out/.CLAUDE.JSON", "out/.GIT/config", "out/.BASHRC"):
            self.assertIsNotNone(self.denial("Write", {"file_path": path}), path)

    def test_custom_agents_modes_nested_resumed_and_background_agents_are_denied(self):
        args = {"subagent_type": guard.WORKER, "prompt": "write out/a", "description": "A", "run_in_background": False}
        for tool in ("Task", "Agent"):
            self.assertIsNone(self.denial(tool, args))
            for kind in ("general-purpose", "Explore", "malicious-acceptEdits", "malicious-bypassPermissions", "plugin:worker"):
                self.assertIsNotNone(self.denial(tool, {**args, "subagent_type": kind}))
            for extra in ({"mode": "acceptEdits"}, {"permissionMode": "auto"}, {"resume": "old-id"},
                          {"fork": True}, {"isolation": "worktree"}, {"team_name": "team"}, {"run_in_background": True}):
                self.assertIsNotNone(self.denial(tool, {**args, **extra}))
            self.assertIsNotNone(self.denial(tool, args, agent_id="nested"))

    def test_unexpected_mode_cwd_or_tool_fails_closed(self):
        for mode in (None, "acceptEdits", "auto", "bypassPermissions", "plan", "manual"):
            self.assertIsNotNone(self.denial("Write", {"file_path": "out/x"}, permission_mode=mode))
        self.assertIsNotNone(self.denial("Write", {"file_path": "out/x"}, cwd=str(self.root)))
        for tool in ("ExitPlanMode", "EnterPlanMode", "TeamCreate", "mcp__server__run", "PowerShell", "Skill", "WebFetch"):
            self.assertIsNotNone(self.denial(tool, {}))
        self.assertIsNone(self.denial("TodoWrite", {}))
        self.assertIsNone(self.denial("Read", {"file_path": "file"}))

    def test_actual_inline_hook_returns_blocking_exit_two_on_policy_denials_and_bad_json(self):
        hook = guard.settings(self.options, str(self.cwd))["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hook["onFailure"], "block")
        for event, code in ((self.event("Write", {"file_path": "out/x"}), 0),
                            (self.event("Write", {"file_path": "outside"}), 2),
                            (self.event("Bash", {"command": "touch x"}), 2), ({}, 2)):
            result = subprocess.run(shlex.split(hook["command"]), input=json.dumps(event), text=True, capture_output=True)
            self.assertEqual(result.returncode, code, result.stderr)
        result = subprocess.run(shlex.split(hook["command"]), input="broken", text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_first_and_resumed_builds_install_the_same_guard_and_leave_settings_unchanged(self):
        settings = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        content = json.dumps({"permissions": {"deny": ["Bash(rm *)"], "ask": ["WebSearch"], "allow": ["Write"]}})
        settings.write_text(content)
        for resume in (None, "22222222-2222-4222-8222-222222222222"):
            spec = self.build(resume, options={**self.options, "effort": "low"})
            engines.assert_safe("claude", spec.argv, options={**self.options, "effort": "low"}, cwd=str(self.cwd))
            self.assertEqual(spec.argv.count("--settings"), 1)
            self.assertEqual(spec.evidence["claude_subagent_guard"]["worker"], guard.WORKER)
            agent = json.loads(spec.argv[spec.argv.index("--agents") + 1])[guard.WORKER]
            self.assertEqual(agent["permissionMode"], "dontAsk")
            self.assertFalse(set(agent["tools"]) & {"Task", "Agent", "Skill"})
        self.assertEqual(settings.read_text(), content)

    def test_argv_recheck_catches_removed_modified_and_extra_policy_values(self):
        argv = self.build().argv
        for flag in ("--settings", "--agents", "--tools", "--mcp-config", "--permission-prompts"):
            i = argv.index(flag)
            for replacement in ([], [flag, "{}"], [flag, argv[i + 1], "extra"]):
                mutated = argv[:i] + replacement + argv[i + 2:]
                with self.subTest(flag=flag, replacement=replacement), self.assertRaises(RuntimeError):
                    engines.assert_safe("claude", mutated, options=self.options, cwd=str(self.cwd))
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-chrome"):
            with self.assertRaises(RuntimeError):
                engines.assert_safe("claude", [arg for arg in argv if arg != flag], options=self.options, cwd=str(self.cwd))
        for extra in (["--safe-mode"], ["--plugin-dir", "/tmp/x"], ["--agent", "other"], ["--agents={}"]):
            with self.assertRaises(RuntimeError):
                engines.assert_safe("claude", argv + extra, options=self.options, cwd=str(self.cwd))

    def test_customizations_that_disable_or_bypass_hooks_block_before_launch(self):
        path = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        for setting in ({"disableAllHooks": True}, {"allowManagedHooksOnly": True}, {"hooks": {"SessionStart": []}},
                        {"enabledPlugins": {"plugin": True}}, {"agent": "custom"}, {"mods": ["mod"]},
                        {"env": {"CLAUDE_CODE_SAFE_MODE": "1"}}, {"env": {"PATH": str(self.cwd / "out")}}):
            path.write_text(json.dumps(setting))
            self.assertFalse(engines.preflight("claude", self.env, str(self.cwd), options=self.options).ok, setting)
            with self.assertRaises(RuntimeError):
                self.build()

    def test_config_overlap_is_refused(self):
        env = {**self.env, "CLAUDE_CONFIG_DIR": str(self.cwd / "out" / "config")}
        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            self.build(env=env)
        with self.assertRaisesRegex(RuntimeError, "absolute"):
            self.build(env={**self.env, "CLAUDE_CONFIG_DIR": "out/config"})
        binary = self.cwd / "out" / "claude"
        binary.write_bytes(Path(self.env["CLI_RELAY_CLAUDE_BIN"]).read_bytes())
        binary.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            self.build(env={**self.env, "CLI_RELAY_CLAUDE_BIN": str(binary)})
        managed = self.cwd / "out" / "managed" / "managed-settings.json"
        with patch.object(engines, "CLAUDE_MANAGED_SETTINGS", (managed,)):
            with self.assertRaisesRegex(RuntimeError, "overlaps"):
                self.build()

    def test_config_overlap_through_case_or_unicode_alias_is_refused(self):
        # Linux can have distinct OUT/out directories; over-denying that configuration is safe.
        # macOS commonly aliases them and must never let the grant hide the live config dir.
        config = self.cwd / "out" / "config"
        config.mkdir()
        env = {**self.env, "CLAUDE_CONFIG_DIR": str(config)}
        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            self.build(options={**self.options, "allowed_write_dirs": ["OUT"]}, env=env)
        self.assertTrue(guard.protected_scope_overlap(Path("/tmp/caf\u00e9/out"), Path("/tmp/cafe\u0301/OUT/config")))
        self.assertFalse(guard.protected_scope_overlap(Path("/tmp/out"), Path("/tmp/out-other/config")))

    def test_managed_dropins_cannot_disable_the_policy_hook(self):
        managed = self.root / "managed-settings.json"
        directory = self.root / "managed-settings.d"
        directory.mkdir()
        (directory / "disable.json").write_text('{"disableAllHooks":true}')
        with patch.object(engines, "CLAUDE_MANAGED_SETTINGS", (managed,)):
            self.assertFalse(engines.preflight("claude", self.env, str(self.cwd), options=self.options).ok)
            with self.assertRaisesRegex(RuntimeError, "disableAllHooks"):
                self.build()

    def test_unverified_version_blocks_before_a_model_can_run(self):
        binary = Path(self.env["CLI_RELAY_CLAUDE_BIN"])
        binary.write_text(binary.read_text().replace("2.1.301", "2.1.302"))
        pf = engines.preflight("claude", self.env, str(self.cwd), options=self.options)
        self.assertEqual(pf.reason, "subagent_guard_unverified")
        with self.assertRaisesRegex(RuntimeError, "live-verified"):
            self.build()

    def test_clean_environment_prevents_safe_mode_teams_and_path_shadowing(self):
        spec = self.build(env={**self.env, "CLAUDE_CODE_SAFE_MODE": "1", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                               "BASH_ENV": "malicious", "PATH": str(self.cwd / "out")})
        self.assertNotIn("CLAUDE_CODE_SAFE_MODE", spec.env)
        self.assertNotIn("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", spec.env)
        self.assertNotIn("BASH_ENV", spec.env)
        self.assertEqual(spec.env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(spec.env["CLAUDE_CONFIG_DIR"], self.env["CLAUDE_CONFIG_DIR"])

    def test_default_argv_cannot_drop_native_subagent_denials(self):
        argv = self.build(options={"allowed_tools": ["TodoWrite"]}).argv
        i = argv.index("--disallowedTools")
        with self.assertRaises(RuntimeError):
            engines.assert_safe("claude", argv[:i] + argv[i+2:], options={"allowed_tools": ["TodoWrite"]})

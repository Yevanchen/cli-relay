"""Task-local model/effort and a restrictive Claude mode, with no real CLI or account access."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import RelayTestCase, base_env, isolated_env, needs_clean_cli_config, wait_until
from test_audit_03_provider_bypass import fake_claude

from cli_relay import engines, procs
from cli_relay.errors import UsageError


@needs_clean_cli_config
class TaskLocalOptions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-model-"))
        self.env = isolated_env(self.tmp, {"CLI_RELAY_CLAUDE_BIN": str(fake_claude(self.tmp)),
                                           "CLI_RELAY_CODEX_BIN": "/bin/echo"})

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, engine, options, resume=None):
        return engines.build(engine, payload={"cwd": str(self.tmp), "options": options},
                             attempt={"native_session_id": "11111111-1111-4111-8111-111111111111",
                                      "resume_session_id": resume, "attempt_id": "a-test"},
                             run_dir=self.tmp, fixture_state_dir=str(self.tmp), env=self.env)

    def test_effort_is_a_closed_enum_not_an_arbitrary_config_channel(self) -> None:
        for engine in ("claude", "codex"):
            for effort in engines.EFFORT_LEVELS:
                self.assertEqual(engines.validate_options(engine, {"effort": effort}), {"effort": effort})
            for effort in (True, 1, None, [], "MAX", "auto", 'max"\nmodel_provider="other'):
                with self.subTest(engine=engine, effort=effort), self.assertRaises(UsageError):
                    engines.validate_options(engine, {"effort": effort})
            with self.assertRaises(UsageError):
                self.build(engine, {"effort": "unvalidated"})
        with self.assertRaises(UsageError):
            engines.validate_options("claude", {"settings": {"permissions": {"defaultMode": "bypassPermissions"}}})

    def test_new_and_resumed_claude_keep_auth_scope_and_override_effort_at_the_right_layer(self) -> None:
        self.env[engines.CLAUDE_EFFORT_ENV] = "low"
        settings = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        saved = json.dumps({"env": {engines.CLAUDE_EFFORT_ENV: "medium"},
                            "permissions": {"defaultMode": "bypassPermissions", "deny": ["Read(.env)"]}})
        settings.write_text(saved)
        for resume in (None, "22222222-2222-4222-8222-222222222222"):
            spec = self.build("claude", {"model": "claude-opus-5-5", "effort": "max"}, resume)
            args = spec.argv
            self.assertEqual(args[args.index("--model") + 1], "claude-opus-5-5")
            self.assertEqual(args[args.index("--effort") + 1], "max")
            self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(args[args.index("--permission-prompts") + 1], "none")
            self.assertEqual(json.loads(args[args.index("--settings") + 1]),
                             {"env": {engines.CLAUDE_EFFORT_ENV: "max"}})
            self.assertFalse(set(args) & {"--bare", "--setting-sources", "--restricted"})
            for key in ("HOME", "CLAUDE_CONFIG_DIR"):
                self.assertEqual(spec.env[key], self.env[key])
            self.assertEqual(settings.read_text(), saved)

    def test_new_and_resumed_codex_pin_model_effort_without_widening_sandbox(self) -> None:
        for resume in (None, "22222222-2222-4222-8222-222222222222"):
            args = self.build("codex", {"model": "gpt-6-astra", "effort": "max"}, resume).argv
            self.assertEqual(args[args.index("-m") + 1], "gpt-6-astra")
            self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
            configs = [args[i + 1] for i, arg in enumerate(args) if arg == "-c"]
            self.assertIn('model_reasoning_effort="max"', configs)
            if resume:
                self.assertIn('sandbox_mode="read-only"', configs)
                self.assertEqual(args[-2:], [resume, "-"])

    def test_default_effort_is_not_changed_when_task_omits_it(self) -> None:
        self.assertNotIn("--effort", self.build("claude", {}).argv)
        self.assertNotIn("--settings", self.build("claude", {}).argv)
        self.assertNotIn("-c", self.build("codex", {}).argv)

    def test_generated_argv_guard_does_not_allow_unsafe_options(self) -> None:
        for mode in ("bypassPermissions", "acceptEdits", "auto", "manual", "plan"):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                engines.assert_safe("claude", ["claude", "--permission-mode", mode])
        for argv in (["claude"], ["claude", "--permission-mode=dontAsk"],
                     ["claude", "--permission-mode", "dontAsk", "--settings", '{"apiKeyHelper":"x"}'],
                     ["claude", "--permission-mode", "dontAsk", "--setting-sources", "project,local"]):
            with self.subTest(argv=argv), self.assertRaises(RuntimeError):
                engines.assert_safe("claude", argv)
        for config in ('model_provider="other"', 'model_reasoning_effort="invalid"',
                       'sandbox_mode="danger-full-access"', 'approval_policy="never"'):
            with self.subTest(config=config), self.assertRaises(RuntimeError):
                engines.assert_safe("codex", ["codex", "exec", "-c", config])

    def test_only_known_overridden_mode_is_tolerated_and_rules_are_not_removed(self) -> None:
        settings = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        data = {"permissions": {"defaultMode": "bypassPermissions", "deny": ["Bash(rm *)"],
                                 "ask": ["Edit"], "allow": ["Read"]}}
        saved = json.dumps(data)
        settings.write_text(saved)
        self.assertTrue(engines.preflight("claude", self.env, options={"effort": "max"}).ok)
        self.assertEqual(settings.read_text(), saved)
        self.assertIn("user:permissions.defaultMode", engines.claude_settings_problems(self.env)[0])
        data["permissions"]["defaultMode"] = "plan"
        settings.write_text(json.dumps(data))
        self.assertFalse(engines.preflight("claude", self.env).ok, "dontAsk must not replace stricter plan mode")
        for mode in (["bypassPermissions"], "totally-invalid-mode"):
            data["permissions"]["defaultMode"] = mode
            settings.write_text(json.dumps(data))
            self.assertFalse(engines.preflight("claude", self.env).ok)
        settings.write_text('{"permissions":{"defaultMode":"bypassPermissions"},"apiKeyHelper":"x"}')
        self.assertFalse(engines.preflight("claude", self.env).ok)

    def test_managed_modes_and_effort_caps_are_not_overridden(self) -> None:
        managed = self.tmp / "managed.json"
        with patch.object(engines, "CLAUDE_MANAGED_SETTINGS", (managed,)):
            for data in ({"permissions": {"defaultMode": "bypassPermissions"}},
                         {"permissions": {"defaultMode": "plan"}},
                         {"permissions": {"defaultMode": "manual"}},
                         {"permissions": {"defaultMode": []}},
                         {"env": {engines.CLAUDE_EFFORT_ENV: "high"}},
                         {"maxEffortLevel": "high"}, {"maxEffortLevel": []},
                         {"env": {"ANTHROPIC_BASE_URL": "https://invalid.example"}},
                         {"forceLoginMethod": "console"}):
                with self.subTest(settings=data):
                    managed.write_text(json.dumps(data))
                    self.assertFalse(engines.preflight("claude", self.env, options={"effort": "max"}).ok)
            managed.write_text("invalid json")
            self.assertFalse(engines.preflight("claude", self.env).ok)
            managed.write_text(json.dumps({"permissions": {"defaultMode": "dontAsk", "deny": ["Edit"]},
                                           "maxEffortLevel": "max"}))
            self.assertTrue(engines.preflight("claude", self.env, options={"effort": "max"}).ok)

    def test_runtime_missing_or_different_mode_cannot_be_success(self) -> None:
        for mode in (None, "default", "manual", "acceptEdits", "plan", "bypassPermissions"):
            parsed = {"init": {"permissionMode": mode, "apiKeySource": "none"}, "native": {"ok": True}}
            outcome, _ = engines.decide("claude", parsed, env=self.env, session_id="S", exit_code=0,
                                        exit_signal=None, exit_known=True, cancel_requested=False)
            self.assertEqual(outcome, "blocked", mode)
        outcome, evidence = engines.decide("claude", {"init": None, "native": {"ok": True}}, env=self.env,
                                           session_id="S", exit_code=0, exit_signal=None, exit_known=True,
                                           cancel_requested=False)
        self.assertEqual((outcome, evidence["permission_check"]["missing"]), ("blocked", "init"))
        parsed = {"init": {"permissionMode": "dontAsk", "apiKeySource": "none"}, "native": {"ok": True}}
        self.assertEqual(engines.decide("claude", parsed, env=self.env, session_id="S", exit_code=0,
                                        exit_signal=None, exit_known=True, cancel_requested=False)[0], "succeeded")

    def test_lower_user_or_project_effort_cap_is_not_lifted_by_managed_max(self) -> None:
        managed = self.tmp / "managed.json"
        managed.write_text('{"maxEffortLevel":"max"}')
        project = self.tmp / "work" / ".claude"
        project.mkdir(parents=True)
        for layer, path in (("user", Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"),
                            ("project", project / "settings.json"),
                            ("project", project / "settings.local.json")):
            with self.subTest(layer=layer, path=path), patch.object(engines, "CLAUDE_MANAGED_SETTINGS", (managed,)):
                for cap in ("high", ["high"]):
                    path.write_text(json.dumps({"maxEffortLevel": cap}))
                    pf = engines.preflight("claude", self.env, cwd=str(project.parent), options={"effort": "max"})
                    self.assertFalse(pf.ok)
                    self.assertIn(f"{layer}:maxEffortLevel", pf.detail["settings"])
                path.unlink()

    def test_alias_to_managed_settings_keeps_managed_policy_provenance(self) -> None:
        managed = self.tmp / "managed.json"
        user = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        project = self.tmp / "work" / ".claude"
        project.mkdir(parents=True)
        user.symlink_to(managed)
        (project / "settings.json").symlink_to(managed)
        with patch.object(engines, "CLAUDE_MANAGED_SETTINGS", (managed,)):
            for data, key in (({"permissions": {"defaultMode": "bypassPermissions"}}, "permissions.defaultMode"),
                              ({"env": {engines.CLAUDE_EFFORT_ENV: "high"}}, "env." + engines.CLAUDE_EFFORT_ENV)):
                with self.subTest(settings=data):
                    managed.write_text(json.dumps(data))
                    problems, layers = engines.claude_settings_problems(
                        self.env, str(project.parent), permission_mode="dontAsk", effort="max")
                    self.assertEqual(layers, ["user", "project", "managed"])
                    self.assertIn("managed:" + key, problems)
                    self.assertFalse(engines.preflight("claude", self.env, cwd=str(project.parent),
                                                       options={"effort": "max"}).ok)


def recording_cli(tmp: Path, engine: str, reported_mode: str = "dontAsk") -> Path:
    """A fake subprocess records argv and emits vendor-shaped evidence; it has no network code."""
    path = tmp / engine
    script = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] in ("auth", "login"):
    print('{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}' if ENGINE == "claude"
          else "Logged in using ChatGPT")
    sys.exit(0)
with open(MARKER, "a") as f:
    f.write(json.dumps(args) + "\n")
def value(flag):
    return args[args.index(flag) + 1]
if ENGINE == "claude":
    sid = value("--resume") if "--resume" in args else value("--session-id")
    print(json.dumps({"type":"system", "subtype":"init", "session_id":sid, "apiKeySource":"none",
                      "permissionMode":MODE, "model":value("--model")}), flush=True)
    print(json.dumps({"type":"result", "subtype":"success", "is_error":False, "session_id":sid,
                      "result":"done", "permission_denials":[]}))
else:
    sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    directory = Path(os.environ["CODEX_HOME"]) / "sessions/2026/10/19"
    directory.mkdir(parents=True, exist_ok=True)
    rollout = directory / ("rollout-2026-10-19T00-00-00-" + sid + ".jsonl")
    with rollout.open("a") as f:
        if "resume" not in args:
            f.write(json.dumps({"type":"session_meta", "payload":{"model_provider":"openai"}}) + "\n")
        f.write(json.dumps({"type":"turn_context", "payload":{"sandbox_policy":{"type":"read-only"},
              "active_permission_profile":None}}) + "\n")
    for event in ({"type":"thread.started", "thread_id":sid},
                  {"type":"item.completed", "item":{"type":"agent_message", "text":"done"}},
                  {"type":"turn.completed"}):
        print(json.dumps(event))
'''
    path.write_text(f"#!{sys.executable}\nENGINE={engine!r}\nMODE={reported_mode!r}\n"
                    f"MARKER={str(tmp / 'calls.jsonl')!r}\n" + script)
    path.chmod(0o755)
    return path


@needs_clean_cli_config
class TaskLocalConfigClosedLoop(RelayTestCase):
    def fake_env(self, engine, mode="dontAsk"):
        fake = recording_cli(self.tmp, engine, mode)
        return base_env(isolated_env(self.tmp, {f"CLI_RELAY_{engine.upper()}_BIN": str(fake)}))

    def submit_model(self, engine, env):
        return self.cli("submit", "--key", "k-model-" + engine, "--engine", engine, "--cwd", str(self.work),
                        "--prompt", "first", "--option", f'model="{"claude-opus-5-5" if engine == "claude" else "gpt-6-astra"}"',
                        "--option", 'effort="max"', env=env)

    def test_claude_new_and_exact_resume_preserve_global_settings(self) -> None:
        env = self.fake_env("claude")
        settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        saved = json.dumps({"permissions": {"defaultMode": "bypassPermissions", "deny": ["Edit"]},
                            "env": {engines.CLAUDE_EFFORT_ENV: "low"}})
        settings.write_text(saved)
        sub = self.submit_model("claude", env)
        self.assertEqual(self.cli("wait", sub["task_id"], "--timeout", "30")["task"]["state"], "succeeded")
        self.cli("resume", sub["task_id"], "--prompt", "second", env=env)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(done["attempts"][1]["native"]["session_id"], sub["native_session_id"])
        self.assertEqual(settings.read_text(), saved)
        for args in map(json.loads, (self.tmp / "calls.jsonl").read_text().splitlines()):
            self.assertEqual(args[args.index("--model") + 1], "claude-opus-5-5")
            self.assertEqual(args[args.index("--effort") + 1], "max")
            self.assertEqual(json.loads(args[args.index("--settings") + 1]),
                             {"env": {engines.CLAUDE_EFFORT_ENV: "max"}})

    def test_codex_new_and_exact_resume_pass_effort_and_keep_new_turn_check(self) -> None:
        env = self.fake_env("codex")
        config = Path(env["CODEX_HOME"]) / "config.toml"
        saved = 'model = "other-model"\nmodel_reasoning_effort = "medium"\n'
        config.write_text(saved)
        sub = self.submit_model("codex", env)
        self.assertEqual(self.cli("wait", sub["task_id"], "--timeout", "30")["task"]["state"], "succeeded")
        self.cli("resume", sub["task_id"], "--prompt", "second", env=env)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertIn("offset", done["attempts"][1]["evidence"]["codex_permission_checkpoint"])
        self.assertEqual(config.read_text(), saved)
        for args in map(json.loads, (self.tmp / "calls.jsonl").read_text().splitlines()):
            self.assertEqual(args[args.index("-m") + 1], "gpt-6-astra")
            self.assertIn('model_reasoning_effort="max"', args)

    def test_different_runtime_mode_stays_blocked_after_keeper_crash(self) -> None:
        env = self.fake_env("claude", "default")
        env["CLI_RELAY_TEST_CRASH"] = "after_exit_before_commit"
        sub = self.submit_model("claude", env)
        wait_until(lambda: (lambda r: bool(r["keeper_pid"]) and procs.same_process_alive(
            r["keeper_pid"], r["keeper_token"]) is False)(self.attempt_row(sub["task_id"])))
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["billing_guard"]["reason"],
                         "permission_mode_not_as_requested")

    def test_known_user_cap_blocks_before_model_subprocess_starts(self) -> None:
        env = self.fake_env("claude")
        (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text('{"maxEffortLevel":"high"}')
        sub = self.submit_model("claude", env)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertIn("user:maxEffortLevel", done["attempts"][0]["evidence"]["preflight"]["settings"])
        self.assertFalse((self.tmp / "calls.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

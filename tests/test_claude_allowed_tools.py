"""Extra Claude tools need a validated per-task opt-in that stays visible and never weakens the
permission mode, the settings layers or the deny rules in them."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config
from test_audit_03_provider_bypass import fake_claude

from notify_helpers import write_config
from cli_relay import engines, notify
from cli_relay.errors import UsageError
from cli_relay.ledger import payload_hash

TOOLS = ["WebFetch", "WebSearch"]
OPT_IN = {"allowed_tools": ["WebSearch", "WebFetch"]}
SID = "11111111-1111-4111-8111-111111111111"
RESUME = "22222222-2222-4222-8222-222222222222"
BASE = ["claude", "-p", "--permission-mode", "dontAsk", "--permission-prompts", "none"]


class AllowedToolsOptionsAndArgv(unittest.TestCase):
    def build(self, options, resume=None):
        return engines.build("claude", payload={"cwd": "/", "options": options},
                             attempt={"native_session_id": SID, "resume_session_id": resume}, run_dir=Path("/"),
                             fixture_state_dir="/", env={"CLI_RELAY_CLAUDE_BIN": "/bin/echo"}).argv

    def test_only_exact_allowlisted_names_in_a_list_are_accepted(self) -> None:
        for tools, stored in ((["WebSearch"], ["WebSearch"]), (["WebFetch"], ["WebFetch"]),
                              (["WebSearch", "WebFetch"], TOOLS), (TOOLS, TOOLS), ([], [])):
            with self.subTest(tools=tools):
                self.assertEqual(engines.validate_options("claude", {"allowed_tools": tools}),
                                 {"allowed_tools": stored})
        for value in ("WebSearch", "WebFetch,WebSearch", '["WebSearch"]', True, 1, None, {"WebSearch": True}):
            with self.subTest(value=value), self.assertRaisesRegex(UsageError, "wrong type"):
                engines.validate_options("claude", {"allowed_tools": value})
        for tools in (["Bash"], ["Read"], ["Edit"], ["Bash(curl *)"], ["WebFetch(domain:example.com)"],
                      ["WebFetch(domain:*)"], ["WebSearch(*)"], ["*"], ["Web*"], ["mcp__web__fetch"],
                      ["websearch"], ["WEBFETCH"], [" WebSearch"], ["WebSearch "], [""], ["WebSearch,Bash"],
                      ["WebSearch Bash"], ["WebFetch", "Bash"], [None], [1], [True], [["WebSearch"]],
                      [{"tool": "WebSearch"}]):
            with self.subTest(tools=tools), self.assertRaisesRegex(UsageError, "exact tool names"):
                engines.validate_options("claude", {"allowed_tools": tools})
        with self.assertRaisesRegex(UsageError, "more than once"):
            engines.validate_options("claude", {"allowed_tools": ["WebSearch", "WebFetch", "WebSearch"]})
        for engine in ("codex", "fixture"):
            with self.subTest(engine=engine), self.assertRaisesRegex(UsageError, "not supported"):
                engines.validate_options(engine, {"allowed_tools": ["WebSearch"]})

    def test_default_adds_no_tool_and_keeps_dont_ask(self) -> None:
        for resume in (None, RESUME):
            for options in ({}, {"allowed_tools": []}, {"model": "claude-opus-5-5"}):
                with self.subTest(resume=resume, options=options):
                    argv = self.build(options, resume)
                    self.assertFalse(set(argv) & set(engines.CLAUDE_ALLOWED_TOOLS_FLAGS))
                    self.assertFalse([arg for arg in argv if any(tool in arg for tool in TOOLS)])
                    self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
                    self.assertNotIn("--settings", argv)

    def test_opt_in_appends_one_allowed_tools_value_on_new_and_resumed_runs(self) -> None:
        for resume in (None, RESUME):
            with self.subTest(resume=resume):
                argv = self.build(OPT_IN, resume)
                self.assertEqual(argv.count("--allowedTools"), 1)
                self.assertEqual(argv[argv.index("--allowedTools") + 1], "WebFetch,WebSearch")
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
                self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
                self.assertEqual(argv[argv.index("--resume" if resume else "--session-id") + 1], resume or SID)
                self.assertNotIn("--settings", argv, "the opt-in is never a settings layer")
                self.assertFalse(set(argv) & {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                                              "--setting-sources", "--bare", "--add-dir", "--permission-prompt-tool"})
                self.assertEqual(self.build({"allowed_tools": ["WebSearch"]}, resume)[-2:],
                                 ["--allowedTools", "WebSearch"])
        argv = self.build({**OPT_IN, "effort": "max"})
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "WebFetch,WebSearch")
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"env": {engines.CLAUDE_EFFORT_ENV: "max"}})

    def test_argv_recheck_accepts_only_the_tasks_validated_tools(self) -> None:
        engines.assert_safe("claude", BASE + ["--allowedTools", "WebFetch,WebSearch"], options=OPT_IN)
        engines.assert_safe("claude", BASE + ["--allowed-tools", "WebFetch,WebSearch", "--model", "m"], options=OPT_IN)
        engines.assert_safe("claude", BASE + ["--allowedTools", "WebSearch"], options={"allowed_tools": ["WebSearch"]})
        for flags in (["--allowedTools", "Bash"], ["--allowedTools", "WebFetch,WebSearch,Bash"],
                      ["--allowedTools", "WebFetch,WebSearch", "Bash"], ["--allowedTools", "WebFetch,WebSearch", "-"],
                      ["--allowedTools", "WebFetch", "WebSearch"], ["--allowedTools", "WebSearch WebFetch"],
                      ["--allowedTools", "WebFetch(domain:*),WebSearch"], ["--allowedTools"],
                      ["--allowedTools=WebFetch,WebSearch"], ["--allowed-tools=WebFetch,WebSearch"],
                      ["--allowed-tools", "Bash(curl *)"]):
            with self.subTest(flags=flags), self.assertRaisesRegex(RuntimeError, "allowed tools"):
                engines.assert_safe("claude", BASE + flags, options=OPT_IN)
        for options in (None, {}, {"allowed_tools": []}, {"allowed_tools": "WebFetch,WebSearch"},
                        {"allowed_tools": ["WebSearch"]}, {"allowed_tools": ["Bash", "WebFetch"]}):
            with self.subTest(options=options), self.assertRaisesRegex(RuntimeError, "allowed tools"):
                engines.assert_safe("claude", BASE + ["--allowedTools", "WebFetch,WebSearch"], options=options)

    def test_opt_in_does_not_allow_another_mode_settings_or_forbidden_flags(self) -> None:
        tools = ["--allowedTools", "WebFetch,WebSearch"]
        for argv in (["claude", "-p", "--permission-mode", "bypassPermissions", *tools],
                     ["claude", "-p", "--permission-mode=dontAsk", *tools],
                     BASE + ["--dangerously-skip-permissions", *tools],
                     BASE + ["--setting-sources", "project", *tools],
                     BASE + [*tools, "--settings", '{"permissions":{"allow":["Bash"]}}'],
                     BASE + [*tools, "--settings", '{"permissions":{"deny":[]}}']):
            with self.subTest(argv=argv), self.assertRaises(RuntimeError):
                engines.assert_safe("claude", argv, options=OPT_IN)

    def test_permission_details_name_the_requested_tools_per_engine(self) -> None:
        self.assertEqual(engines.permission_details("claude", {"options": OPT_IN}), {"allowed_tools": TOOLS})
        for options in ({}, {"allowed_tools": []}, {"effort": "max"}):
            with self.subTest(options=options):
                self.assertEqual(engines.permission_details("claude", {"options": options}), {"allowed_tools": []})
        self.assertEqual(engines.permission_details("codex", {"options": {}}),
                         {"sandbox": "read-only", "full_access": False, "network_access": False})
        self.assertEqual(engines.permission_details("fixture", {"options": {}}), {})


class NetworkToolDefaultsForDoctor(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-tools-"))
        self.env = isolated_env(self.tmp)
        self.user = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        self.project = self.tmp / "work" / ".claude"
        self.project.mkdir(parents=True)
        self.managed = self.tmp / "managed.json"
        # Exactly these layers: neither the host's managed settings nor TMPDIR's ancestors are read.
        for p in (patch.object(engines, "CLAUDE_MANAGED_SETTINGS", (self.managed,)),
                  patch.object(engines, "_dirs_up", return_value=[self.project.parent])):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rules(self):
        return engines.claude_tool_rules(self.env, str(self.project.parent))

    def write(self, path, permissions, **extra):
        path.write_text(json.dumps({"permissions": permissions, **extra}))

    def test_no_settings_allow_no_network_tool(self) -> None:
        self.assertEqual(self.rules(), {"allowed_by_default": False, "default_allowed": [], "ask": [], "denied": [],
                                        "managed_rules_only": False, "complete": True})

    def test_bare_allow_rules_are_reported_by_tool_name_only(self) -> None:
        self.write(self.user, {"allow": ["WebSearch", "Bash(npm test)"], "deny": ["Read(./secrets/**)"]},
                   env={"PRIVATE_SETTING": "value-never-reported"}, model="private-model-name")
        self.write(self.project / "settings.local.json", {"allow": ["WebFetch"]})
        report = self.rules()
        self.assertEqual((report["allowed_by_default"], report["default_allowed"]), (True, TOOLS))
        self.assertEqual((report["ask"], report["denied"]), ([], []))
        text = json.dumps(report)
        for content in ("value-never-reported", "PRIVATE_SETTING", "private-model-name", "Bash", "npm", "secrets"):
            self.assertNotIn(content, text)

    def test_a_scoped_allow_does_not_allow_the_whole_tool(self) -> None:
        self.write(self.user, {"allow": ["WebFetch(domain:example.com)", "WebSearch"]})
        report = self.rules()
        self.assertEqual((report["allowed_by_default"], report["default_allowed"]), (False, ["WebSearch"]))

    def test_deny_and_ask_rules_of_any_layer_win_over_allow_rules(self) -> None:
        self.write(self.user, {"allow": TOOLS})
        self.write(self.project / "settings.json", {"ask": ["WebFetch(domain:example.com)"]})
        self.write(self.managed, {"deny": ["WebSearch"]})
        report = self.rules()
        self.assertEqual((report["allowed_by_default"], report["default_allowed"]), (False, []))
        self.assertEqual((report["ask"], report["denied"]), (["WebFetch"], ["WebSearch"]))
        for pattern in ("*", "Web*", "WebSearch(*)"):
            with self.subTest(pattern=pattern):
                self.write(self.managed, {"deny": [pattern]})
                self.assertIn("WebSearch", self.rules()["denied"])
        self.write(self.managed, {"deny": ["mcp__*", "Bash", "WebFetchX"]})
        self.assertEqual(self.rules()["denied"], [])

    def test_managed_rules_only_drops_every_other_source(self) -> None:
        self.write(self.user, {"allow": TOOLS, "deny": ["WebFetch"]})
        for value in (True, "yes", 1, 0):
            with self.subTest(value=value):
                self.write(self.managed, {"allow": ["WebSearch"]}, allowManagedPermissionRulesOnly=value)
                report = self.rules()
                self.assertIs(report["managed_rules_only"], True)
                self.assertEqual((report["default_allowed"], report["denied"]), (["WebSearch"], []))
        self.write(self.managed, {}, allowManagedPermissionRulesOnly=False)
        self.assertIs(self.rules()["managed_rules_only"], False)
        self.managed.unlink()
        self.write(self.user, {"allow": TOOLS}, allowManagedPermissionRulesOnly=True)
        self.assertIs(self.rules()["managed_rules_only"], False, "only managed settings can set it")

    def test_unreadable_or_unexpected_settings_make_the_report_incomplete(self) -> None:
        for text in ("not json", "[]", json.dumps({"permissions": []}),
                     json.dumps({"permissions": {"allow": "WebSearch"}}),
                     json.dumps({"permissions": {"deny": [1, "WebSearch"]}})):
            with self.subTest(text=text):
                self.user.write_text(text)
                self.assertIs(self.rules()["complete"], False)
        self.assertEqual(self.rules()["denied"], ["WebSearch"], "the readable rules are still reported")

    def test_a_deny_rule_neither_blocks_the_preflight_nor_changes_the_argv(self) -> None:
        self.env["CLI_RELAY_CLAUDE_BIN"] = str(fake_claude(self.tmp))
        self.write(self.managed, {"deny": ["WebSearch"]})
        saved = self.managed.read_text()
        self.assertTrue(engines.preflight("claude", self.env, cwd=str(self.project.parent), options=OPT_IN).ok)
        argv = engines.build("claude", payload={"cwd": str(self.project.parent), "options": OPT_IN},
                             attempt={"native_session_id": SID, "resume_session_id": None}, run_dir=self.tmp,
                             fixture_state_dir=str(self.tmp), env=self.env).argv
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "WebFetch,WebSearch")
        self.assertNotIn("--settings", argv)
        self.assertEqual(self.managed.read_text(), saved)
        self.assertEqual(self.rules()["denied"], ["WebSearch"])


def recording_claude(tmp: Path) -> Path:
    """No network: record argv. WebSearch is a structured denial unless --allowedTools names it and
    no deny rule in the user settings does, as dontAsk treats a tool that needs permission.
    This fixture needs the search to finish, so a denial also reports native failure."""
    script = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "auth":
    print('{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}')
    sys.exit(0)
with open(MARKER, "a") as f:
    f.write(json.dumps(args) + "\n")
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
sid = value("--resume") or value("--session-id")
settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
deny = json.loads(settings.read_text()).get("permissions", {}).get("deny", []) if settings.exists() else []
denied = "WebSearch" not in (value("--allowedTools") or "").split(",") or "WebSearch" in deny
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "apiKeySource": "none",
                  "permissionMode": value("--permission-mode"), "model": "fake"}), flush=True)
print(json.dumps({"type": "result", "subtype": "error_during_execution" if denied else "success",
                  "is_error": denied, "session_id": sid, "num_turns": 1,
                  "result": "could not search" if denied else "found it",
                  "permission_denials": [{"tool_name": "WebSearch", "tool_use_id": "t-1",
                                          "tool_input": {"query": "q"}}] if denied else []}))
sys.exit(1 if denied else 0)
'''
    path = tmp / "claude"
    path.write_text(f"#!{sys.executable}\nMARKER={str(tmp / 'calls.jsonl')!r}\n" + script)
    path.chmod(0o755)
    return path


@needs_clean_cli_config
class AllowedToolsRoundTrip(RelayTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.env = base_env(isolated_env(self.tmp, {"CLI_RELAY_CLAUDE_BIN": str(recording_claude(self.tmp))}))
        self.settings = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"

    def submit_claude(self, key="research", options=None, env=None, expect=0):
        args = ["submit", "--key", key, "--engine", "claude", "--cwd", str(self.work), "--prompt", "look it up"]
        for name, value in (OPT_IN if options is None else options).items():
            args += ["--option", f"{name}={json.dumps(value)}"]
        return self.cli(*args, env=env or self.env, expect=expect)

    def wait(self, task_id):
        return self.cli("wait", task_id, "--timeout", "30", env=self.env)

    def calls(self):
        path = self.tmp / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_without_the_opt_in_research_still_ends_needs_input(self) -> None:
        sub = self.submit_claude(options={})
        self.assertEqual(sub["allowed_tools"], [])
        done = self.wait(sub["task_id"])
        self.assertEqual(done["task"]["state"], "needs_input", done)
        self.assertEqual(done["attempts"][0]["native"]["permission_denials"], ["WebSearch"])
        self.assertEqual((done["task"]["allowed_tools"], done["task"]["options"]), ([], {}))
        self.assertEqual(done["events"][0]["body"]["allowed_tools"], [])
        (call,) = self.calls()
        self.assertFalse(set(call) & set(engines.CLAUDE_ALLOWED_TOOLS_FLAGS))
        self.assertEqual(call[call.index("--permission-mode") + 1], "dontAsk")

    def test_opt_in_is_stored_visible_and_lets_the_task_use_the_tools(self) -> None:
        saved = json.dumps({"permissions": {"allow": ["Read"], "deny": ["Bash(rm *)"]}, "model": "user-default"})
        self.settings.write_text(saved)
        sub = self.submit_claude()
        self.assertEqual(sub["allowed_tools"], TOOLS)
        done = self.wait(sub["task_id"])
        self.assertEqual(done["task"]["state"], "succeeded", done)
        status = self.cli("status", sub["task_id"], env=self.env)
        self.assertEqual(status["task"]["allowed_tools"], TOOLS)
        self.assertEqual(status["task"]["options"], {"allowed_tools": TOOLS})
        self.assertEqual(status["events"][0]["body"]["allowed_tools"], TOOLS)
        events = self.cli("events", "--consumer", "bot", env=self.env)["events"]
        self.assertEqual(events[0]["body"]["allowed_tools"], TOOLS)
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        self.assertEqual(stored["options"], {"allowed_tools": TOOLS})
        self.assertEqual(sub["payload_sha256"], payload_hash(stored))
        reordered = self.submit_claude(options={"allowed_tools": ["WebFetch", "WebSearch"]})
        self.assertEqual((reordered["status"], reordered["task_id"]), ("duplicate", sub["task_id"]))
        self.assertEqual(reordered["payload_sha256"], sub["payload_sha256"])
        self.assertEqual(reordered["allowed_tools"], TOOLS)
        (call,) = self.calls()
        self.assertEqual(call[call.index("--allowedTools") + 1], "WebFetch,WebSearch")
        self.assertEqual(call[call.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--settings", call)
        self.assertEqual(self.settings.read_text(), saved, "global settings are never changed")

        write_config(self.home, {"source_id": "test-host", "default_route": "bot",
            "routes": {"bot": {"consumer": "bot", "chat": "test chat",
                               "adapter": {"type": "command", "argv": ["/usr/bin/false"]}}}})
        with patch("cli_relay.notify.send", return_value={"outcome": "handed_off"}) as send:
            notify.flush(self.home, env=self.env)
        send.assert_called_once()
        notification = send.call_args.args[1]
        self.assertEqual(notification["cli_relay"]["events"][0]["allowed_tools"], TOOLS)
        self.assertIn("allowed_tools=WebFetch,WebSearch", notification["summary"])

    def test_unknown_tools_are_refused_before_a_task_exists(self) -> None:
        for tools in (["Bash"], ["WebFetch(domain:example.com)"], ["*"], ["websearch"], ["WebSearch", "WebSearch"],
                      "WebSearch"):
            with self.subTest(tools=tools):
                rejected = self.submit_claude(options={"allowed_tools": tools}, expect=2)
                self.assertEqual(rejected["error"], "usage")
                self.assertIn("allowed_tools", rejected["message"])
        self.assertEqual(db_rows(self.home, "SELECT task_id FROM tasks"), [])
        self.assertEqual(self.calls(), [])

    def test_a_different_tool_list_under_the_same_key_is_a_conflict(self) -> None:
        sub = self.submit_claude()
        for options in ({"allowed_tools": ["WebSearch"]}, {"allowed_tools": []}, {}):
            with self.subTest(options=options):
                conflict = self.submit_claude(options=options, expect=3)
                self.assertEqual(conflict["error"], "conflict")
                self.assertEqual(conflict["detail"]["existing_payload_sha256"], sub["payload_sha256"])
        self.assertEqual(self.wait(sub["task_id"])["task"]["state"], "succeeded")

    def test_opt_in_survives_exact_resume_a_keeper_crash_and_a_fresh_rerun(self) -> None:
        sub = self.submit_claude()
        self.assertEqual(self.wait(sub["task_id"])["task"]["state"], "succeeded")
        resumed = self.cli("resume", sub["task_id"], "--prompt", "and the next source",
                           env={**self.env, "CLI_RELAY_TEST_CRASH": "after_exit_before_commit"})
        self.assertEqual(resumed["allowed_tools"], TOOLS)
        done = self.wait(sub["task_id"])
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assertEqual(done["attempts"][-1]["resume_session_id"], sub["native_session_id"])
        self.assertEqual(done["events"][-1]["body"]["source"], "reconcile")
        self.assertEqual(done["events"][-1]["body"]["allowed_tools"], TOOLS)
        rerun = self.cli("resume", sub["task_id"], "--fresh", "--confirm-rerun", env=self.env)
        self.assertEqual(rerun["allowed_tools"], TOOLS)
        self.assertEqual(self.wait(sub["task_id"])["task"]["state"], "succeeded")
        first, again, fresh = self.calls()
        self.assertEqual(again[again.index("--resume") + 1], sub["native_session_id"])
        self.assertNotEqual(fresh[fresh.index("--session-id") + 1], sub["native_session_id"])
        for call in (first, again, fresh):
            self.assertEqual(call[call.index("--allowedTools") + 1], "WebFetch,WebSearch")
            self.assertEqual(call[call.index("--permission-mode") + 1], "dontAsk")

    def test_a_deny_rule_still_wins_and_the_task_ends_needs_input(self) -> None:
        saved = json.dumps({"permissions": {"deny": ["WebSearch"]}})
        self.settings.write_text(saved)
        sub = self.submit_claude()
        done = self.wait(sub["task_id"])
        self.assertEqual(done["task"]["state"], "needs_input", done)
        self.assertNotIn("preflight", done["attempts"][0]["evidence"], "a deny rule is not a preflight block")
        self.assertEqual(done["attempts"][0]["native"]["permission_denials"], ["WebSearch"])
        self.assertEqual(done["events"][0]["body"]["allowed_tools"], TOOLS, "requested permissions, not proof of use")
        (call,) = self.calls()
        self.assertEqual(call[call.index("--allowedTools") + 1], "WebFetch,WebSearch")
        self.assertNotIn("--settings", call)
        self.assertEqual(self.settings.read_text(), saved)

    def test_doctor_reports_the_policy_and_this_machines_defaults_by_name_only(self) -> None:
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["WebSearch", "Bash(make-never-printed *)"],
                            "deny": ["WebFetch(domain:internal.example)"]},
            "env": {"SOME_PRIVATE_NAME": "never-printed-value"}}))
        doctor = self.cli("doctor", "--engine", "claude", "--cwd", str(self.work), env=self.env)
        policy = doctor["claude_tool_policy"]
        self.assertEqual((policy["default_allowed_tools"], policy["allowable_tools"]), ([], TOOLS))
        self.assertEqual(policy["opt_in"], "--option 'allowed_tools=[\"WebSearch\",\"WebFetch\"]'")
        self.assertIn("dontAsk", policy["effect"])
        self.assertIn("deny", policy["precedence"])
        tools = doctor["engine"]["network_tools"]
        self.assertIs(tools["allowed_by_default"], False)
        self.assertIn("WebSearch", tools["default_allowed"])
        self.assertIn("WebFetch", tools["denied"])
        self.assertIs(tools["complete"], True)
        text = json.dumps(doctor)
        for content in ("never-printed-value", "SOME_PRIVATE_NAME", "make-never-printed", "internal.example"):
            self.assertNotIn(content, text)
        plain = self.cli("doctor", env=self.env)
        self.assertEqual(plain["claude_tool_policy"], policy)
        self.assertNotIn("engine", plain)


if __name__ == "__main__":
    unittest.main()

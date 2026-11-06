"""Issue #22: report ineffective Claude Bash rules without changing permissions or outcomes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config, wait_until, production_args, production_options
from test_terminal_observations import notification

from cli_relay import engines, procs
from cli_relay.ledger import payload_hash


class BashRuleWarnings(unittest.TestCase):
    def test_only_cp_with_a_later_flag_warns(self):
        for command, warns in (("cp -n a b", True), ("cp -p a b", True), ("cp a -n b", True),
                               ("cp a b", False), ("ls -la x", False), ("mkdir -p k/l", False),
                               ("mv -n a b", False), ("rm -f a", False), ("/bin/cp -n a b", False)):
            with self.subTest(command=command):
                payload = {"options": {"allowed_bash_commands": [command]}}
                warnings = engines.bash_rule_warnings("claude", payload)
                self.assertEqual(len(warnings), int(warns))
                if warns:
                    self.assertEqual(warnings[0]["code"], "bash_rule_not_honoured")
                    self.assertEqual(warnings[0]["command"], command)
                    for text in ("Claude Code", "2.1.312", "prefix rule is listed", "cp <src> <dst>",
                                 "drop -n only when overwriting the destination is acceptable", "test -e"):
                        self.assertIn(text, warnings[0]["hint"])
        self.assertEqual(engines.bash_rule_warnings("fixture", payload), [])


class BashDenialParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="relay-bash-denials-")
        self.addCleanup(self.tmp.cleanup)
        self.stdout = Path(self.tmp.name) / "stdout.jsonl"

    def parse(self, denials, allowed=(), engine="claude"):
        self.stdout.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                           "result": "done", "permission_denials": denials}) + "\n")
        return engines.parse_output(engine, self.stdout, allowed_bash_commands=allowed)

    def test_exact_and_prefix_matches_are_stripped_unique_and_sorted(self):
        denials = [{"tool_name": "Bash", "tool_input": {"command": command, "description": "not exported"}}
                   for command in ("python3 checker.py", " cp -n a b \n", "python3 checker.py", "cp -n a bad")]
        denials += [{"tool_name": "Monitor", "tool_input": {"command": "cp -n a b"}},
                    {"tool": "Bash", "tool_input": {"command": "python3 other.py"}}]
        parsed = self.parse(denials, ["cp -n a b", "python3", "python3 checker.py"])
        self.assertEqual(parsed["warnings"], [{"code": "allowed_rule_denied",
                                               "commands": ["cp -n a b", "python3", "python3 checker.py"]}])
        self.assertEqual(parsed["native"], self.parse(denials)["native"])
        self.assertNotIn("not exported", json.dumps(parsed))
        self.assertNotIn("tool_input", json.dumps(parsed))

    def test_nonmatching_denials_do_not_warn(self):
        for command in ("cp -p a b", "cp -n a bad", "env cp -n a b", "cp -n a b\nother"):
            with self.subTest(command=command):
                parsed = self.parse([{"tool_name": "Bash", "tool_input": {"command": command}}], ["cp -n a b"])
                self.assertNotIn("warnings", parsed)

    def test_old_and_odd_denial_shapes_do_not_crash(self):
        for denials in (None, [], ["Bash"], "Bash", 1, True, {"tool_name": "Bash"},
                        [None, "Bash", 1, [], {}, {"tool_name": "Bash"},
                         *({"tool_name": "Bash", "tool_input": value}
                           for value in (None, "cp -n a b", [], 1, {}, {"command": None},
                                         {"command": 1}, {"command": []}, {"command": " "}))]):
            with self.subTest(denials=denials):
                self.assertNotIn("warnings", self.parse(denials, ["cp -n a b"]))

    def test_truncation_happens_after_matching_and_keeps_commands_unique(self):
        command = "cp -n " + "a" * 220 + " b"
        other = "cp -n " + "z" * 220 + " b"
        denials = [{"tool_name": "Bash", "tool_input": {"command": value}}
                   for value in (other, command + " extra", command)]
        warning, = self.parse(denials, [command, other])["warnings"]
        self.assertEqual(warning, {"code": "allowed_rule_denied", "commands": [command[:200], other[:200]]})
        self.assertTrue(all(len(value) == 200 for value in warning["commands"]))
        parsed = self.parse([{"tool_name": "Bash", "tool_input": {"command": command[:-1] + "c"}}], [command])
        self.assertNotIn("warnings", parsed)

    def test_other_tools_and_fixture_outputs_do_not_add_a_diagnostic(self):
        denial = {"tool_name": "Monitor", "tool_input": {"command": "cp -n a b"}}
        self.assertNotIn("warnings", self.parse([denial], ["cp -n a b"]))
        denial["tool_name"] = "Bash"
        self.assertNotIn("warnings", self.parse([denial], ["cp -n a b"], engine="fixture"))


@needs_clean_cli_config
class BashDiagnosticsRoundTrip(RelayTestCase):
    def submit_claude(self, commands, *, denials=(), ok=True, crash=False):
        output = {"type": "result", "subtype": "success" if ok else "error_during_execution",
                  "is_error": not ok, "result": "done", "permission_denials": list(denials)}
        path = self.tmp / "claude"
        path.write_text(f"#!{sys.executable}\nimport json, sys\n"
                        "if sys.argv[1] == 'auth':\n"
                        "    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))\n"
                        "    sys.exit(0)\n"
                        "print(json.dumps({'type': 'system', 'subtype': 'init', 'apiKeySource': 'none', "
                        "'permissionMode': 'dontAsk'}))\n"
                        f"print(json.dumps({output!r}))\nsys.exit({0 if ok else 1})\n")
        path.chmod(0o755)
        extra = {"CLI_RELAY_CLAUDE_BIN": str(path)}
        if crash:
            extra["CLI_RELAY_TEST_CRASH"] = "after_exit_before_commit"
        env = base_env(isolated_env(self.tmp, extra))
        sub = self.cli("submit", *production_args('claude'), "--key", f"bash-{ok}-{crash}", "--engine", "claude", "--cwd", str(self.work),
                       "--prompt", "work", "--option", "allowed_bash_commands=" + json.dumps(commands), env=env)
        return sub, env

    def finish(self, sub, env, crash=False):
        if crash:
            wait_until(lambda: (lambda r: r["keeper_pid"] and procs.same_process_alive(
                r["keeper_pid"], r["keeper_token"]) is False)(self.attempt_row(sub["task_id"])))
        return self.cli("wait", sub["task_id"], "--timeout", "30", env=env)

    def test_submit_warning_does_not_change_payload_hash_or_duplicates(self):
        commands = ["cp -p a b", "cp a b", "cp -n a b", "ls -la x", "mkdir -p k/l"]
        sub, env = self.submit_claude(commands)
        self.assertTrue(sub["accepted"])
        self.assertEqual([(w["code"], w["command"]) for w in sub["warnings"]],
                         [("bash_rule_not_honoured", "cp -n a b"), ("bash_rule_not_honoured", "cp -p a b")])
        done = self.finish(sub, env)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertNotIn("warnings", done["events"][0]["body"])
        expected = {"engine": "claude", "cwd": str(self.work.resolve()), "prompt": "work",
                    "options": {**production_options('claude'), "allowed_bash_commands": sorted(commands)}, "task_class": "highest_quality", "resource_class": "standard"}
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        self.assertEqual(stored, expected)
        self.assertEqual(sub["payload_sha256"], payload_hash(expected))
        again, _ = self.submit_claude(list(reversed(commands)))
        self.assertEqual((again["status"], again["task_id"], again["payload_sha256"], again["warnings"]),
                         ("duplicate", sub["task_id"], sub["payload_sha256"], sub["warnings"]))

    def test_unaffected_submit_keeps_old_output_shape_and_hash(self):
        commands = ["cp a b", "ls -la x", "mkdir -p k/l"]
        sub, env = self.submit_claude(commands)
        self.assertNotIn("warnings", sub)
        self.assertEqual(sub["payload_sha256"], payload_hash({"engine": "claude", "cwd": str(self.work.resolve()),
                         "prompt": "work", "task_class":"highest_quality", "resource_class":"standard",
                         "options": {**production_options("claude"), "allowed_bash_commands": commands}}))
        self.assertEqual(self.finish(sub, env)["task"]["state"], "succeeded")

    def assert_diagnostics(self, done, outcome):
        expected = [{"code": "allowed_rule_denied", "commands": ["cp -n a b", "python3"]},
                    {"code": "permission_denials", "tools": ["Bash", "Monitor"]}]
        self.assertEqual(done["task"]["state"], outcome)
        delivered = notification(done)
        for record in (done["attempts"][0]["evidence"], done["events"][0]["body"],
                       delivered["cli_relay"]["events"][0]):
            self.assertEqual(record["warnings"], expected)
        status = self.cli("status", done["task"]["task_id"])
        self.assertEqual(status["attempts"][0]["evidence"]["warnings"], expected)
        self.assertIn("warnings=allowed_rule_denied,permission_denials", delivered["summary"])
        self.assertIn("allowed_rule_denied=2 first_command=cp -n a b", delivered["summary"])
        self.assertNotIn("not exported", json.dumps(done))

    def run_denials(self, *, ok=True, crash=False):
        denials = [{"tool_name": "Bash", "tool_input": {"command": command, "description": "not exported"}}
                   for command in ("python3 checker.py", " cp -n a b ", "cp -n a b", "cp -p a b")]
        denials.append({"tool_name": "Monitor", "tool_input": {"detail": "not exported"}})
        sub, env = self.submit_claude(["cp -n a b", "python3"], denials=denials, ok=ok, crash=crash)
        return self.finish(sub, env, crash=crash)

    def test_recovered_success_keeps_outcome_and_reports_diagnostics(self):
        self.assert_diagnostics(self.run_denials(), "succeeded")

    def test_native_failure_keeps_needs_input_and_reports_diagnostics(self):
        self.assert_diagnostics(self.run_denials(ok=False), "needs_input")

    def test_runtime_arguments_are_not_exported_after_bash_denial(self):
        secret = "PLACEHOLDER-SECRET-123"
        command = f'curl -H "Authorization: Bearer {secret}" https://example.invalid'
        expected = [{"code": "allowed_rule_denied", "commands": ["curl"]},
                    {"code": "permission_denials", "tools": ["Bash"]}]
        for crash in (False, True):
            with self.subTest(crash=crash):
                denial = {"tool_name": "Bash", "tool_input": {"command": command}}
                sub, env = self.submit_claude(["curl"], denials=[denial], ok=False, crash=crash)
                done = self.finish(sub, env, crash=crash)
                self.assertEqual(done["task"]["state"], "needs_input")
                status = self.cli("status", sub["task_id"])
                delivered = notification(done)
                events = [json.loads(row["body_json"]) for row in db_rows(
                    self.home, "SELECT body_json FROM events WHERE task_id=?", (sub["task_id"],))]
                self.assertEqual(len(events), 1)
                for record in (done["attempts"][0]["evidence"], status["attempts"][0]["evidence"],
                               *events, delivered["cli_relay"]["events"][0]):
                    self.assertNotIn(secret, json.dumps(record))
                    self.assertEqual(record["warnings"], expected)
                self.assertNotIn(secret, json.dumps(delivered))
                self.assertNotIn(secret, delivered["summary"])
                self.assertIn("allowed_rule_denied=1 first_command=curl", delivered["summary"])
                if crash:
                    self.assertEqual(events[0]["source"], "reconcile")

    def test_diagnostics_survive_keeper_crash(self):
        done = self.run_denials(crash=True)
        self.assert_diagnostics(done, "succeeded")
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")

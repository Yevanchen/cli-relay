"""A dispatcher can make success depend on the CLI's own completion report (issue #7): with
require_completion_marker=true a run only ends succeeded if the last non-empty line of the CLI's
final message is exactly `CLI-RELAY-STATUS: complete`; otherwise it ends needs_input. The relay
reads that one line and nothing else, and the marker never changes any other outcome."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config, wait_until

from notify_helpers import write_config
from cli_relay import engines, notify, procs
from cli_relay.errors import UsageError
from cli_relay.ledger import payload_hash

DONE = "CLI-RELAY-STATUS: complete"
OPEN = "CLI-RELAY-STATUS: incomplete"
MARK = "require_completion_marker=true"
NOTIFIER = {"source_id": "test-host", "default_route": "bot",
            "routes": {"bot": {"consumer": "bot", "chat": "test chat",
                               "adapter": {"type": "command", "argv": ["/usr/bin/false"]}}}}


class MarkerLine(unittest.TestCase):
    def test_only_an_exact_last_line_is_a_marker(self) -> None:
        for text, marker in ((f"All 235 tests passed.\n\n{DONE}", "complete"), (DONE, "complete"),
                             (f"32 tests could not bind localhost.\n{OPEN}\n", "incomplete"),
                             (f"done\n   {DONE}\t\n\n  \n", "complete"), (f"open items\r\n{OPEN}\r\n", "incomplete"),
                             (f"{OPEN}\nretried, all green\n{DONE}", "complete"),
                             (f"{DONE}\nthen it broke again\n{OPEN}", "incomplete")):
            with self.subTest(text=text):
                self.assertEqual(engines.completion_marker(text), marker)
        for text in (None, "", " \n\n ", "All tests passed.", "Everything is complete.", f"{DONE}\nOne more thing.",
                     f"I will end with {DONE} when done", f"`{DONE}`", f"**{DONE}**", f"> {DONE}", f"- {DONE}",
                     f"```\n{DONE}\n```", f"{DONE}.", "cli-relay-status: complete", "CLI-RELAY-STATUS: COMPLETE",
                     "CLI-RELAY-STATUS:complete", "CLI-RELAY-STATUS:  complete", "CLI-RELAY-STATUS: complete, verified",
                     "CLI-RELAY-STATUS: done", "CLI-RELAY-STATUS: failed", "STATUS: complete", 42, [DONE],
                     {"result": DONE}):
            with self.subTest(text=text):
                self.assertIsNone(engines.completion_marker(text))


class OutcomeGate(unittest.TestCase):
    def decide(self, native, *, engine="fixture", required=True, exit_code=0, exit_known=True, cancel=False,
               init=None, guard=None):
        if init is None and engine != "codex":
            init = {"apiKeySource": "none", "permissionMode": "dontAsk"}
        parsed = {"native": native, "session_id": None, "init": init}
        return engines.decide(engine, parsed, env={}, session_id=None, exit_code=exit_code, exit_signal=None,
                              exit_known=exit_known, cancel_requested=cancel, guard=guard,
                              require_completion_marker=required)

    def test_success_needs_the_complete_line_on_every_engine(self) -> None:
        for engine in engines.ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(self.decide({"ok": True, "result": f"235/235 passed\n{DONE}"}, engine=engine),
                                 ("succeeded", {"completion_check": {"marker": "complete"}}))
                self.assertEqual(self.decide({"ok": True, "result": f"32 could not run\n{OPEN}"}, engine=engine),
                                 ("needs_input", {"completion_check": {"marker": "incomplete",
                                                                       "downgraded_from": "succeeded"}}))
                for native in ({"ok": True, "result": "All tests passed."}, {"ok": True, "result": None},
                               {"ok": True}, {"ok": True, "result": f"**{DONE}**"}):
                    self.assertEqual(self.decide(native, engine=engine),
                                     ("needs_input", {"completion_check": {"marker": None,
                                                                           "downgraded_from": "succeeded"}}))
                self.assertEqual(self.decide({"ok": True, "result": DONE}, engine=engine, exit_known=False)[0],
                                 "succeeded", "a run closed from its native result is gated the same way")

    def test_without_the_opt_in_nothing_changes(self) -> None:
        for required in (False, None, "true", 1):
            for text in ("All tests passed.", f"x\n{OPEN}", None):
                with self.subTest(required=required, text=text):
                    self.assertEqual(self.decide({"ok": True, "result": text}, required=required), ("succeeded", {}))

    def test_the_marker_never_changes_another_outcome(self) -> None:
        self.assertEqual(self.decide({"ok": False, "result": DONE}),
                         ("failed", {"completion_check": {"marker": "complete"}}))
        self.assertEqual(self.decide({"ok": True, "result": DONE}, exit_code=1)[0], "failed")
        self.assertEqual(self.decide(None), ("failed", {"completion_check": {"marker": None}}))
        self.assertEqual(self.decide({"ok": True, "result": DONE}, cancel=True, exit_code=None, exit_known=False)[0],
                         "succeeded")
        self.assertEqual(self.decide(None, cancel=True, exit_code=None, exit_known=False)[0], "cancelled")
        denied = {"ok": True, "result": DONE, "permission_denials": ["WebSearch"]}
        self.assertEqual(self.decide(denied, engine="claude"), ("needs_input", {"completion_check": {"marker": "complete"}}))
        blocked = self.decide({"ok": True, "result": DONE}, guard={"reason": "api_key_source_detected"})
        self.assertEqual(blocked, ("blocked", {"billing_guard": {"reason": "api_key_source_detected"}}))
        self.assertEqual(self.decide({"ok": True, "result": DONE}, engine="claude",
                                     init={"apiKeySource": "none", "permissionMode": "default"})[0], "blocked")

    def test_the_opt_in_is_a_typed_bool_on_every_engine_and_part_of_the_request(self) -> None:
        for engine in engines.ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(engines.validate_options(engine, {"require_completion_marker": True}),
                                 {"require_completion_marker": True})
                for value in ("true", 1, 0, None, [], DONE):
                    with self.subTest(value=value), self.assertRaisesRegex(UsageError, "wrong type"):
                        engines.validate_options(engine, {"require_completion_marker": value})
                self.assertIs(engines.request_details(engine, {"options": {"require_completion_marker": True}})
                              ["completion_marker_required"], True)
                for options in ({}, {"require_completion_marker": False}, {"require_completion_marker": "true"},
                                {"require_completion_marker": 1}):
                    self.assertIs(engines.request_details(engine, {"options": options})["completion_marker_required"],
                                  False)
        self.assertEqual(engines.request_details("codex", {"options": {}}),
                         {"sandbox": "read-only", "full_access": False, "network_access": False,
                          "completion_marker_required": False})
        self.assertEqual(engines.request_details("claude", {"options": {}}),
                         {"allowed_tools": [], "completion_marker_required": False})


class MarkerRoundTrip(RelayTestCase):
    def run_fixture(self, key, result, *, marker=True, env=None, expect=0):
        options = [f"result={json.dumps(result)}"] + ([MARK] if marker else [])
        sub = self.submit(key, duration=0, extra_options=options, env=env, expect=expect)
        return sub, (self.cli("wait", sub["task_id"], "--timeout", "30") if expect == 0 else None)

    def test_an_incomplete_or_missing_marker_ends_needs_input_with_the_reason_on_record(self) -> None:
        for key, result, marker in (("k-open", f"203/235 passed\n\n{OPEN}", "incomplete"),
                                    ("k-none", "203/235 passed; 32 could not bind localhost", None)):
            with self.subTest(key=key):
                sub, done = self.run_fixture(key, result)
                self.assertIs(sub["completion_marker_required"], True)
                self.assertEqual(done["task"]["state"], "needs_input", done)
                self.assertIs(done["task"]["completion_marker_required"], True)
                self.assertIs(done["task"]["options"]["require_completion_marker"], True)
                attempt = done["attempts"][0]
                self.assertEqual((attempt["outcome"], attempt["native"]["ok"]), ("needs_input", True))
                self.assertEqual(attempt["exit"], {"code": 0, "signal": None, "known": True})
                self.assertEqual(attempt["evidence"]["completion_check"],
                                 {"marker": marker, "downgraded_from": "succeeded"})
                body = done["events"][0]["body"]
                self.assertEqual((body["state"], body["outcome"], body["native_ok"], body["completion_marker_required"],
                                  body["completion_marker"]), ("needs_input", "needs_input", True, True, marker))

    def test_the_complete_marker_succeeds_and_without_the_opt_in_nothing_changes(self) -> None:
        sub, done = self.run_fixture("k-done", f"235/235 passed\n{DONE}")
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assertEqual(done["attempts"][0]["evidence"]["completion_check"], {"marker": "complete"})
        self.assertEqual(done["events"][0]["body"]["completion_marker"], "complete")
        sub, done = self.run_fixture("k-plain", f"203/235 passed\n{OPEN}", marker=False)
        self.assertIs(sub["completion_marker_required"], False)
        self.assertEqual(done["task"]["state"], "succeeded", "the CLI's own result decides without the opt-in")
        self.assertNotIn("completion_check", done["attempts"][0]["evidence"])
        body = done["events"][0]["body"]
        self.assertIs(body["completion_marker_required"], False)
        self.assertNotIn("completion_marker", body, "the result is not interpreted without the opt-in")

    def test_the_opt_in_is_part_of_the_payload_and_its_hash(self) -> None:
        sub, done = self.run_fixture("k-hash", DONE)
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        self.assertIs(stored["options"]["require_completion_marker"], True)
        self.assertEqual(sub["payload_sha256"], payload_hash(stored))
        again, _ = self.run_fixture("k-hash", DONE)
        self.assertEqual((again["status"], again["task_id"], again["payload_sha256"]),
                         ("duplicate", sub["task_id"], sub["payload_sha256"]))
        conflict, _ = self.run_fixture("k-hash", DONE, marker=False, expect=3)
        self.assertEqual(conflict["error"], "conflict")
        rejected = self.submit("k-type", duration=0, extra_options=['require_completion_marker="true"'], expect=2)
        self.assertEqual(rejected["error"], "usage")
        self.assertEqual(len(db_rows(self.home, "SELECT task_id FROM tasks")), 1)

    def test_a_keeper_crash_reaches_the_same_outcome_through_reconciliation(self) -> None:
        sub = self.submit("k-crash", duration=0, extra_options=[f"result={json.dumps(OPEN)}", MARK],
                          env=base_env({"CLI_RELAY_TEST_CRASH": "after_exit_before_commit"}))
        wait_until(lambda: (lambda r: bool(r["keeper_pid"]) and procs.same_process_alive(
            r["keeper_pid"], r["keeper_token"]) is False)(self.attempt_row(sub["task_id"])))
        done = wait_until(lambda: (lambda s: s if s["task"]["state"] not in ("accepted", "running") else None)(
            self.status(sub["task_id"])), timeout=30, interval=0.3)
        self.assertEqual(done["task"]["state"], "needs_input", done)
        self.assertEqual(done["attempts"][0]["evidence"]["completion_check"],
                         {"marker": "incomplete", "downgraded_from": "succeeded"})
        self.assertEqual(done["attempts"][0]["evidence"]["exit_source"], "exit.json")
        body = done["events"][0]["body"]
        self.assertEqual((body["source"], body["outcome"], body["completion_marker"]),
                         ("reconcile", "needs_input", "incomplete"))

    def test_the_notification_carries_the_outcome_and_the_marker(self) -> None:
        self.run_fixture("k-notify", f"203/235 passed\n{OPEN}")
        write_config(self.home, NOTIFIER)
        with patch("cli_relay.notify.send", return_value={"outcome": "handed_off"}) as send:
            notify.flush(self.home, env=base_env())
        send.assert_called_once()
        notification = send.call_args.args[1]
        self.assertEqual(notification["status"], "needs_input")
        ref = notification["cli_relay"]["events"][0]
        self.assertEqual((ref["outcome"], ref["completion_marker_required"], ref["completion_marker"]),
                         ("needs_input", True, "incomplete"))
        self.assertIn("needs_input", notification["summary"])

    def test_doctor_describes_the_marker_contract(self) -> None:
        policy = self.cli("doctor")["completion_marker_policy"]
        self.assertIs(policy["default_required"], False)
        self.assertEqual(policy["opt_in"], "--option require_completion_marker=true")
        self.assertEqual((policy["complete_line"], policy["incomplete_line"]), (DONE, OPEN))
        self.assertIn("needs_input", policy["rule"])
        self.assertIn("never edits a prompt", policy["prompt"])
        self.assertIn("not verification", policy["limits"])


def echo_codex(tmp: Path) -> Path:
    """No network and no model: the final agent message is the prompt itself, so each attempt's
    marker is chosen by its prompt. The rollout is shaped like Codex 0.163.0's."""
    script = r'''
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
if args[0] == "login":
    print("Logged in using ChatGPT")
    sys.exit(0)
prompt = sys.stdin.read()
thread = args[-2] if "resume" in args else str(uuid.uuid4())
directory = Path(os.environ["CODEX_HOME"]) / "sessions/2026/10/21"
directory.mkdir(parents=True, exist_ok=True)
with (directory / ("rollout-test-" + thread + ".jsonl")).open("a") as f:
    if "resume" not in args:
        f.write(json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n")
    f.write(json.dumps({"type": "turn_context", "payload": {"sandbox_policy": {"type": "read-only"},
                                                            "active_permission_profile": None}}) + "\n")
for event in ({"type": "thread.started", "thread_id": thread},
              {"type": "item.completed", "item": {"type": "agent_message", "text": prompt}},
              {"type": "turn.completed"}):
    print(json.dumps(event))
'''
    path = tmp / "codex"
    path.write_text(f"#!{sys.executable}\n" + script)
    path.chmod(0o755)
    return path


@needs_clean_cli_config
class MarkerPerAttempt(RelayTestCase):
    def test_each_attempt_is_judged_by_its_own_final_message(self) -> None:
        env = base_env(isolated_env(self.tmp, {"CLI_RELAY_CODEX_BIN": str(echo_codex(self.tmp))}))
        sub = self.cli("submit", "--key", "k-codex-marker", "--engine", "codex", "--cwd", str(self.work),
                       "--prompt", f"Ran 203/235; 32 need localhost.\n{OPEN}", "--option", MARK, env=env)
        done = self.cli("wait", sub["task_id"], "--timeout", "30", env=env)
        self.assertEqual(done["task"]["state"], "needs_input", done)
        resumed = self.cli("resume", sub["task_id"], "--prompt", f"Ran the 32 remaining; 235/235 pass.\n{DONE}",
                           env=env)
        self.assertIs(resumed["completion_marker_required"], True)
        done = self.cli("wait", sub["task_id"], "--timeout", "30", env=env)
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assertEqual([a["outcome"] for a in done["attempts"]], ["needs_input", "succeeded"])
        self.assertEqual([e["body"]["completion_marker"] for e in done["events"]], ["incomplete", "complete"])
        self.cli("resume", sub["task_id"], "--prompt", "Checked again, all fine.", env=env)
        done = self.cli("wait", sub["task_id"], "--timeout", "30", env=env)
        self.assertEqual(done["task"]["state"], "needs_input", "an attempt without the line is not succeeded")
        self.assertEqual(done["attempts"][-1]["evidence"]["completion_check"],
                         {"marker": None, "downgraded_from": "succeeded"})


if __name__ == "__main__":
    unittest.main()

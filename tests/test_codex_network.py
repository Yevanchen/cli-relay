"""Network access in the Codex workspace-write sandbox (localhost included) needs a typed per-task
opt-in, never widens the sandbox beyond workspace-write, and stays explicit through exact resume,
recovery and delivery (issue #7)."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config
from test_ind_02_codex_permissions import fake_codex as fixed_turn_codex

from notify_helpers import write_config
from cli_relay import engines, notify
from cli_relay.errors import UsageError
from cli_relay.ledger import payload_hash

WW = "workspace-write"
FULL = "danger-full-access"
OPT_IN = {"sandbox": WW, "allow_network": True}
ON = "sandbox_workspace_write.network_access=true"
OFF = "sandbox_workspace_write.network_access=false"
THREAD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NOTIFIER = {"source_id": "test-host", "default_route": "bot",
            "routes": {"bot": {"consumer": "bot", "chat": "test chat",
                               "adapter": {"type": "command", "argv": ["/usr/bin/false"]}}}}


def config_overrides(argv):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]


def network_settings(argv):
    return [arg for arg in argv if "sandbox_workspace_write" in arg]


class NetworkOptionsAndArgv(unittest.TestCase):
    def build(self, options, resume=None):
        return engines.build("codex", payload={"cwd": "/", "options": options},
                             attempt={"resume_session_id": resume}, run_dir=Path("/"),
                             fixture_state_dir="/", env={"CLI_RELAY_CODEX_BIN": "/bin/echo"}).argv

    def test_opt_in_is_a_typed_bool_for_workspace_write_only(self) -> None:
        self.assertEqual(engines.validate_options("codex", OPT_IN), OPT_IN)
        self.assertEqual(engines.validate_options("codex", {"sandbox": WW, "allow_network": False}),
                         {"sandbox": WW, "allow_network": False})
        for value in ("true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(UsageError, "wrong type"):
                engines.validate_options("codex", {"sandbox": WW, "allow_network": value})
        for options in ({"allow_network": True}, {"allow_network": False},
                        {"sandbox": "read-only", "allow_network": True}, {"sandbox": "read-only", "allow_network": False}):
            with self.subTest(options=options), self.assertRaisesRegex(UsageError, 'only to sandbox="workspace-write"'):
                engines.validate_options("codex", options)
        for flag in (True, False):
            with self.subTest(flag=flag), self.assertRaisesRegex(UsageError, "redundant with sandbox danger-full"):
                engines.validate_options("codex", {"sandbox": FULL, "allow_full_access": True, "allow_network": flag})
        with self.assertRaisesRegex(UsageError, "allow_full_access=true"):
            engines.validate_options("codex", {"sandbox": FULL, "allow_network": True})
        for engine in ("claude", "fixture"):
            with self.subTest(engine=engine), self.assertRaisesRegex(UsageError, "not supported"):
                engines.validate_options(engine, {"allow_network": True})

    def test_default_is_off_and_pinned_on_every_workspace_write_run(self) -> None:
        for resume in (None, THREAD):
            for options in ({"sandbox": WW}, {"sandbox": WW, "allow_network": False}):
                with self.subTest(resume=resume, options=options):
                    argv = self.build(options, resume)
                    self.assertEqual(argv[argv.index("--sandbox") + 1], WW)
                    self.assertEqual(network_settings(argv), [OFF], "no config layer may turn the network on")
            for options in ({}, {"sandbox": "read-only"}, {"sandbox": FULL, "allow_full_access": True}):
                with self.subTest(resume=resume, options=options):
                    self.assertEqual(network_settings(self.build(options, resume)), [])

    def test_opt_in_adds_exactly_the_network_setting_on_new_and_resumed_runs(self) -> None:
        for resume in (None, THREAD):
            with self.subTest(resume=resume):
                argv = self.build(OPT_IN, resume)
                self.assertEqual(argv[argv.index("--sandbox") + 1], WW)
                self.assertEqual(network_settings(argv), [ON])
                self.assertEqual(argv[argv.index(ON) - 1], "-c")
                self.assertNotIn(FULL, " ".join(argv))
                self.assertNotIn("--allow-network", argv, "the opt-in is relay metadata, not a Codex flag")
                self.assertFalse(set(argv) & {"--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto",
                                              "--approve-for-me", "--add-dir", "--profile", "-p"})
                if resume:
                    self.assertEqual(argv[1:5], ["exec", "--sandbox", WW, "resume"])
                    self.assertEqual(config_overrides(argv), [f'sandbox_mode="{WW}"', ON])
                    self.assertEqual(argv[-2:], [THREAD, "-"])
                else:
                    self.assertEqual(config_overrides(argv), [ON])
                    self.assertEqual(argv[-1], "-")
        argv = self.build({**OPT_IN, "model": "gpt-6-astra", "effort": "max"}, THREAD)
        self.assertEqual(config_overrides(argv), [f'sandbox_mode="{WW}"', ON, 'model_reasoning_effort="max"'])

    def test_argv_recheck_accepts_only_the_setting_built_from_the_options(self) -> None:
        def spellings(value):
            return (["-c", value], ["--config", value], [f"--config={value}"], [f"-c{value}"])

        base = ["codex", "exec", "--sandbox", WW]
        for flags in (*spellings(ON), ["-c", "sandbox_workspace_write.network_access = true"]):
            with self.subTest(flags=flags):
                engines.assert_safe("codex", base + flags, options=OPT_IN)
                for options in (None, {}, {"sandbox": WW}, {"sandbox": WW, "allow_network": False},
                                {"sandbox": WW, "allow_network": "true"}, {"sandbox": WW, "allow_network": 1},
                                {"allow_network": True}, {"sandbox": "read-only", "allow_network": True},
                                {"sandbox": FULL, "allow_full_access": True},
                                {"sandbox": FULL, "allow_full_access": True, "allow_network": True}):
                    with self.subTest(options=options), self.assertRaisesRegex(RuntimeError, "config override"):
                        engines.assert_safe("codex", base + flags, options=options)
        for flags in spellings(OFF):
            with self.subTest(flags=flags):
                engines.assert_safe("codex", base + flags, options={"sandbox": WW})
                for options in (OPT_IN, None, {}, {"sandbox": FULL, "allow_full_access": True}):
                    with self.subTest(options=options), self.assertRaisesRegex(RuntimeError, "config override"):
                        engines.assert_safe("codex", base + flags, options=options)
        for value in ("sandbox_workspace_write.network_access=1", 'sandbox_workspace_write.network_access="true"',
                      "sandbox_workspace_write.network_access=TRUE", "sandbox_workspace_write={network_access=true}",
                      'sandbox_workspace_write.writable_roots=["/"]', "sandbox_workspace_write.exclude_slash_tmp=true",
                      '"sandbox_workspace_write".network_access=true', 'sandbox_mode="danger-full-access"',
                      'approval_policy="never"', "features.network_proxy=true", 'sandbox_permissions=["network"]'):
            for flags in spellings(value):
                with self.subTest(flags=flags), self.assertRaises(RuntimeError):
                    engines.assert_safe("codex", base + flags, options=OPT_IN)
        for flag in ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto", "--approve-for-me"):
            with self.subTest(flag=flag), self.assertRaisesRegex(RuntimeError, "forbidden flags"):
                engines.assert_safe("codex", base + ["-c", ON, flag], options=OPT_IN)

    def test_permission_details_report_network_access(self) -> None:
        for options, sandbox, full, network in (({}, "read-only", False, False), ({"sandbox": WW}, WW, False, False),
                                                ({"sandbox": WW, "allow_network": False}, WW, False, False),
                                                (OPT_IN, WW, False, True),
                                                ({"sandbox": FULL, "allow_full_access": True}, FULL, True, True)):
            with self.subTest(options=options):
                self.assertEqual(engines.permission_details("codex", {"options": options}),
                                 {"sandbox": sandbox, "full_access": full, "network_access": network})
        for value in ("true", 1):
            with self.subTest(value=value):
                details = engines.permission_details("codex", {"options": {"sandbox": WW, "allow_network": value}})
                self.assertIs(details["network_access"], False, "a truthy non-boolean is never an opt-in")
        self.assertIs(engines.network_enabled("codex", {"options": {"allow_network": True}}), False)
        self.assertNotIn("network_access", engines.permission_details("claude", {"options": {}}))


class NetworkRuntimeVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-network-"))
        self.env = {"CODEX_HOME": str(self.tmp)}
        self.path = self.tmp / "sessions/2026/10/21" / f"rollout-test-{THREAD}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.reset()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def reset(self):
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n")

    def append_turn(self, sandbox=WW, **policy):
        # As Codex 0.163.0 records it: workspace-write turns carry a boolean, the others no network key.
        if sandbox == WW:
            policy = {"network_access": False, "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False, **policy}
        with self.path.open("a") as f:
            f.write(json.dumps({"type": "turn_context", "payload": {"sandbox_policy": {"type": sandbox, **policy},
                                                                    "active_permission_profile": None}}) + "\n")

    def check(self, sandbox=WW, **kwargs):
        return engines.permission_violation("codex", self.env, THREAD, sandbox, **kwargs)

    def test_a_turn_with_network_needs_the_opt_in(self) -> None:
        self.append_turn(network_access=True)
        self.assertIsNone(self.check(allow_network=True))
        for opt_in in (False, None, "true", 1):
            with self.subTest(opt_in=opt_in):
                self.assertEqual(self.check(allow_network=opt_in),
                                 {"reason": "network_not_as_requested", "requested": WW, "network_access": False,
                                  "observed": [WW]})
        self.assertEqual(self.check("read-only", allow_network=True)["reason"], "sandbox_not_as_requested")

    def test_turns_without_network_are_narrower_and_accepted_either_way(self) -> None:
        self.append_turn()
        self.append_turn("read-only")
        for opt_in in (False, True):
            with self.subTest(opt_in=opt_in):
                self.assertIsNone(self.check(allow_network=opt_in))
        self.reset()
        self.append_turn(FULL)
        self.append_turn(network_access=True)
        self.assertIsNone(self.check(FULL, allow_full_access=True), "full access includes the network")

    def test_a_network_value_other_than_false_is_not_taken_for_off(self) -> None:
        for sandbox, value in ((WW, None), (WW, "enabled"), (WW, "restricted"), (WW, 0), (WW, "false"),
                               ("read-only", True)):
            with self.subTest(sandbox=sandbox, value=value):
                self.reset()
                self.append_turn(sandbox, network_access=value)
                self.assertEqual(self.check(sandbox)["reason"], "network_not_as_requested")

    def test_a_resumed_attempt_still_needs_its_own_turn_and_the_same_opt_in(self) -> None:
        self.append_turn(network_access=True)
        checkpoint = engines.codex_permission_checkpoint(self.env, THREAD)
        resumed = {"permission_checkpoint": checkpoint, "resume_session_id": THREAD}
        self.assertEqual(self.check(allow_network=True, **resumed)["missing"], "current_turn_context")
        self.append_turn(network_access=True)
        self.assertIsNone(self.check(allow_network=True, **resumed))
        self.assertEqual(self.check(**resumed)["reason"], "network_not_as_requested")

    def test_decide_blocks_and_names_the_reason(self) -> None:
        self.append_turn(network_access=True)
        parsed = {"native": {"ok": True, "status": "completed", "result": "done"}, "init": None, "session_id": THREAD}
        common = {"env": self.env, "session_id": THREAD, "exit_code": 0, "exit_signal": None, "exit_known": True,
                  "cancel_requested": False, "sandbox": WW}
        outcome, evidence = engines.decide("codex", parsed, **common)
        self.assertEqual((outcome, evidence["permission_check"]["reason"]), ("blocked", "network_not_as_requested"))
        self.assertEqual(engines.decide("codex", parsed, allow_network=True, **common), ("succeeded", {}))


def network_codex(tmp: Path) -> Path:
    """No network and no model: record argv and append a rollout shaped like Codex 0.163.0's. The
    turn's workspace-write policy takes network_access from the user config, with -c on top (as the
    real precedence is), and only with it can the test suite bind 127.0.0.1, as under Seatbelt. The
    final message reports the result with the completion marker."""
    script = r'''
import json, os, sys, tomllib, uuid
from pathlib import Path
args = sys.argv[1:]
if args[0] == "login":
    print("Logged in using ChatGPT")
    sys.exit(0)
sys.stdin.read()
with open(MARKER, "a") as f:
    f.write(json.dumps(args) + "\n")
home = Path(os.environ["CODEX_HOME"])
config = tomllib.loads((home / "config.toml").read_text()) if (home / "config.toml").exists() else {}
network = config.get("sandbox_workspace_write", {}).get("network_access", False)
for i, arg in enumerate(args[:-1]):
    if arg == "-c" and args[i + 1].startswith("sandbox_workspace_write.network_access="):
        network = args[i + 1].partition("=")[2] == "true"
sandbox = args[args.index("--sandbox") + 1]
policy = {"type": sandbox}
if sandbox == "workspace-write":
    policy.update(network_access=network, exclude_tmpdir_env_var=False, exclude_slash_tmp=False)
thread = args[-2] if "resume" in args else str(uuid.uuid4())
directory = home / "sessions/2026/10/21"
directory.mkdir(parents=True, exist_ok=True)
with (directory / ("rollout-test-" + thread + ".jsonl")).open("a") as f:
    if "resume" not in args:
        f.write(json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n")
    f.write(json.dumps({"type": "turn_context", "payload": {"sandbox_policy": policy,
                                                            "active_permission_profile": None}}) + "\n")
if sandbox == "danger-full-access" or (sandbox == "workspace-write" and network):
    text = "235/235 tests passed.\n\nCLI-RELAY-STATUS: complete"
else:
    text = ("203/235 tests passed; 32 could not bind 127.0.0.1 (Operation not permitted).\n"
            "Open item: rerun them where localhost is allowed.\n\nCLI-RELAY-STATUS: incomplete")
for event in ({"type": "thread.started", "thread_id": thread},
              {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
              {"type": "turn.completed"}):
    print(json.dumps(event))
'''
    path = tmp / "codex"
    path.write_text(f"#!{sys.executable}\nMARKER={str(tmp / 'calls.jsonl')!r}\n" + script)
    path.chmod(0o755)
    return path


@needs_clean_cli_config
class NetworkRoundTrip(RelayTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.env = base_env(isolated_env(self.tmp, {"CLI_RELAY_CODEX_BIN": str(network_codex(self.tmp))}))

    def submit_codex(self, key="tests", options=None, env=None, expect=0):
        args = ["submit", "--key", key, "--engine", "codex", "--cwd", str(self.work), "--prompt",
                "Run the full test suite and report the result."]
        for name, value in (OPT_IN if options is None else options).items():
            args += ["--option", f"{name}={json.dumps(value)}"]
        return self.cli(*args, env=env or self.env, expect=expect)

    def wait(self, task_id):
        return self.cli("wait", task_id, "--timeout", "30", env=self.env)

    def calls(self):
        path = self.tmp / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_usage_errors_create_no_task_and_start_nothing(self) -> None:
        for options in ({"allow_network": True}, {"sandbox": "read-only", "allow_network": True},
                        {"sandbox": FULL, "allow_full_access": True, "allow_network": True},
                        {"sandbox": WW, "allow_network": "true"}, {"sandbox": WW, "allow_network": 1}):
            with self.subTest(options=options):
                rejected = self.submit_codex(options=options, expect=2)
                self.assertEqual(rejected["error"], "usage")
                self.assertIn("allow_network", rejected["message"])
        self.assertEqual(db_rows(self.home, "SELECT task_id FROM tasks"), [])
        self.assertEqual(self.calls(), [])

    def test_opt_in_is_stored_visible_everywhere_and_lets_the_tests_bind_localhost(self) -> None:
        sub = self.submit_codex()
        self.assertEqual((sub["sandbox"], sub["full_access"], sub["network_access"]), (WW, False, True))
        done = self.wait(sub["task_id"])
        self.assertEqual(done["task"]["state"], "succeeded", done)
        status = self.cli("status", sub["task_id"], env=self.env)
        self.assertEqual((status["task"]["sandbox"], status["task"]["network_access"]), (WW, True))
        self.assertEqual(status["task"]["options"], OPT_IN)
        self.assertIs(status["events"][0]["body"]["network_access"], True)
        events = self.cli("events", "--consumer", "bot", env=self.env)["events"]
        self.assertIs(events[0]["body"]["network_access"], True)
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        self.assertEqual(stored["options"], OPT_IN)
        self.assertEqual(sub["payload_sha256"], payload_hash(stored))
        duplicate = self.submit_codex()
        self.assertEqual((duplicate["status"], duplicate["task_id"]), ("duplicate", sub["task_id"]))
        self.assertEqual(duplicate["payload_sha256"], sub["payload_sha256"])
        self.assertIs(duplicate["network_access"], True)
        for options in ({"sandbox": WW}, {"sandbox": WW, "allow_network": False}):
            with self.subTest(options=options):
                conflict = self.submit_codex(options=options, expect=3)
                self.assertEqual(conflict["error"], "conflict")
                self.assertEqual(conflict["detail"]["existing_payload_sha256"], sub["payload_sha256"])
        (call,) = self.calls()
        self.assertEqual(call[call.index("--sandbox") + 1], WW)
        self.assertEqual(network_settings(call), [ON])

        write_config(self.home, NOTIFIER)
        with patch("cli_relay.notify.send", return_value={"outcome": "handed_off"}) as send:
            notify.flush(self.home, env=self.env)
        send.assert_called_once()
        notification = send.call_args.args[1]
        self.assertIs(notification["cli_relay"]["events"][0]["network_access"], True)
        self.assertIn("network_access=true", notification["summary"])
        self.assertNotIn("full_access=true", notification["summary"])

    def test_default_has_no_network_even_where_the_user_config_enables_it(self) -> None:
        config = Path(self.env["CODEX_HOME"]) / "config.toml"
        saved = "[sandbox_workspace_write]\nnetwork_access = true\n"
        config.write_text(saved)
        sub = self.submit_codex(options={"sandbox": WW})
        self.assertIs(sub["network_access"], False)
        done = self.wait(sub["task_id"])
        # Without the completion marker the CLI's own result decides, as before: the gap of issue #7.
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assertIn("CLI-RELAY-STATUS: incomplete", done["attempts"][0]["native"]["result"])
        self.assertIs(done["events"][0]["body"]["network_access"], False)
        (call,) = self.calls()
        self.assertEqual(network_settings(call), [OFF])
        self.assertEqual(config.read_text(), saved, "the user's configuration is never changed")

    def test_a_test_task_that_could_not_finish_is_not_succeeded_with_the_marker(self) -> None:
        gated = self.submit_codex(key="tests-gated", options={"sandbox": WW, "require_completion_marker": True})
        self.assertIs(gated["completion_marker_required"], True)
        done = self.wait(gated["task_id"])
        self.assertEqual(done["task"]["state"], "needs_input", done)
        attempt = done["attempts"][0]
        self.assertIs(attempt["native"]["ok"], True, "the CLI itself ended its turn normally")
        self.assertEqual(attempt["evidence"]["completion_check"],
                         {"marker": "incomplete", "downgraded_from": "succeeded"})
        body = done["events"][0]["body"]
        self.assertEqual((body["outcome"], body["network_access"], body["completion_marker_required"],
                          body["completion_marker"]), ("needs_input", False, True, "incomplete"))
        fixed = self.submit_codex(key="tests-gated-network", options={**OPT_IN, "require_completion_marker": True})
        done = self.wait(fixed["task_id"])
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assertEqual(done["attempts"][0]["evidence"]["completion_check"], {"marker": "complete"})
        self.assertEqual(done["events"][0]["body"]["completion_marker"], "complete")

    def test_opt_in_survives_exact_resume_and_a_keeper_crash(self) -> None:
        sub = self.submit_codex(options={**OPT_IN, "require_completion_marker": True})
        first = self.wait(sub["task_id"])
        self.assertEqual(first["task"]["state"], "succeeded", first)
        thread = first["attempts"][0]["native_session_id"]
        resumed = self.cli("resume", sub["task_id"], "--prompt", "Run the suite again.",
                           env={**self.env, "CLI_RELAY_TEST_CRASH": "after_exit_before_commit"})
        self.assertEqual((resumed["network_access"], resumed["completion_marker_required"]), (True, True))
        done = self.wait(sub["task_id"])
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assertEqual(done["attempts"][-1]["resume_session_id"], thread)
        last = done["events"][-1]["body"]
        self.assertEqual((last["source"], last["network_access"], last["completion_marker"]),
                         ("reconcile", True, "complete"))
        new, again = self.calls()
        self.assertEqual(network_settings(new), [ON])
        self.assertEqual(again[:4], ["exec", "--sandbox", WW, "resume"])
        self.assertEqual(config_overrides(again), [f'sandbox_mode="{WW}"', ON])
        self.assertEqual(again[-2:], [thread, "-"])

    def test_a_recorded_turn_with_network_but_no_opt_in_is_blocked(self) -> None:
        turn = {"type": WW, "network_access": True, "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False}
        for key, options, state in (("k-net-off", {"sandbox": WW}, "blocked"), ("k-net-on", OPT_IN, "succeeded")):
            with self.subTest(options=options):
                codex_home, bin_dir = self.tmp / f"codex-home-{key}", self.tmp / f"bin-{key}"
                codex_home.mkdir()
                bin_dir.mkdir()
                env = base_env({"CODEX_HOME": str(codex_home), "CLI_RELAY_CODEX_BIN": str(
                    fixed_turn_codex(bin_dir, self.tmp / f"m-{key}", turns=(turn,)))})
                sub = self.submit_codex(key=key, options=options, env=env)
                done = self.cli("wait", sub["task_id"], "--timeout", "30", env=env)
                self.assertEqual(done["task"]["state"], state, done["attempts"][0]["evidence"])
                if state == "blocked":
                    self.assertEqual(done["attempts"][0]["evidence"]["permission_check"],
                                     {"reason": "network_not_as_requested", "requested": WW, "network_access": False,
                                      "observed": [WW]})
                    self.assertIs(done["events"][0]["body"]["network_access"], False)

    def test_doctor_explains_the_default_and_the_opt_in_and_reports_names_and_booleans_only(self) -> None:
        config = Path(self.env["CODEX_HOME"]) / "config.toml"
        config.write_text('model = "private-model-name"\n\n[sandbox_workspace_write]\nnetwork_access = true\n'
                          'writable_roots = ["/never-printed-root"]\n')
        doctor = self.cli("doctor", "--engine", "codex", "--cwd", str(self.work), env=self.env)
        policy = doctor["codex_sandbox_policy"]
        self.assertIs(policy["network_default"], False)
        self.assertEqual(policy["network_opt_in"], '--option sandbox="workspace-write" --option allow_network=true')
        self.assertIn("not even to localhost", policy["network_without_opt_in"])
        self.assertIn(ON, policy["network_effect"])
        self.assertIn("never danger-full-access", policy["network_effect"])
        self.assertIn("usage error", policy["network_scope"])
        self.assertIn("no loopback-only setting", policy["network_risk"])
        self.assertEqual(doctor["engine"]["workspace_network"],
                         {"config_network_access": True, "set_by_layers": ["user"], "complete": True})
        text = json.dumps(doctor)
        for content in ("private-model-name", "never-printed-root"):
            self.assertNotIn(content, text)
        config.write_text("[sandbox_workspace_write]\nnetwork_access = \"yes\"\n")
        report = self.cli("doctor", "--engine", "codex", "--cwd", str(self.work), env=self.env)["engine"]
        self.assertEqual(report["workspace_network"], {"config_network_access": None, "set_by_layers": [],
                                                       "complete": False})
        config.unlink()
        report = self.cli("doctor", "--engine", "codex", "--cwd", str(self.work), env=self.env)["engine"]
        self.assertEqual(report["workspace_network"], {"config_network_access": None, "set_by_layers": [],
                                                       "complete": True})
        plain = self.cli("doctor", env=self.env)
        self.assertEqual(plain["codex_sandbox_policy"], policy)
        self.assertNotIn("engine", plain)


if __name__ == "__main__":
    unittest.main()

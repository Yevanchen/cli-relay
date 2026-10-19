"""Codex full access requires two task options and remains explicit through recovery and delivery."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config
from cli_relay import engines, notify
from cli_relay.errors import UsageError
from cli_relay.ledger import payload_hash

FULL = "danger-full-access"
OPT_IN = {"sandbox": FULL, "allow_full_access": True}
THREAD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FullAccessOptionsAndArgv(unittest.TestCase):
    def build(self, options, resume=None):
        return engines.build("codex", payload={"cwd": "/", "options": options},
                             attempt={"resume_session_id": resume}, run_dir=Path("/"),
                             fixture_state_dir="/", env={"CLI_RELAY_CODEX_BIN": "/bin/echo"}).argv

    def test_full_access_rejected_without_explicit_true(self) -> None:
        for options in ({"sandbox": FULL}, {"sandbox": FULL, "allow_full_access": False}):
            with self.subTest(options=options), self.assertRaisesRegex(UsageError, "allow_full_access=true"):
                engines.validate_options("codex", options)
        self.assertEqual(engines.validate_options("codex", OPT_IN), OPT_IN)

    def test_opt_in_is_a_typed_bool_and_codex_only(self) -> None:
        for value in ("true", "false", 1, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(UsageError, "wrong type"):
                engines.validate_options("codex", {"sandbox": FULL, "allow_full_access": value})
        for engine in ("claude", "fixture"):
            with self.subTest(engine=engine), self.assertRaisesRegex(UsageError, "not supported"):
                engines.validate_options(engine, {"allow_full_access": True})
        with self.assertRaises(UsageError):
            engines.validate_options("codex", {"sandbox": "unknown", "allow_full_access": True})

    def test_argv_uses_full_access_only_with_both_options_on_initial_and_resume(self) -> None:
        for resume in (None, THREAD):
            with self.subTest(resume=resume):
                argv = self.build(OPT_IN, resume)
                self.assertEqual(argv[argv.index("--sandbox") + 1], FULL)
                self.assertNotIn("--allow-full-access", argv, "the opt-in is relay metadata, not a Codex flag")
                if resume:
                    self.assertLess(argv.index("--sandbox"), argv.index("resume"))
                    self.assertIn(f'sandbox_mode="{FULL}"', argv)
                for options in ({"sandbox": FULL}, {"sandbox": FULL, "allow_full_access": False}):
                    with self.assertRaises(UsageError):
                        self.build(options, resume)

    def test_opt_in_alone_is_harmless_and_does_not_select_a_sandbox(self) -> None:
        for resume in (None, THREAD):
            for options, expected in (({}, "read-only"), ({"allow_full_access": True}, "read-only"),
                                      ({"sandbox": "workspace-write", "allow_full_access": True}, "workspace-write"),
                                      ({"allow_full_access": False}, "read-only")):
                with self.subTest(resume=resume, options=options):
                    argv = self.build(options, resume)
                    self.assertEqual(argv[argv.index("--sandbox") + 1], expected)
                    self.assertNotIn(FULL, " ".join(argv))

    def test_argv_recheck_requires_both_stored_options_for_all_flag_forms(self) -> None:
        forms = (["--sandbox", FULL], ["-s", FULL], [f"--sandbox={FULL}"], [f"-s{FULL}"],
                 ["-c", f'sandbox_mode="{FULL}"'], ["--config", f'sandbox_mode="{FULL}"'],
                 [f'--config=sandbox_mode="{FULL}"'], [f'-csandbox_mode="{FULL}"'])
        for flags in forms:
            argv = ["codex", "exec", *flags]
            with self.subTest(flags=flags):
                engines.assert_safe("codex", argv, options=OPT_IN)
                for options in (None, {}, {"allow_full_access": True}, {"sandbox": FULL},
                                {**OPT_IN, "allow_full_access": False}, {**OPT_IN, "allow_full_access": 1},
                                {**OPT_IN, "allow_full_access": "true"}):
                    with self.subTest(options=options), self.assertRaises(RuntimeError):
                        engines.assert_safe("codex", argv, options=options)

    def test_opt_in_does_not_allow_forbidden_flags_or_arbitrary_config(self) -> None:
        flags = ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto", "--approve-for-me",
                 "--last", "--dangerously-bypass-hook-trust")
        for flag in flags:
            for spelling in (flag, flag + "=true"):
                with self.subTest(flag=spelling), self.assertRaisesRegex(RuntimeError, "forbidden flags"):
                    engines.assert_safe("codex", ["codex", "exec", "--sandbox", FULL, spelling], options=OPT_IN)
        for config in ('approval_policy="never"', 'model_provider="other"', 'sandbox_mode="unknown"'):
            for flags in (["-c", config], ["--config=" + config], ["-c" + config]):
                with self.subTest(flags=flags), self.assertRaises(RuntimeError):
                    engines.assert_safe("codex", ["codex", "exec", *flags], options=OPT_IN)


class FullAccessRuntimeVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-full-access-"))
        self.env = {"CODEX_HOME": str(self.tmp)}
        self.path = self.tmp / "sessions/2026/10/20" / f"rollout-test-{THREAD}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def append_turn(self, sandbox=FULL, profile=None):
        with self.path.open("a") as f:
            f.write(json.dumps({"type": "turn_context", "payload": {"sandbox_policy": {"type": sandbox},
                                                                    "active_permission_profile": profile}}) + "\n")

    def test_full_access_runtime_requires_explicit_bool_and_matching_request(self) -> None:
        self.append_turn()
        self.assertIsNone(engines.permission_violation("codex", self.env, THREAD, FULL, allow_full_access=True))
        for opt_in in (False, None, "true", 1):
            with self.subTest(opt_in=opt_in):
                result = engines.permission_violation("codex", self.env, THREAD, FULL, allow_full_access=opt_in)
                self.assertEqual(result["reason"], "full_access_not_opted_in")
        for sandbox in (None, "read-only", "workspace-write"):
            with self.subTest(sandbox=sandbox):
                result = engines.permission_violation("codex", self.env, THREAD, sandbox, allow_full_access=True)
                self.assertEqual(result["reason"], "sandbox_not_as_requested")

    def test_full_access_still_requires_permission_evidence_and_no_named_profile(self) -> None:
        self.assertEqual(engines.permission_violation("codex", self.env, THREAD, FULL,
                                                      allow_full_access=True)["missing"], "turn_context")
        self.append_turn(profile="custom")
        self.assertEqual(engines.permission_violation("codex", self.env, THREAD, FULL,
                                                      allow_full_access=True)["reason"], "named_permission_profile_active")

    def test_full_access_allows_narrower_turns_but_not_unknown_policies(self) -> None:
        for sandbox in ("read-only", "workspace-write", FULL):
            self.append_turn(sandbox)
        self.assertIsNone(engines.permission_violation("codex", self.env, THREAD, FULL, allow_full_access=True))
        self.append_turn("unknown")
        self.assertEqual(engines.permission_violation("codex", self.env, THREAD, FULL,
                                                      allow_full_access=True)["reason"], "sandbox_not_as_requested")

    def test_full_access_resume_still_requires_a_new_turn(self) -> None:
        self.append_turn()
        checkpoint = engines.codex_permission_checkpoint(self.env, THREAD)
        kwargs = {"allow_full_access": True, "permission_checkpoint": checkpoint, "resume_session_id": THREAD}
        self.assertEqual(engines.permission_violation("codex", self.env, THREAD, FULL, **kwargs)["missing"],
                         "current_turn_context")
        self.append_turn()
        self.assertIsNone(engines.permission_violation("codex", self.env, THREAD, FULL, **kwargs))


def recording_codex(tmp: Path) -> Path:
    """No network: record the actual argv and append the sandbox to a vendor-shaped rollout."""
    script = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "login":
    print("Logged in using ChatGPT")
    sys.exit(0)
with open(MARKER, "a") as f:
    f.write(json.dumps(args) + "\n")
directory = Path(os.environ["CODEX_HOME"]) / "sessions/2026/10/20"
directory.mkdir(parents=True, exist_ok=True)
with (directory / ("rollout-test-" + THREAD + ".jsonl")).open("a") as f:
    if "resume" not in args:
        f.write(json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n")
    f.write(json.dumps({"type": "turn_context", "payload": {
        "sandbox_policy": {"type": args[args.index("--sandbox") + 1]},
        "active_permission_profile": None}}) + "\n")
for event in ({"type": "thread.started", "thread_id": THREAD},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
              {"type": "turn.completed"}):
    print(json.dumps(event))
'''
    path = tmp / "codex"
    path.write_text(f"#!{sys.executable}\nTHREAD={THREAD!r}\nMARKER={str(tmp / 'calls.jsonl')!r}\n" + script)
    path.chmod(0o755)
    return path


@needs_clean_cli_config
class FullAccessRoundTrip(RelayTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.env = base_env(isolated_env(self.tmp, {"CLI_RELAY_CODEX_BIN": str(recording_codex(self.tmp))}))

    def submit_codex(self, key="full-access", options=None, env=None, expect=0):
        args = ["submit", "--key", key, "--engine", "codex", "--cwd", str(self.work), "--prompt", "hi"]
        for key, value in (OPT_IN if options is None else options).items():
            args += ["--option", f"{key}={json.dumps(value)}"]
        return self.cli(*args, env=env or self.env, expect=expect)

    def assert_choice(self, record, sandbox=FULL, full_access=True):
        self.assertEqual(record["sandbox"], sandbox)
        self.assertIs(record["full_access"], full_access)

    def test_submit_rejects_default_false_and_non_bool_without_creating_a_task(self) -> None:
        for options in ({"sandbox": FULL}, {**OPT_IN, "allow_full_access": False},
                        {**OPT_IN, "allow_full_access": "true"}):
            with self.subTest(options=options):
                rejected = self.submit_codex(options=options, expect=2)
                self.assertEqual(rejected["error"], "usage")
                self.assertIn("allow_full_access", rejected["message"])
        self.assertEqual(db_rows(self.home, "SELECT task_id FROM tasks"), [])
        self.assertFalse((self.tmp / "calls.jsonl").exists())

    def test_opt_in_is_stored_and_visible_in_receipt_status_events_and_notification(self) -> None:
        sub = self.submit_codex()
        self.assert_choice(sub)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded", done)
        status = self.status(sub["task_id"])
        self.assert_choice(status["task"])
        self.assertEqual(status["task"]["options"], OPT_IN)
        self.assert_choice(status["events"][0]["body"])
        events = self.cli("events", "--consumer", "bot")["events"]
        self.assert_choice(events[0]["body"])
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        self.assertEqual(stored["options"], OPT_IN)
        self.assertEqual(sub["payload_sha256"], payload_hash(stored))
        duplicate = self.submit_codex()
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["task_id"], sub["task_id"])
        self.assertEqual(duplicate["payload_sha256"], sub["payload_sha256"])
        self.assert_choice(duplicate)
        calls = [json.loads(line) for line in (self.tmp / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("--sandbox") + 1], FULL)

        (self.home / "notifier.json").write_text(json.dumps({"source_id": "test-host", "default_route": "bot",
            "routes": {"bot": {"consumer": "bot", "chat": "test chat",
                               "adapter": {"type": "command", "argv": ["/usr/bin/false"]}}}}))
        with patch("cli_relay.notify.send", return_value={"outcome": "handed_off"}) as send:
            notify.flush(self.home, env=self.env)
        send.assert_called_once()
        notification = send.call_args.args[1]
        self.assert_choice(notification["cli_relay"]["events"][0])
        self.assertIn("full_access=true", notification["summary"])

    def test_opt_in_alone_is_reported_as_disabled_and_is_part_of_the_payload_hash(self) -> None:
        sub = self.submit_codex(options={"allow_full_access": True})
        self.assert_choice(sub, "read-only", False)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(done["task"]["options"], {"allow_full_access": True})
        self.assert_choice(done["task"], "read-only", False)
        self.assert_choice(done["events"][0]["body"], "read-only", False)
        conflict = self.submit_codex(options={}, expect=3)
        self.assertEqual(conflict["error"], "conflict")
        self.assertNotEqual(conflict["detail"]["submitted_payload_sha256"], sub["payload_sha256"])

    def test_default_receipt_status_and_event_report_read_only(self) -> None:
        sub = self.submit_codex(options={})
        self.assert_choice(sub, "read-only", False)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_choice(done["task"], "read-only", False)
        self.assert_choice(done["events"][0]["body"], "read-only", False)

    def test_opt_in_survives_exact_resume_and_keeper_crash(self) -> None:
        sub = self.submit_codex()
        self.assertEqual(self.cli("wait", sub["task_id"], "--timeout", "30")["task"]["state"], "succeeded")
        resumed = self.cli("resume", sub["task_id"], "--prompt", "second",
                           env={**self.env, "CLI_RELAY_TEST_CRASH": "after_exit_before_commit"})
        self.assert_choice(resumed)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assertEqual(done["attempts"][-1]["resume_session_id"], THREAD)
        self.assert_choice(done["events"][-1]["body"])
        self.assertEqual(done["events"][-1]["body"]["source"], "reconcile")
        calls = [json.loads(line) for line in (self.tmp / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][:4], ["exec", "--sandbox", FULL, "resume"])
        self.assertIn(f'sandbox_mode="{FULL}"', calls[1])

    def test_opt_in_survives_initial_keeper_crash(self) -> None:
        sub = self.submit_codex(env={**self.env, "CLI_RELAY_TEST_CRASH": "after_exit_before_commit"})
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assert_choice(done["events"][0]["body"])
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")

    def test_opt_in_does_not_bypass_config_layer_permission_checks(self) -> None:
        config = Path(self.env["CODEX_HOME"]) / "config.toml"
        config.write_text('default_permissions = "custom"\n')
        sub = self.submit_codex()
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["preflight"]["reason"], "config_not_subscription")
        self.assert_choice(done["events"][0]["body"])
        self.assertFalse((self.tmp / "calls.jsonl").exists())

    def test_opt_in_does_not_bypass_subscription_billing_checks(self) -> None:
        sub = self.submit_codex(env={**self.env, "OPENAI_API_KEY": "test-value"})
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["preflight"]["reason"], "billing_env_present")
        self.assertFalse((self.tmp / "calls.jsonl").exists())

    def test_doctor_explains_opt_in_default_and_risk(self) -> None:
        doctor = self.cli("doctor", "--engine", "codex", "--cwd", str(self.work), env=self.env)
        policy = doctor["codex_sandbox_policy"]
        self.assertEqual(policy["default_sandbox"], "read-only")
        self.assertIs(policy["full_access_default"], False)
        self.assertIn("--option allow_full_access=true", policy["full_access_opt_in"])
        self.assertIn('sandbox="danger-full-access"', policy["full_access_opt_in"])
        self.assertIn("disables the Codex sandbox", policy["risk"])
        self.assertIn("harmless", policy["allow_full_access_without_sandbox"])


if __name__ == "__main__":
    unittest.main()

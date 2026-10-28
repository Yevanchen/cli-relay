"""Issue #15: Codex can run outside Git without changing the requested sandbox."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config, wait_until
from test_terminal_observations import notification

from cli_relay import engines, procs
from cli_relay.errors import UsageError
from cli_relay.ledger import payload_hash

FLAG = "--skip-git-repo-check"
DECISION = "codex_git_repo_check"
THREAD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TRUST_ERROR = "Not inside a trusted directory and --skip-git-repo-check was not specified."


def init_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True,
                   env=base_env(), timeout=5)


class GitRepoCheckArgv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cli-relay-git-check-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work dir"
        self.work.mkdir()

    def build(self, options=None, resume=None, cwd=None, engine="codex"):
        return engines.build(engine, payload={"cwd": str(cwd or self.work),
                                               "options": {} if options is None else options},
                             attempt={"resume_session_id": resume, "native_session_id": THREAD,
                                      "attempt_id": "a-test"},
                             run_dir=self.work, fixture_state_dir=str(self.work),
                             env={"CLI_RELAY_CODEX_BIN": "/bin/echo"})

    def assert_decision(self, spec, expected):
        self.assertEqual(spec.evidence, {DECISION: expected})
        self.assertEqual(spec.argv[spec.argv.index("--sandbox") + 1], "read-only")

    def test_unset_in_non_git_directory_automatically_skips_on_first_run_and_resume(self):
        options = {}
        for resume in (None, THREAD):
            with self.subTest(resume=resume):
                spec = self.build(options, resume)
                self.assertIn(FLAG, spec.argv)
                self.assertEqual(spec.argv.count(FLAG), 1)
                self.assert_decision(spec, "auto_skipped")
        self.assertEqual(options, {}, "auto detection must not add an option to the payload")

    def test_unset_in_git_work_tree_keeps_original_argv(self):
        init_repo(self.work)
        for resume in (None, THREAD):
            with self.subTest(resume=resume):
                spec = self.build(resume=resume)
                self.assertNotIn(FLAG, spec.argv)
                self.assertEqual(spec.argv, self.build({"skip_git_repo_check": False}, resume).argv)
                self.assert_decision(spec, "enforced")

    def test_subdirectory_of_git_work_tree_does_not_skip(self):
        init_repo(self.work)
        nested = self.work / "nested" / "job dir"
        nested.mkdir(parents=True)
        for resume in (None, THREAD):
            with self.subTest(resume=resume):
                spec = self.build(resume=resume, cwd=nested)
                self.assertNotIn(FLAG, spec.argv)
                self.assert_decision(spec, "enforced")

    def test_explicit_true_skips_in_git_work_tree_without_probing(self):
        init_repo(self.work)
        with patch("cli_relay.git_state.subprocess.run") as probe:
            for resume in (None, THREAD):
                with self.subTest(resume=resume):
                    spec = self.build({"skip_git_repo_check": True}, resume)
                    self.assertIn(FLAG, spec.argv)
                    self.assert_decision(spec, "explicit_skip")
        probe.assert_not_called()

    def test_explicit_false_stays_strict_in_non_git_directory_without_probing(self):
        with patch("cli_relay.git_state.subprocess.run") as probe:
            for resume in (None, THREAD):
                with self.subTest(resume=resume):
                    spec = self.build({"skip_git_repo_check": False}, resume)
                    self.assertNotIn(FLAG, spec.argv)
                    self.assert_decision(spec, "enforced")
        probe.assert_not_called()

    def test_only_booleans_are_accepted_when_option_is_set(self):
        for value in ("yes", "true", None, 0, 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(UsageError, "wrong type"):
                self.build({"skip_git_repo_check": value})
        self.assertEqual(engines.validate_options("codex", {}), {})

    def test_missing_git_and_timeout_fall_back_to_auto_skip(self):
        for failure in (FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired("git", 2)):
            with self.subTest(failure=type(failure).__name__), \
                    patch("cli_relay.git_state.subprocess.run", side_effect=failure):
                spec = self.build()
                self.assertIn(FLAG, spec.argv)
                self.assert_decision(spec, "auto_skipped")

    def test_nonzero_git_exit_and_non_work_tree_fall_back_to_auto_skip(self):
        for result in (subprocess.CompletedProcess([], 128, stdout=b""),
                       subprocess.CompletedProcess([], 0, stdout=b"false\n")):
            with self.subTest(result=result), \
                    patch("cli_relay.git_state.subprocess.run", return_value=result):
                spec = self.build()
                self.assertIn(FLAG, spec.argv)
                self.assert_decision(spec, "auto_skipped")

    def test_auto_probe_is_read_only_bounded_argv_with_no_git_routing(self):
        with patch.dict("os.environ", {"GIT_DIR": "/other/repo", "GIT_WORK_TREE": "/other"}), \
                patch("cli_relay.git_state.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0, stdout=b"true\n")) as probe:
            spec = self.build()
        self.assertNotIn(FLAG, spec.argv)
        probe.assert_called_once()
        call = probe.call_args
        self.assertEqual(call.args[0], ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                        "-C", str(self.work), "rev-parse", "--is-inside-work-tree"])
        self.assertFalse(call.kwargs.get("shell", False))
        self.assertGreater(call.kwargs["timeout"], 0)
        self.assertLessEqual(call.kwargs["timeout"], 5)
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(any(key.startswith("GIT_") for key in call.kwargs["env"]))

    def test_fixture_does_not_probe_or_report_codex_decision(self):
        with patch("cli_relay.git_state.subprocess.run") as probe:
            spec = self.build(engine="fixture")
        probe.assert_not_called()
        self.assertEqual(spec.evidence, {})
        self.assertNotIn(FLAG, spec.argv)


def fixture_codex(tmp):
    """Local stand-in: enforce the flag and emit a subscription rollout, never call a model."""
    script = r'''
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
if args[0] == "login":
    print("Logged in using ChatGPT")
    sys.exit(0)
prompt = sys.stdin.read()
with open(CALLS, "a") as f:
    f.write(json.dumps(args) + "\n")
if "--skip-git-repo-check" not in args or prompt == "reject":
    print(TRUST_ERROR, file=sys.stderr)
    print("private stderr detail", file=sys.stderr)
    sys.exit(1)
if prompt == "other-error":
    print("unrelated error", file=sys.stderr)
    sys.exit(1)
if prompt == "success-with-stderr":
    print(TRUST_ERROR, file=sys.stderr)
thread = str(uuid.uuid4())
directory = Path(os.environ["CODEX_HOME"]) / "sessions/2026/10/28"
directory.mkdir(parents=True, exist_ok=True)
with (directory / ("rollout-test-" + thread + ".jsonl")).open("w") as f:
    f.write(json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n")
    f.write(json.dumps({"type": "turn_context", "payload": {
        "sandbox_policy": {"type": "read-only"}, "active_permission_profile": None}}) + "\n")
for event in ({"type": "thread.started", "thread_id": thread},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
              {"type": "turn.completed"}):
    print(json.dumps(event))
'''
    path = tmp / "codex"
    path.write_text(f"#!{sys.executable}\nCALLS={str(tmp / 'calls.jsonl')!r}\n"
                    f"TRUST_ERROR={TRUST_ERROR!r}\n" + script)
    path.chmod(0o755)
    return path


@needs_clean_cli_config
class GitRepoCheckRoundTrip(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.env = base_env(isolated_env(self.tmp, {"CLI_RELAY_CODEX_BIN": str(fixture_codex(self.tmp))}))

    def submit_codex(self, key="job", options=None, prompt="do it", crash=False, expect=0):
        args = ["submit", "--key", key, "--engine", "codex", "--cwd", str(self.work), "--prompt", prompt]
        for name, value in (options or {}).items():
            args += ["--option", f"{name}={json.dumps(value)}"]
        env = dict(self.env)
        if crash:
            env["CLI_RELAY_TEST_CRASH"] = "after_exit_before_commit"
        return self.cli(*args, env=env, expect=expect)

    def finish(self, sub, crash=False):
        if crash:
            wait_until(lambda: (lambda r: r["keeper_pid"] and procs.same_process_alive(
                r["keeper_pid"], r["keeper_token"]) is False)(self.attempt_row(sub["task_id"])))
        return self.cli("wait", sub["task_id"], "--timeout", "30", env=self.env)

    def assert_observations(self, done, decision, hint=False):
        delivered = notification(done)
        for record in (done["attempts"][0]["evidence"], done["events"][0]["body"],
                       delivered["cli_relay"]["events"][0]):
            self.assertEqual(record[DECISION], decision)
            warnings = record.get("warnings", [])
            matches = [w for w in warnings if w["code"] == "codex_trusted_directory"]
            self.assertEqual(len(matches), int(hint))
            if hint:
                self.assertIn("skip_git_repo_check=true", matches[0]["hint"])
        if hint:
            self.assertIn("skip_git_repo_check=true", delivered["summary"])
        self.assertNotIn("private stderr detail", json.dumps(done))

    def test_auto_decision_is_durable_and_does_not_change_payload_or_hash(self):
        sub = self.submit_codex()
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assert_observations(done, "auto_skipped")
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        expected = {"engine": "codex", "cwd": str(self.work.resolve()), "prompt": "do it", "options": {}}
        self.assertEqual(stored, expected)
        self.assertEqual(sub["payload_sha256"], payload_hash(expected))
        again = self.submit_codex()
        self.assertEqual((again["status"], again["payload_sha256"]), ("duplicate", sub["payload_sha256"]))
        argv = json.loads((self.tmp / "calls.jsonl").read_text())
        self.assertIn(FLAG, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")

    def test_strict_failure_reports_explicit_option_hint(self):
        done = self.finish(self.submit_codex(options={"skip_git_repo_check": False}))
        self.assertEqual(done["task"]["state"], "failed", done)
        self.assert_observations(done, "enforced", hint=True)

    def test_trusted_directory_failure_reports_hint_even_with_explicit_skip(self):
        done = self.finish(self.submit_codex(options={"skip_git_repo_check": True}, prompt="reject"))
        self.assertEqual(done["task"]["state"], "failed", done)
        self.assert_observations(done, "explicit_skip", hint=True)

    def test_success_and_unrelated_failure_do_not_add_trusted_directory_warning(self):
        for prompt, outcome in (("success-with-stderr", "succeeded"), ("other-error", "failed")):
            with self.subTest(prompt=prompt):
                done = self.finish(self.submit_codex(key=prompt, prompt=prompt))
                self.assertEqual(done["task"]["state"], outcome, done)
                self.assert_observations(done, "auto_skipped")
                self.assertNotIn("warnings", done["events"][0]["body"])

    def test_auto_decision_survives_keeper_crash(self):
        done = self.finish(self.submit_codex(crash=True), crash=True)
        self.assertEqual(done["task"]["state"], "succeeded", done)
        self.assert_observations(done, "auto_skipped")
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")

    def test_failure_hint_survives_keeper_crash_and_preserves_git_warning(self):
        init_repo(self.work)
        (self.work / "untracked.txt").write_text("change\n")
        done = self.finish(self.submit_codex(options={"skip_git_repo_check": True}, prompt="reject", crash=True),
                           crash=True)
        self.assertEqual(done["task"]["state"], "failed", done)
        self.assert_observations(done, "explicit_skip", hint=True)
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")
        self.assertIn({"code": "uncommitted_changes"}, done["attempts"][0]["evidence"]["warnings"])

    def test_invalid_option_is_rejected_before_creating_a_task(self):
        rejected = self.submit_codex(options={"skip_git_repo_check": "yes"}, expect=2)
        self.assertEqual(rejected["error"], "usage")
        self.assertEqual(db_rows(self.home, "SELECT task_id FROM tasks"), [])
        self.assertFalse((self.tmp / "calls.jsonl").exists())

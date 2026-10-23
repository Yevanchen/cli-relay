"""Issues #11/#12: terminal observations survive delivery without changing request identity."""

from __future__ import annotations

import json
import subprocess
import sys

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config, wait_until

from cli_relay import git_state, notify, procs
from cli_relay.ledger import payload_hash


def notification(done):
    event = done["events"][-1]
    ref = notify._event_ref({**event, "task_id": done["task"]["task_id"],
                             "request_key": done["task"]["key"], "body_json": json.dumps(event["body"])})
    return notify.build_payload({"source_id": "test-host"},
                                {"name": "test", "chat": "test", "consumer": "test",
                                 "adapter": {"job": "test"}}, "test-ledger", [ref], reminder=False)


class GitObservations(RelayTestCase):
    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                               "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", *args],
                              cwd=self.work, env=base_env(), check=True, capture_output=True,
                              text=True, timeout=5).stdout.strip()

    def repo(self):
        self.git("init", "-q")
        (self.work / "file.txt").write_text("before\n")
        self.commit()
        return self.git("rev-parse", "HEAD")

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "test change")

    def finish(self, sub):
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        return done

    def assert_observation(self, done, before, after, dirty):
        expected = {"head_before": before, "head_after": after, "dirty": dirty}
        self.assertEqual(done["attempts"][-1]["evidence"]["git"], expected)
        event = done["events"][-1]["body"]
        self.assertEqual(event["git"], expected)
        payload = notification(done)
        self.assertEqual(payload["cli_relay"]["events"][0]["git"], expected)
        for record in (done["attempts"][-1]["evidence"], event, payload["cli_relay"]["events"][0]):
            if dirty:
                self.assertIn({"code": "uncommitted_changes"}, record["warnings"])
            else:
                self.assertNotIn("warnings", record)

    def test_fixture_leaves_dirty_tree_and_request_hash_unchanged(self):
        before = self.repo()
        self.effects = self.work / "untracked.jsonl"
        sub = self.submit("dirty", duration=0)
        done = self.finish(sub)
        self.assert_observation(done, before, before, True)
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        expected = {"engine": "fixture", "cwd": str(self.work.resolve()), "prompt": "do the thing",
                    "options": {"duration_s": 0, "side_effect_file": str(self.effects)}}
        self.assertEqual(stored, expected)
        self.assertEqual(sub["payload_sha256"], payload_hash(expected))
        again = self.submit("dirty", duration=0)
        self.assertEqual((again["status"], again["payload_sha256"]), ("duplicate", sub["payload_sha256"]))

    def test_clean_commit_during_fixture_records_both_heads(self):
        before = self.repo()
        sub = self.submit("commit", duration=4)
        wait_until(lambda: self.attempt_row(sub["task_id"])["cli_pid"])
        (self.work / "file.txt").write_text("after\n")
        self.commit()  # Only this disposable test repo; the relay never commits.
        after = self.git("rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        self.assert_observation(self.finish(sub), before, after, False)

    def test_non_git_directory_has_no_new_fields_or_hash_changes(self):
        sub = self.submit("plain", duration=0)
        done = self.finish(sub)
        for record in (done["attempts"][0]["evidence"], done["events"][0]["body"],
                       notification(done)["cli_relay"]["events"][0]):
            self.assertNotIn("git", record)
            self.assertNotIn("warnings", record)
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        self.assertEqual(sub["payload_sha256"], payload_hash(stored))
        self.assertEqual(set(stored), {"engine", "cwd", "prompt", "options"})

    def test_git_probes_do_not_write_index_and_include_staged_and_unstaged_changes(self):
        before = self.repo()
        for staged in (False, True):
            with self.subTest(staged=staged):
                (self.work / "file.txt").write_text(f"changed {staged}\n")
                if staged:
                    self.git("add", "file.txt")
                index = self.work / ".git" / "index"
                saved = (index.read_bytes(), index.stat().st_mtime_ns)
                self.assertEqual(git_state.snapshot(str(self.work)), {"head": before, "dirty": True})
                self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), saved)
                self.assertFalse((self.work / ".git" / "index.lock").exists())

    def test_unborn_repo_reports_null_heads_and_uncommitted_changes(self):
        self.git("init", "-q")
        self.effects = self.work / "new.jsonl"
        self.assert_observation(self.finish(self.submit("unborn", duration=0)), None, None, True)

    def test_dirty_warning_does_not_change_failure(self):
        head = self.repo()
        self.effects = self.work / "new.jsonl"
        sub = self.submit("failure", duration=0, extra_options=["exit_code=1"])
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "failed")
        self.assert_observation(done, head, head, True)

    def test_reconciliation_uses_saved_end_snapshot_after_later_edits(self):
        before = self.repo()
        sub = self.submit("crash", duration=0,
                          env=base_env({"CLI_RELAY_TEST_CRASH": "after_exit_before_commit"}))
        wait_until(lambda: (lambda r: r["keeper_pid"] and procs.same_process_alive(
            r["keeper_pid"], r["keeper_token"]) is False)(self.attempt_row(sub["task_id"])))
        (self.work / "file.txt").write_text("edit after task ended\n")
        done = self.finish(sub)
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")
        self.assert_observation(done, before, before, False)

    def test_keeper_lost_before_exit_preserves_before_head(self):
        head = self.repo()
        self.effects = self.work / "new.jsonl"
        sub = self.submit("no-exit", duration=0.2, env=base_env({"CLI_RELAY_TEST_CRASH": "after_record"}))
        done = self.finish(sub)
        self.assertFalse(done["attempts"][0]["exit"]["known"])
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")
        self.assert_observation(done, head, head, True)


@needs_clean_cli_config
class DenialObservations(RelayTestCase):
    def run_claude(self, *, ok=True, marker=None, crash=False):
        result = "done" if marker is None else "done\nCLI-RELAY-STATUS: " + marker
        output = {"type": "result", "subtype": "success" if ok else "error_during_execution",
                  "is_error": not ok, "result": result,
                  "permission_denials": [{"tool_name": "Monitor", "tool_input": {"detail": "not exported"}},
                                        {"tool_name": "Monitor"}]}
        script = (f"#!{sys.executable}\nimport json, sys\n"
                  "if sys.argv[1] == 'auth':\n"
                  "    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))\n"
                  "    sys.exit(0)\n"
                  "print(json.dumps({'type': 'system', 'subtype': 'init', 'apiKeySource': 'none', "
                  "'permissionMode': 'dontAsk'}))\n"
                  f"print(json.dumps({output!r}))\nsys.exit({0 if ok else 1})\n")
        path = self.tmp / "claude"
        path.write_text(script)
        path.chmod(0o755)
        extra = {"CLI_RELAY_CLAUDE_BIN": str(path)}
        if crash:
            extra["CLI_RELAY_TEST_CRASH"] = "after_exit_before_commit"
        env = base_env(isolated_env(self.tmp, extra))
        options = ["--option", "require_completion_marker=true"] if marker is not None else []
        sub = self.cli("submit", "--key", f"denied-{ok}-{marker}-{crash}", "--engine", "claude",
                       "--cwd", str(self.work), "--prompt", "do it", *options, env=env)
        return self.cli("wait", sub["task_id"], "--timeout", "30", env=env)

    def assert_warning(self, done, outcome):
        self.assertEqual(done["task"]["state"], outcome)
        expected = [{"code": "permission_denials", "tools": ["Monitor"]}]
        self.assertEqual(done["attempts"][0]["evidence"]["warnings"], expected)
        self.assertEqual(done["events"][0]["body"]["warnings"], expected)
        payload = notification(done)
        self.assertEqual(payload["status"], outcome)
        self.assertEqual(payload["cli_relay"]["events"][0]["warnings"], expected)
        self.assertIn("warnings=permission_denials", payload["summary"])
        self.assertIn("denied_tools=Monitor", payload["summary"])
        self.assertNotIn("not exported", json.dumps(done))
        self.assertEqual(done["task"]["allowed_tools"], [])

    def test_native_success_with_denial_is_success_with_warning(self):
        self.assert_warning(self.run_claude(), "succeeded")

    def test_native_failure_with_denial_still_needs_input(self):
        self.assert_warning(self.run_claude(ok=False), "needs_input")

    def test_marker_gate_still_decides_native_success_with_denial(self):
        for marker, outcome in (("complete", "succeeded"), ("incomplete", "needs_input"),
                                ("missing", "needs_input")):
            with self.subTest(marker=marker):
                self.assert_warning(self.run_claude(marker=marker), outcome)

    def test_success_with_denial_survives_keeper_crash(self):
        done = self.run_claude(crash=True)
        self.assert_warning(done, "succeeded")
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")

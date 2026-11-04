"""Issue #16: business self-reports never redefine the process outcome or request identity."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import RelayTestCase, base_env, db_rows, wait_until
from test_terminal_observations import notification

from cli_relay import business_status, engines, notify, procs
from cli_relay.errors import UsageError
from cli_relay.ledger import Ledger, payload_hash


class BusinessReports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cli-relay-business-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        (self.work / "out").mkdir(parents=True)
        self.result = self.work / "out/result.json"

    def evaluate(self, text=None, *, engine="codex", outcome="succeeded", options=None, denials=None):
        return business_status.evaluate(engine, {"cwd": str(self.work), "options": options or {}}, outcome,
                                        {"ok": True, "result": text, "permission_denials": denials or []})

    def test_status_aliases_share_one_importable_mapping(self):
        for word, expected in (("ok", "ok"), ("done", "ok"), ("partial", "partial"),
                               ("stopped_for_approval", "stopped_for_approval"),
                               ("needs_approval", "stopped_for_approval"), ("failed", "failed"),
                               ("fail", "failed"), ("surprise", "unreported"), ("未知", "unreported")):
            with self.subTest(word=word):
                self.assertEqual(business_status.normalize_status(word), expected)
                report = self.evaluate(f"TASK_DONE status={word}\n\n ")
                self.assertEqual((report["business_status"], report["business_status_source"]),
                                 (expected, "status_line"))
                if expected == "unreported":
                    self.assertEqual(report["warnings"], [{"code": "business_status_unknown", "source": "status_line"}])
                else:
                    self.assertNotIn("warnings", report)

    def test_only_a_status_token_on_the_last_nonempty_line_counts(self):
        for text in (None, "", " \n ", "本次失败", "The task failed (cgWindowNotFound).", "done",
                     "Quoted: status=failed\nThe task finished.", "status=ok\nOne more thing.\n\n",
                     "```\nTASK_DONE status=ok\n```", "not_status=ok", "status=failed-ish", "ok=1 fail=2"):
            with self.subTest(text=text):
                report = self.evaluate(text)
                self.assertEqual((report["business_status"], report["business_status_source"]),
                                 ("unreported", "none"))
                self.assertNotIn("business_status_counts", report)
        report = self.evaluate("status=failed\r\nTASK_DONE status=ok\r\n \r\n")
        self.assertEqual(report["business_status"], "ok")

    def test_optional_counters_are_integers_from_the_status_line(self):
        report = self.evaluate("earlier ok=200\nTASK_DONE status=partial ok=9 fail=4 skip=04\n")
        self.assertEqual(report["business_status_counts"], {"ok": 9, "fail": 4, "skip": 4})
        self.assertEqual(self.evaluate("status=ok ok=0")["business_status_counts"], {"ok": 0})
        self.assertNotIn("business_status_counts", self.evaluate("status=ok ok=-1 fail=no skip=1.5"))

    def test_result_file_maps_status_and_ignores_other_contents(self):
        for word, expected in (("done", "ok"), ("partial", "partial"), ("fail", "failed"),
                               ("needs_approval", "stopped_for_approval"), ("mystery", "unreported")):
            with self.subTest(word=word):
                self.result.write_text(json.dumps({"status": word, "ok": 9, "detail": "not exported"}))
                report = self.evaluate(options={"result_file": "out/result.json"})
                self.assertEqual((report["business_status"], report["business_status_source"]), (expected, "result_file"))
                self.assertNotIn("business_status_counts", report)
                self.assertNotIn("not exported", json.dumps(report))
                if expected == "unreported":
                    self.assertEqual(report["warnings"], [{"code": "business_status_unknown", "source": "result_file"}])

    def test_result_file_is_explicit_and_a_missing_file_reports_nothing(self):
        report = self.evaluate(options={"result_file": "out/result.json"})
        self.assertEqual(report["business_status"], "unreported")
        self.assertNotIn("warnings", report)
        self.result.write_text('{"status":"failed"}')
        self.assertEqual(self.evaluate()["business_status"], "unreported")

    def test_malformed_result_files_add_warnings_without_claiming_status(self):
        for data, reason in ((b"{", "invalid_json"), (b"\xff", "invalid_json"), (b"[]", "invalid_status"),
                             (b"{}", "invalid_status"), (b'{"status":1}', "invalid_status"),
                             (b'{"status":null}', "invalid_status")):
            with self.subTest(data=data):
                self.result.write_bytes(data)
                report = self.evaluate("status=ok", options={"result_file": "out/result.json"})
                self.assertEqual(report["business_status"], "ok")
                self.assertEqual(report["warnings"], [{"code": "business_status_result_file_invalid", "reason": reason}])

    def test_result_file_size_and_read_errors_are_bounded_warnings(self):
        self.result.write_bytes(b" " * (business_status.MAX_RESULT_FILE_BYTES + 1))
        with patch.object(business_status.os, "open", wraps=business_status.os.open) as opened:
            report = self.evaluate(options={"result_file": "out/result.json"})
        self.assertEqual(report["warnings"][0]["reason"], "too_large")
        self.assertTrue(opened.call_args.args[1] & business_status.os.O_NONBLOCK)
        self.result.write_text('{"status":"done"}')
        with patch.object(business_status.os, "open", side_effect=PermissionError):
            report = self.evaluate(options={"result_file": "out/result.json"})
        self.assertEqual(report["warnings"], [{"code": "business_status_result_file_invalid", "reason": "unreadable"}])

    def test_conflicts_choose_the_more_severe_status_and_preserve_line_counters(self):
        words = ("ok", "partial", "stopped_for_approval", "failed")
        for i, line in enumerate(words):
            for j, file in enumerate(words):
                with self.subTest(line=line, file=file):
                    self.result.write_text(json.dumps({"status": file}))
                    report = self.evaluate(f"status={line} ok=1", options={"result_file": "out/result.json"})
                    self.assertEqual(report["business_status"], words[max(i, j)])
                    self.assertEqual(report["business_status_source"], "status_line" if i >= j else "result_file")
                    self.assertEqual(report["business_status_counts"], {"ok": 1})
                    self.assertEqual([w["code"] for w in report.get("warnings", [])],
                                     ["business_status_conflict"] if i != j else [])
        self.result.write_text('{"status":"done"}')
        self.assertNotIn("warnings", self.evaluate("status=ok", options={"result_file": "out/result.json"}))
        report = self.evaluate("status=mystery", options={"result_file": "out/result.json"})
        self.assertEqual(report["business_status"], "ok", "a known report wins over an unknown word")
        self.assertIn("business_status_conflict", [w["code"] for w in report["warnings"]])

    def test_non_success_outcomes_are_failed_except_for_an_approval_report(self):
        for outcome in ("failed", "blocked", "needs_input", "cancelled", "unknown"):
            for text in (None, "status=ok", "status=partial", "status=failed", "status=mystery"):
                with self.subTest(outcome=outcome, text=text):
                    report = self.evaluate(text, outcome=outcome, engine="claude", denials=["Read"])
                    self.assertEqual((report["business_status"], report["business_status_source"]), ("failed", "outcome"))
            report = self.evaluate("status=needs_approval", outcome=outcome)
            self.assertEqual((report["business_status"], report["business_status_source"]),
                             ("stopped_for_approval", "status_line"))
        self.result.write_text('{"status":"stopped_for_approval"}')
        report = self.evaluate(outcome="failed", options={"result_file": "out/result.json"})
        self.assertEqual((report["business_status"], report["business_status_source"]), ("stopped_for_approval", "result_file"))

    def test_claude_denials_count_entries_and_downgrade_ok_or_no_status(self):
        for text in (None, "status=ok", "status=done"):
            with self.subTest(text=text):
                report = self.evaluate(text, engine="claude", denials=["Bash", "Read", "Bash"])
                self.assertEqual(report["denials"], {"count": 3, "tools": ["Bash", "Read"], "available": True})
                self.assertEqual((report["business_status"], report["business_status_source"]), ("partial", "denials"))
        for word in ("partial", "stopped_for_approval", "failed"):
            report = self.evaluate(f"status={word}", engine="claude", denials=["Bash"])
            self.assertEqual((report["business_status"], report["business_status_source"]), (word, "status_line"))

    def test_codex_denials_are_unavailable_and_claude_zero_is_not_a_downgrade(self):
        for engine in ("codex", "claude"):
            report = self.evaluate(engine=engine)
            self.assertEqual(report["business_status"], "unreported")
            self.assertEqual(report["denials"], {"count": 0 if engine == "claude" else None,
                                                "tools": [], "available": engine == "claude"})
        report = business_status.evaluate("claude", {}, "blocked", None)
        self.assertEqual(report["denials"], {"count": None, "tools": [], "available": False})

    def test_claude_and_codex_use_their_own_final_result(self):
        stdout = self.work / "stdout.jsonl"
        stdout.write_text(json.dumps({"type": "result", "subtype": "success", "result": "status=ok",
                                      "permission_denials": [{"tool_name": "Read"}, {"tool_name": "Read"}]}) + "\n")
        native = engines.parse_output("claude", stdout)["native"]
        report = business_status.evaluate("claude", {}, "succeeded", native)
        self.assertEqual((report["business_status"], report["denials"]["count"]), ("partial", 2))
        stdout.write_text("\n".join(json.dumps(e) for e in (
            {"type": "item.completed", "item": {"type": "agent_message", "text": "status=failed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "status=partial ok=9 fail=4 skip=4"}},
            {"type": "turn.completed"})))
        native = engines.parse_output("codex", stdout)["native"]
        report = business_status.evaluate("codex", {}, "succeeded", native)
        self.assertEqual(report["business_status"], "partial")
        self.assertEqual(report["denials"], {"count": None, "tools": [], "available": False})
        stdout.write_text('{"type":"turn.completed"}\n')
        last_message = self.work / "last_message.txt"
        last_message.write_text("status=stopped_for_approval")
        native = engines.parse_output("codex", stdout, last_message)["native"]
        self.assertEqual(business_status.evaluate("codex", {}, "succeeded", native)["business_status"], "stopped_for_approval")

    def test_result_file_validation_and_option_types_on_every_engine(self):
        for engine in engines.ENGINES:
            self.assertEqual(engines.validate_options(engine, {}, cwd=str(self.work)), {})
            self.assertEqual(engines.validate_options(engine, {"result_file": "./out/result.json",
                                                               "require_business_status": True}, cwd=str(self.work)),
                             {"result_file": "out/result.json", "require_business_status": True})
            for value in ("true", 1, 0, None, [], {}):
                with self.subTest(engine=engine, value=value), self.assertRaises(UsageError):
                    engines.validate_options(engine, {"require_business_status": value})
            for value in ("../result.json", "out/../result.json", str(self.result), ".", "", "out/*", "out\n", None, 1):
                with self.subTest(engine=engine, value=value), self.assertRaises(UsageError):
                    engines.validate_options(engine, {"result_file": value}, cwd=str(self.work))
        with self.assertRaises(UsageError):
            engines.validate_options("fixture", {"result_file": "out/result.json"})

    def test_result_file_symlink_is_rechecked_after_launch(self):
        outside = self.work.parent / "outside.json"
        outside.write_text('{"status":"failed"}')
        self.result.symlink_to(outside)
        with self.assertRaises(UsageError):
            engines.validate_options("codex", {"result_file": "out/result.json"}, cwd=str(self.work))
        report = self.evaluate("status=ok", options={"result_file": "out/result.json"})
        self.assertEqual(report["business_status"], "ok")
        self.assertEqual(report["warnings"], [{"code": "business_status_result_file_invalid", "reason": "unsafe_path"}])

    def test_reporting_options_do_not_change_engine_argv_or_permission_evidence(self):
        for engine in engines.ENGINES:
            for resume in (None, "old-session"):
                def build(options):
                    return engines.build(engine, payload={"cwd": str(self.work), "options": options},
                                         attempt={"native_session_id": "sid", "resume_session_id": resume, "attempt_id": "a-test"},
                                         run_dir=self.work, fixture_state_dir=str(self.work),
                                         env={"CLI_RELAY_CLAUDE_BIN": "/bin/echo", "CLI_RELAY_CODEX_BIN": "/bin/echo"})
                before = build({})
                after = build({"result_file": "out/result.json", "require_business_status": True})
                self.assertEqual(after.argv, before.argv)
                self.assertEqual(after.evidence, before.evidence)

    def test_notifier_summary_keeps_business_status_for_every_event_in_a_batch(self):
        refs = [{"event_id": f"e-{i}", "task_id": f"t-{i}", "attempt_no": 1, "outcome": "succeeded",
                 "result_excerpt": "x" * 500, "business_status": "partial", "business_status_source": "status_line",
                 "denials": {"count": None if i == 4 else i, "tools": [], "available": i != 4}}
                for i in range(5)]
        payload = notify.build_payload({"source_id": "test-host"},
                                       {"name": "test", "chat": "test", "consumer": "test", "adapter": {"job": "test"}},
                                       "test-ledger", refs, reminder=False)
        self.assertEqual(payload["status"], "multiple")
        for i in range(5):
            count = "null" if i == 4 else str(i)
            self.assertIn(f"t-{i}#1 business_status=partial denials={count}", payload["summary"])
        self.assertEqual(payload["cli_relay"]["events"], refs)


class BusinessStatusRoundTrip(RelayTestCase):
    def finish(self, text, *, key="business", options=None, env=None):
        opts = ["result=" + json.dumps(text)]
        opts += [f"{key}={json.dumps(value)}" for key, value in (options or {}).items()]
        sub = self.submit(key, duration=0, extra_options=opts, env=env)
        return sub, self.cli("wait", sub["task_id"], "--timeout", "30")

    def assert_fields(self, done, status, source, counts=None):
        delivered = notification(done)
        records = (done["task"], done["attempts"][0], done["attempts"][0]["evidence"],
                   done["events"][0]["body"], delivered["cli_relay"]["events"][0])
        for record in records:
            self.assertEqual((record["business_status"], record["business_status_source"]), (status, source))
            self.assertEqual(record["denials"], {"count": 0, "tools": [], "available": True})
            if counts is not None:
                self.assertEqual(record["business_status_counts"], counts)
        self.assertIn(f"business_status={status} denials=0", delivered["summary"])

    def test_status_and_notification_expose_business_failure_without_changing_success(self):
        _, done = self.finish("x" * 3000 + "\nTASK_DONE status=partial ok=9 fail=4 skip=4")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertNotIn("status=partial", done["events"][0]["body"]["result_excerpt"])
        self.assert_fields(done, "partial", "status_line", {"ok": 9, "fail": 4, "skip": 4})

    def test_result_file_conflict_is_durable_in_evidence_events_and_notifications(self):
        (self.work / "result.json").write_text('{"status":"failed"}')
        _, done = self.finish("TASK_DONE status=ok", options={"result_file": "result.json"})
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_fields(done, "failed", "result_file")
        for record in (done["attempts"][0]["evidence"], done["events"][0]["body"],
                       notification(done)["cli_relay"]["events"][0]):
            self.assertIn({"code": "business_status_conflict", "status_line": "ok", "result_file": "failed"}, record["warnings"])

    def test_missing_and_invalid_result_files_do_not_fail_the_task(self):
        for invalid in (False, True):
            if invalid:
                (self.work / "result.json").write_text("invalid json")
            _, done = self.finish("no report", key=f"file-{invalid}", options={"result_file": "result.json"})
            self.assertEqual(done["task"]["state"], "succeeded")
            self.assert_fields(done, "unreported", "none")
            self.assertEqual(bool(done["attempts"][0]["evidence"].get("warnings")), invalid)

    def test_require_business_status_warns_without_changing_outcome(self):
        _, done = self.finish("本次失败", options={"require_business_status": True})
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_fields(done, "unreported", "none")
        self.assertEqual(done["attempts"][0]["evidence"]["warnings"], [{"code": "business_status_missing"}])

    def test_failed_outcome_keeps_reported_approval_stop(self):
        _, done = self.finish("TASK_DONE status=stopped_for_approval ok=0 fail=0 skip=4", options={"exit_code": 1})
        self.assertEqual(done["task"]["state"], "failed")
        self.assert_fields(done, "stopped_for_approval", "status_line", {"ok": 0, "fail": 0, "skip": 4})

    def test_result_file_can_report_business_status_alongside_the_exact_completion_marker(self):
        (self.work / "result.json").write_text('{"status":"partial"}')
        _, done = self.finish("CLI-RELAY-STATUS: complete",
                              options={"result_file": "result.json", "require_completion_marker": True})
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(done["events"][0]["body"]["completion_marker"], "complete")
        self.assert_fields(done, "partial", "result_file")

    def test_keeper_crash_reconciles_the_same_reports(self):
        (self.work / "result.json").write_text('{"status":"partial"}')
        sub = self.submit("crash", duration=0, extra_options=['result="status=ok"', 'result_file="result.json"'],
                          env=base_env({"CLI_RELAY_TEST_CRASH": "after_exit_before_commit"}))
        wait_until(lambda: (lambda r: r["keeper_pid"] and procs.same_process_alive(
            r["keeper_pid"], r["keeper_token"]) is False)(self.attempt_row(sub["task_id"])))
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")
        self.assert_fields(done, "partial", "result_file")

    def test_paths_outside_cwd_are_rejected_at_submit_before_creating_tasks(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.work / "link").symlink_to(outside, target_is_directory=True)
        for path in ("../result.json", "out/../result.json", str(outside / "result.json"), "link/result.json"):
            self.cli("submit", "--key", "invalid", "--engine", "fixture", "--cwd", str(self.work),
                     "--prompt", "work", "--option", "result_file=" + json.dumps(path), expect=2)
        self.assertEqual(db_rows(self.home, "SELECT COUNT(*) FROM tasks")[0][0], 0)

    def test_omitted_options_preserve_the_previous_payload_hash(self):
        sub = self.submit("hash", duration=0)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        expected = {"engine": "fixture", "cwd": str(self.work.resolve()), "prompt": "do the thing",
                    "options": {"duration_s": 0, "side_effect_file": str(self.effects)}}
        self.assertEqual(sub["payload_sha256"], payload_hash(expected))
        self.assertEqual(json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0][0]), expected)
        self.assert_fields(done, "unreported", "none")
        duplicate = self.submit("hash", duration=0)
        self.assertEqual((duplicate["status"], duplicate["payload_sha256"]), ("duplicate", sub["payload_sha256"]))

    def test_old_evidence_and_events_remain_readable_without_backfill(self):
        sub, done = self.finish("status=ok")
        body = done["events"][0]["body"]
        evidence = done["attempts"][0]["evidence"]
        for key in business_status.FIELDS:
            body.pop(key, None)
            evidence.pop(key, None)
        ledger = Ledger(self.home)
        with ledger.tx() as c:
            c.execute("UPDATE attempts SET evidence_json=?", (json.dumps(evidence),))
            c.execute("UPDATE events SET body_json=?", (json.dumps(body),))
        ledger.close()
        status = self.cli("status", sub["task_id"], "--no-reconcile")
        events = self.cli("events", "--consumer", "old", "--no-reconcile")["events"]
        self.assertEqual(status["task"]["state"], "succeeded")
        self.assertEqual(events[0]["body"], body)
        self.assertNotIn("business_status", status["task"])
        self.assertNotIn("business_status", notification(status)["cli_relay"]["events"][0])

    def test_unknown_and_cancelled_attempts_have_business_status_without_native_output(self):
        ledger = Ledger(self.home)
        self.addCleanup(ledger.close)
        payload = {"engine": "fixture", "cwd": str(self.work), "options": {}, "prompt": "p"}
        for kind in ("unknown", "cancelled"):
            _, task, _ = ledger.submit(scope="default", key=kind, payload=payload, prompt="p", native_session_id=None)
            att = ledger.current_attempt(task["task_id"])
            if kind == "unknown":
                ledger.mark_unknown(att["attempt_id"], from_states=("pending",), evidence={})
            else:
                ledger.request_cancel(task["task_id"])
            done = self.cli("status", task["task_id"], "--no-reconcile")
            self.assertEqual(done["task"]["state"], kind)
            self.assertEqual(done["task"]["business_status"], "failed")
            self.assertEqual(done["task"]["business_status_source"], "outcome")
            self.assertEqual(done["task"]["denials"], {"count": None, "tools": [], "available": False})

    def test_new_attempt_reports_its_own_status_and_recomputes_late_unknown_evidence(self):
        sub, _ = self.finish("status=partial ok=9")
        ledger = Ledger(self.home)
        self.addCleanup(ledger.close)
        att = ledger.new_attempt(sub["task_id"], expect_attempt_no=1, reason="resume", prompt="continue",
                                 resume_session_id=sub["native_session_id"], native_session_id=sub["native_session_id"])
        pending = self.cli("status", sub["task_id"], "--no-reconcile")
        self.assertNotIn("business_status", pending["task"])
        self.assertEqual(pending["attempts"][0]["business_status"], "partial")
        ledger.mark_unknown(att["attempt_id"], from_states=("pending",), evidence={"warnings": [{"code": "other"}]})
        ledger.finish(att["attempt_id"], from_states=("unknown",), outcome="succeeded", native={"result": "status=ok"})
        done = self.cli("status", sub["task_id"], "--no-reconcile")
        self.assertEqual(done["task"]["business_status"], "ok")
        self.assertNotIn("business_status_counts", done["task"])
        self.assertEqual(done["attempts"][1]["evidence"]["warnings"], [{"code": "other"}])


if __name__ == "__main__":
    unittest.main()

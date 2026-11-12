"""Issue #32: warn before a business-status dependency silently becomes blocked."""

import json

from helpers import RelayTestCase, db_rows
from test_terminal_observations import notification

from cli_relay.ledger import payload_hash


class DependencyStatusDiagnostics(RelayTestCase):
    def after(self, key, dep, require=None):
        return self.cli("submit", "--key", key, "--engine", "fixture", "--cwd", str(self.work),
                        "--prompt", "follow up", "--after", dep["task_id"],
                        *(["--after-require", require] if require else []))

    def finish(self, sub):
        done = self.cli("wait", sub["task_id"], "--timeout", "30", "--interval", "0.1")
        self.assertFalse(done["timed_out"])
        return done

    def test_running_upstream_without_contract_warns_for_both_business_gates(self):
        dep = self.submit("upstream", duration=60)
        for require in (None, "ok_or_partial"):
            with self.subTest(require=require):
                sub = self.after(str(require), dep, require)
                warning, = sub["warnings"]
                self.assertEqual(warning["code"], "dependency_business_status_not_declared")
                self.assertEqual(warning["dependency_task_id"], dep["task_id"])
                self.assertEqual(warning["require"], require or "ok")
                for text in ("result_file", "require_business_status", "ok|partial|failed",
                             "null|blocked|needs_user|stopped_for_approval", "unreported"):
                    self.assertIn(text, warning["hint"])
                self.cli("cancel", sub["task_id"])
        self.cli("cancel", dep["task_id"])

    def test_declared_contract_and_any_terminal_do_not_warn(self):
        for key, options in (("file", ['result_file="out/result.json"']),
                             ("required", ["require_business_status=true"]), ("any", [])):
            with self.subTest(key=key):
                dep = self.submit(key, duration=60, extra_options=options)
                sub = self.after("after-" + key, dep, "any_terminal" if key == "any" else None)
                self.assertNotIn("warnings", sub)
                self.cli("cancel", sub["task_id"])
                self.cli("cancel", dep["task_id"])

    def test_reported_ok_without_declared_contract_runs_without_warning(self):
        dep = self.submit("upstream", duration=0, extra_options=['result="status=ok"'])
        self.finish(dep)
        sub = self.after("downstream", dep)
        self.assertNotIn("warnings", sub)
        self.assertEqual(self.finish(sub)["task"]["state"], "succeeded")

    def test_invalid_result_status_is_in_receipt_terminal_and_notification(self):
        result = self.work / "out" / "result.json"
        result.parent.mkdir()
        result.write_text(json.dumps({"status": "complete_with_gaps", "private": "do-not-copy-this"}))
        dep = self.submit("upstream", duration=0, extra_options=['result_file="out/result.json"'])
        self.finish(dep)
        sub = self.after("downstream", dep, "ok_or_partial")
        warning, = sub["warnings"]
        self.assertEqual(warning["code"], "dependency_business_status_unreported")
        raw = {"result_file": {"raw_status": "complete_with_gaps", "raw_stop_reason": None}}
        self.assertEqual(warning["dependency_business_reports"], raw)
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertIsNone(done["attempts"][0]["cli"]["pid"])
        terminal = next(e["body"] for e in done["events"] if e["kind"] == "terminal")
        for record in (done["attempts"][0]["evidence"], terminal,
                       notification(done)["cli_relay"]["events"][0]):
            self.assertEqual(record["reason_code"], "dependency_not_satisfied")
            self.assertEqual(record["details"]["dependency_business_reports"], raw)
            self.assertIn('"raw_status":"complete_with_gaps"', record["summary"])
        self.assertIn("complete_with_gaps", notification(done)["summary"])
        self.assertNotIn("do-not-copy-this", json.dumps(sub) + json.dumps(notification(done)))
        # Receipt warnings neither change idempotency nor mutate the upstream attempt.
        payload = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks WHERE task_id=?",
                                     (sub["task_id"],))[0][0])
        self.assertEqual(sub["payload_sha256"], payload_hash(payload))
        self.assertNotIn("warnings", payload)
        self.assertEqual(self.after("downstream", dep, "ok_or_partial")["status"], "duplicate")
        self.assertEqual(len(self.status(dep["task_id"])["attempts"]), 1)

    def test_unknown_status_line_is_reported_as_a_raw_value(self):
        dep = self.submit("upstream", duration=0, extra_options=['result="status=complete"'])
        self.finish(dep)
        sub = self.after("downstream", dep)
        raw = {"status_line": {"raw_status": "complete", "raw_stop_reason": None}}
        self.assertEqual(sub["warnings"][0]["dependency_business_reports"], raw)
        done = self.finish(sub)
        self.assertEqual(done["attempts"][0]["evidence"]["details"]["dependency_business_reports"], raw)

    def test_invalid_stop_reason_keeps_both_sources_visible(self):
        (self.work / "result.json").write_text(json.dumps({"status": "ok", "stop_reason": "please review"}))
        dep = self.submit("upstream", duration=0,
                          extra_options=['result_file="result.json"', 'result="status=ok"'])
        self.finish(dep)
        sub = self.after("downstream", dep)
        raw = sub["warnings"][0]["dependency_business_reports"]
        self.assertEqual(raw["result_file"], {"raw_status": "ok", "raw_stop_reason": "please review"})
        self.assertEqual(raw["status_line"], {"raw_status": "ok", "raw_stop_reason": None})
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertIn("please review", notification(done)["summary"])

    def test_missing_report_warns_even_when_required_and_does_not_relax_the_gate(self):
        dep = self.submit("upstream", duration=0, extra_options=["require_business_status=true"])
        self.finish(dep)
        sub = self.after("downstream", dep)
        self.assertEqual(sub["warnings"][0]["code"], "dependency_business_status_unreported")
        self.assertEqual(self.finish(sub)["task"]["state"], "blocked")
        self.assertNotIn("warnings", self.after("any", dep, "any_terminal"))

    def test_diagnostic_values_are_bounded_and_summary_stays_one_line(self):
        raw_status = "bad\n\x1b[31m" + "x" * 1000
        result = self.work / "result.json"
        result.write_text(json.dumps({"status": raw_status, "stop_reason": "ask\nEvan"}))
        dep = self.submit("upstream", duration=0, extra_options=['result_file="result.json"'])
        self.finish(dep)
        sub = self.after("downstream", dep)
        raw = sub["warnings"][0]["dependency_business_reports"]["result_file"]
        self.assertEqual(len(raw["raw_status"]), 128)
        done = self.finish(sub)
        summary = done["attempts"][0]["evidence"]["summary"]
        self.assertNotIn("\n", summary)
        self.assertNotIn("\x1b", summary)
        self.assertIn('"raw_status":"bad\\n\\u001b[31m', summary)
        self.assertIn('"raw_stop_reason":"ask\\nEvan"', summary)

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

    def file(self, data, text=None, **kwargs):
        self.result.write_text(json.dumps(data))
        return self.evaluate(text, options={"result_file":"out/result.json"}, **kwargs)

    def codes(self, report):
        return [w["code"] for w in report.get("warnings", [])]

    def test_status_aliases_and_raw_stops(self):
        for word in ("ok","done","partial","failed","fail",*business_status.STOP_ALIASES):
            with self.subTest(word=word):
                r = self.file({"status":word}, "status=ok")
                self.assertEqual(r['business_status'], business_status.normalize_status(word))
                self.assertEqual(r['business_stop_reason'], business_status.STOP_ALIASES.get(word))
                self.assertEqual(r['business_reports']['result_file']['raw_status'], word)
        self.assertEqual(self.evaluate('status=mystery')['business_status'],'unreported')

    def test_only_exact_final_markers_and_legacy_tagged_markers_count(self):
        for text in (None,'','finished successfully','Quoted: status=ok','status=ok and done',
                     'status=ok\nMore prose','```\nstatus=ok\n```','not_status=ok','status=failed-ish'):
            self.assertEqual(self.evaluate(text)['business_status'],'unreported',text)
        r = self.evaluate('old\nWHAT_IS_DIFY_DONE status=partial ok=9 fail=4 skip=04\n\n')
        self.assertEqual(r['business_status_counts'],{'ok':9,'fail':4,'skip':4})
        self.assertEqual(r['business_status'],'partial')

    def test_canonical_stop_reason_never_promotes_ok(self):
        for reason in ('blocked','needs_user','stopped_for_approval','login_required'):
            r = self.file({'status':'ok','stop_reason':reason},'status=ok')
            self.assertEqual(r['business_status'],'partial')
            self.assertEqual(r['business_stop_reason'],business_status.STOP_ALIASES[reason])
            self.assertEqual(r['business_reports']['result_file']['raw_stop_reason'],reason)
        canonical = self.file({'status':'partial','stop_reason':'needs_user'},'status=partial')
        self.assertEqual(canonical['business_stop_reason'],'needs_user')
        self.assertNotIn('warnings', canonical)

    def test_precedence_conflict_unknown_and_denials(self):
        for a,b,want in [('ok','failed','failed'),('partial','ok','partial'),('blocked','done','partial')]:
            r=self.file({'status':a},'status='+b)
            self.assertEqual(r['business_status'],want)
            self.assertIn('business_status_conflict',self.codes(r))
        self.assertEqual(self.file({'status':'ok'},'status=done')['business_status_source'],'result_file')
        for data,text in [({'status':'mystery'},'status=ok'),({'status':'ok'},'status=mystery'),
                          ({'status':'ok','stop_reason':'maybe'},'status=ok')]:
            self.assertEqual(self.file(data,text)['business_status'],'unreported')
        r=self.file({'status':'done'},'status=ok',engine='claude',denials=['Bash','Read','Bash'])
        self.assertEqual((r['business_status'],r['business_status_source']),('partial','denials'))
        self.assertEqual(r['denials'],{'available':True,'count':3,'tools':['Bash','Read']})

    def test_non_success_process_and_stop_semantics(self):
        self.assertEqual(self.evaluate('status=ok',outcome='failed')['business_status'],'failed')
        for outcome,reason in [('blocked','blocked'),('needs_input','needs_user')]:
            r=self.evaluate('status=ok',outcome=outcome)
            self.assertEqual((r['business_status'],r['business_stop_reason']),('partial',reason))
        r=self.evaluate('status=stopped_for_approval',outcome='failed')
        self.assertEqual((r['business_status'],r['business_stop_reason']),('partial','stopped_for_approval'))
        self.assertFalse(self.evaluate()['denials']['available'])

    def test_missing_declared_file_cannot_be_overruled_by_ok_and_path_mismatch_is_not_read(self):
        (self.work/'result.json').write_text('{"status":"done","summary":"DO_NOT_CAPTURE"}')
        r=self.evaluate('The file is result.json.\nstatus=ok',options={'result_file':'out/result.json'})
        self.assertEqual(r['business_status'],'unreported')
        self.assertIn('business_status_result_path_mismatch',self.codes(r))
        self.assertFalse(r['result_evidence']['exists'])
        self.assertNotIn('DO_NOT_CAPTURE',json.dumps(r))

    def test_malformed_duplicate_unknown_and_oversized_files_stay_visible(self):
        for data in (b'not JSON',b'[]',b'{"status":1}',b'{"status":"done","status":"ok"}',
                     b'\xff',b' '*(business_status.MAX_RESULT_FILE_BYTES+1)):
            self.result.write_bytes(data)
            r=self.evaluate('status=ok',options={'result_file':'out/result.json'})
            self.assertEqual(r['business_status'],'unreported')
            self.assertTrue(self.codes(r))
        self.result.write_text('{"status":"ok"}')
        with patch.object(business_status.os,'open',side_effect=PermissionError):
            r=self.evaluate('status=ok',options={'result_file':'out/result.json'})
        self.assertEqual(r['result_evidence']['validation'],'unreadable')

    def test_required_and_reported_artifacts_checked_separately_from_process(self):
        r=self.file({'status':'done','required_artifacts':['report.md'],'artifacts':[]})
        self.assertEqual((r['business_status'],r['artifact_verification']['state']),('partial','failed'))
        (self.work/'report.md').write_text('fixture')
        r=self.file({'status':'ok','required_artifacts':['report.md'],'artifacts':[str(self.work/'report.md')]})
        self.assertEqual((r['business_status'],r['artifact_verification']['state']),('ok','passed'))
        self.assertEqual(r['artifact_verification']['method'],'local_file_existence_only')
        r=self.evaluate('status=ok',options={'required_artifacts':['missing.md']})
        self.assertEqual((r['business_status'],r['artifact_verification']['state']),('partial','failed'))
        for values in ('report.md',['../outside'],[str(self.work.parent/'outside')],[None]):
            self.assertEqual(self.file({'status':'ok','required_artifacts':values})['artifact_verification']['state'],'failed')

    def test_symlinks_and_submit_validation(self):
        outside=self.work.parent/'outside.json'
        outside.write_text('{"status":"ok"}')
        self.result.symlink_to(outside)
        r=self.evaluate('status=ok',options={'result_file':'out/result.json'})
        self.assertEqual(r['business_status'],'unreported')
        self.assertEqual(r['result_evidence']['validation'],'unsafe_path')
        for path in ('../result.json','out/../result.json',str(self.result),'.','out/*',''):
            with self.assertRaises(UsageError):
                engines.validate_options('fixture',{'result_file':path},cwd=str(self.work))

    def test_nonregular_result_and_artifact_symlink_escape_do_not_count_as_output(self):
        import os
        os.mkfifo(self.result)
        r=self.evaluate('status=ok',options={'result_file':'out/result.json'})
        self.assertEqual(r['business_status'], 'unreported')
        self.result.unlink()
        outside=self.work.parent/'outside.md'
        outside.write_text('do not read')
        (self.work/'report.md').symlink_to(outside)
        r=self.file({'status':'ok','required_artifacts':['report.md']})
        self.assertEqual((r['business_status'],r['artifact_verification']['state']),('partial','failed'))

    def test_snapshot_is_allowlisted_bounded_and_hashes_the_exact_file(self):
        import hashlib
        r=self.file({'status':'partial','stop_reason':'blocked','summary':'SYNTHETIC_SECRET',
                     'extra':{'token':'SYNTHETIC_SECRET'}})
        e=r['result_evidence']
        self.assertEqual(e['sha256'],hashlib.sha256(self.result.read_bytes()).hexdigest())
        self.assertEqual(e['snapshot'],{'status':'partial','stop_reason':'blocked'})
        self.assertNotIn('SYNTHETIC_SECRET',json.dumps(r))

    def test_finance_investigation_fixtures_do_not_infer_missing_reports(self):
        for count in (12,13):
            opts={} if count==12 else {'result_file':'out/result.json'}
            if count==13:
                (self.work/'result.json').write_text('{"status":"done"}')
                (self.work/'report.md').write_text('archived fixture; content quality unverified')
            r=self.evaluate('Report: root result.json',engine='claude',denials=['Bash']*count,options=opts)
            self.assertEqual((r['business_status'],r['business_status_source']),('partial','denials'))
            self.assertEqual(r['denials']['count'],count)
            if count==13:
                self.assertFalse(r['result_evidence']['exists'])
                self.assertTrue((self.work/'report.md').exists())
                self.assertIn('business_status_result_path_mismatch',self.codes(r))

    def test_reporting_options_do_not_change_engine_argv_or_permissions(self):
        for engine in engines.ENGINES:
            for resume in (None,'old-session'):
                def build(options):
                    return engines.build(engine,payload={'cwd':str(self.work),'options':options},
                        attempt={'native_session_id':'sid','resume_session_id':resume,'attempt_id':'a-test'},
                        run_dir=self.work,fixture_state_dir=str(self.work),
                        env={'CLI_RELAY_CLAUDE_BIN':'/bin/echo','CLI_RELAY_CODEX_BIN':'/bin/echo'})
                before,after=build({}),build({'result_file':'out/result.json','required_artifacts':['r.md']})
                self.assertEqual(before.argv,after.argv)
                self.assertEqual(before.evidence,after.evidence)


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
        self.assertIn(f"business_status={status} stop_reason=", delivered["summary"])

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
            self.assertTrue(done["attempts"][0]["evidence"].get("warnings"))

    def test_require_business_status_warns_without_changing_outcome(self):
        _, done = self.finish("本次失败", options={"require_business_status": True})
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_fields(done, "unreported", "none")
        self.assertEqual(done["attempts"][0]["evidence"]["warnings"], [{"code": "business_status_missing"}])

    def test_failed_outcome_keeps_reported_approval_stop(self):
        _, done = self.finish("TASK_DONE status=stopped_for_approval ok=0 fail=0 skip=4", options={"exit_code": 1})
        self.assertEqual(done["task"]["state"], "failed")
        self.assert_fields(done, "partial", "outcome", {"ok": 0, "fail": 0, "skip": 4})

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
        # A later writer must not change the exit-time report recovered after the crash.
        (self.work / "result.json").write_text('{"status":"ok"}')
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")
        self.assert_fields(done, "partial", "result_file")
        self.assertEqual(done["task"]["result_evidence"]["snapshot"]["status"], "partial")

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

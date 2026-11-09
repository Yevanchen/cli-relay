"""Completion self-reports are separate from process and business outcomes."""
import json
import unittest

from cli_relay import business_status, notify
from helpers import RelayTestCase
from test_terminal_observations import notification


class CompletionParsing(unittest.TestCase):
    def test_valid_and_ambiguous_lines(self):
        for done in ("做完", "没做完", "部分"):
            text = f"说明\n做完没：{done}\n还差：无\n下一步谁动：没有\nstatus=ok"
            self.assertEqual(business_status.completion_report(text),
                             {"state": "已报告", "done": done, "remaining": "无", "next_owner": "没有"})
        for text in (None, "做完没：做完", "做完没：也许\n还差：无\n下一步谁动：没有",
                     "做完没：做完\n做完没：部分\n还差：无\n下一步谁动：没有",
                     "做完没：做完\n还差：\n下一步谁动：没有"):
            self.assertEqual(business_status.completion_report(text), {"state": "没说做完没"})

    def test_historical_notification_defaults_to_missing(self):
        row = {"body_json": json.dumps({"outcome": "succeeded"}), "event_id": "e", "task_id": "t",
               "attempt_no": 1, "kind": "terminal"}
        self.assertEqual(notify._event_ref(row)["completion_report"], {"state": "没说做完没"})


class CompletionRoundTrip(RelayTestCase):
    def test_missing_report_does_not_fail_or_retry_and_is_visible(self):
        sub = self.submit("missing", duration=0, extra_options=['result="status=ok"'])
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(len(done["attempts"]), 1)
        for item in (done["task"], done["attempts"][0]["evidence"], done["events"][0]["body"],
                     notification(done)["cli_relay"]["events"][0]):
            self.assertEqual(item["completion_report"], {"state": "没说做完没"})
        self.assertIn("没说做完没", notification(done)["summary"])
        brief = self.cli("brief", "--consumer", "reader")["events"][0]
        self.assertIn("没说做完没", brief["message"])
        prompt = self.attempt_row(sub["task_id"])["prompt"]
        self.assertTrue(prompt.endswith(business_status.COMPLETION_PROMPT))
        again = self.submit("missing", duration=0, extra_options=['result="status=ok"'])
        self.assertEqual(again["status"], "duplicate")

    def test_report_is_frozen_even_beyond_excerpt_and_sent(self):
        report = "做完没：部分\n还差：审核\n下一步谁动：Evan"
        sub = self.submit("reported", duration=0, extra_options=["result=" + json.dumps("x" * 3000 + "\n" + report)])
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        expected = {"state": "已报告", "done": "部分", "remaining": "审核", "next_owner": "Evan"}
        self.assertEqual(done["events"][0]["body"]["completion_report"], expected)
        self.assertEqual(notification(done)["cli_relay"]["events"][0]["completion_report"], expected)
        brief = self.cli("brief", "--consumer", "reader")["events"][0]
        self.assertIn("下一步谁动：Evan", brief["message"])

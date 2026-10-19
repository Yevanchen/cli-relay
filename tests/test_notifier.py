"""Notifier: durable, bounded, deduplicated wake-ups; transport acceptance kept apart from the consumer ACK.

Every scenario uses the fixture engine and a local fake webhook: no model, no real Bot.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import REPO, RelayTestCase, base_env, run_cli, wait_until
from notify_helpers import TOKEN, FakeWebhook, free_port, http_config, write_config

from cli_relay.ledger import SCHEMA, SCHEMA_VERSION, now_iso


class NotifierCase(RelayTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.servers = []

    def tearDown(self) -> None:
        for s in self.servers:
            try:
                s.stop()
            except Exception:
                pass
        super().tearDown()

    def webhook(self, behaviours=None, port: int = 0) -> FakeWebhook:
        s = FakeWebhook(behaviours, port=port)
        self.servers.append(s)
        return s

    def finish_task(self, key: str, *options: str, home: Path = None, expect: str = "succeeded") -> dict:
        args = ["submit", "--key", key, "--engine", "fixture", "--cwd", str(self.work), "--prompt", f"p {key}",
                "--option", "duration_s=0.05"]
        for o in options:
            args += ["--option", o]
        code, sub = run_cli(home or self.home, *args)
        self.assertEqual(code, 0, sub)
        done = run_cli(home or self.home, "wait", sub["task_id"], "--timeout", "30")[1]
        self.assertEqual(done["task"]["state"], expect)
        return {"task_id": sub["task_id"], "event": done["events"][-1]}

    def flush(self, home: Path = None, env: dict = None) -> dict:
        code, out = run_cli(home or self.home, "notify", "flush", env=env)
        self.assertEqual(code, 0, out)
        return out

    def raw_flush(self, env_extra: dict, home: Path = None) -> int:
        p = subprocess.run([sys.executable, "-m", "cli_relay", "--home", str(home or self.home), "notify", "flush"],
                           env=base_env(env_extra), capture_output=True, text=True, timeout=60)
        return p.returncode

    def rows(self, home: Path = None) -> list:
        return run_cli(home or self.home, "notify", "status")[1]["notifications"]

    def ack(self, task_id: str, event: dict, consumer: str = "bot", home: Path = None, *extra: str):
        return run_cli(home or self.home, "ack", "--consumer", consumer, "--task", task_id, "--attempt",
                       str(event["attempt_no"]), "--event", event["event_id"], *extra)


class SendAndAcknowledge(NotifierCase):
    def test_one_wake_with_routing_identity_then_the_consumer_ack_closes_it(self) -> None:
        t = self.finish_task("k-n1")
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url))
        self.flush()
        self.assertEqual(len(hook.requests), 1)
        req = hook.requests[0]
        self.assertEqual(req["auth"], f"Bearer {TOKEN}")
        body = req["body"]
        self.assertEqual(set(body) >= {"job", "stage", "status", "summary", "artifact", "cli_relay"}, True)
        self.assertEqual(body["status"], "succeeded")
        rec = body["cli_relay"]
        ledger_id = json.loads((self.home / "LEDGER.json").read_text())["ledger_id"]
        self.assertEqual((rec["source_id"], rec["ledger_id"], rec["route"], rec["consumer"]),
                         ("host-a", ledger_id, "bot", "bot"))
        self.assertEqual([(e["event_id"], e["task_id"], e["attempt_no"]) for e in rec["events"]],
                         [(t["event"]["event_id"], t["task_id"], 1)])
        row = self.rows()[0]
        self.assertEqual((row["state"], row["transport_accepted"], row["consumer_acked"]), ("accepted", True, False))
        self.flush()
        self.assertEqual(len(hook.requests), 1, "no resend before the ack timeout")
        self.assertEqual(self.ack(t["task_id"], t["event"])[0], 0)
        self.flush()
        row = self.rows()[0]
        self.assertEqual((row["state"], row["consumer_acked"]), ("acked", True))
        time.sleep(2.2)
        self.flush()
        self.assertEqual(len(hook.requests), 1, "an acknowledged event is never sent again")

    def test_only_the_configured_outcomes_wake_the_consumer(self) -> None:
        ok = self.finish_task("k-n2-ok")
        failed = self.finish_task("k-n2-fail", "exit_code=1", expect="failed")
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url, outcomes=["failed"]))
        self.flush()
        states = {r["event_id"]: r["state"] for r in self.rows()}
        self.assertEqual(states[ok["event"]["event_id"]], "skipped")
        self.assertEqual(states[failed["event"]["event_id"]], "accepted")
        self.assertEqual(len(hook.requests), 1)

    def test_cancelled_is_not_a_default_wake_and_no_route_means_no_wake(self) -> None:
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url, default_route=None))
        self.finish_task("k-n3")  # no --notify-route and no default route
        self.flush()
        self.assertEqual([r["state"] for r in self.rows()], ["skipped"])
        self.assertEqual(hook.requests, [])


class CrashesAroundTheSend(NotifierCase):
    def test_crash_before_send_is_sent_once_by_the_next_pass(self) -> None:
        self.finish_task("k-n4")
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url))
        self.assertNotEqual(self.raw_flush({"CLI_RELAY_TEST_CRASH": "notify_after_claim"}), 0)
        self.assertEqual(hook.requests, [])
        self.flush()
        self.assertEqual(len(hook.requests), 1)
        self.assertEqual(self.rows()[0]["state"], "accepted")

    def test_crash_after_send_is_never_resent_blindly(self) -> None:
        self.finish_task("k-n5")
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url))
        self.assertNotEqual(self.raw_flush({"CLI_RELAY_TEST_CRASH": "notify_after_send"}), 0)
        self.assertEqual(len(hook.requests), 1)
        self.flush()
        row = self.rows()[0]
        self.assertEqual((row["state"], row["wakes"]), ("unconfirmed", 1))
        self.assertEqual(len(hook.requests), 1, "a send that may have woken the consumer is not repeated at once")
        time.sleep(2.2)
        self.flush()  # no consumer ACK within the timeout: one reminder
        self.assertEqual(len(hook.requests), 2)
        self.assertTrue(hook.bodies()[1]["cli_relay"]["reminder"])
        time.sleep(2.2)
        self.flush()
        self.assertEqual(len(hook.requests), 2, "max_wakes bounds the wake-ups")
        self.assertEqual(self.rows()[0]["state"], "unacknowledged")


class NetworkFaults(NotifierCase):
    def test_outage_then_recovery(self) -> None:
        self.finish_task("k-n6")
        port = free_port()
        write_config(self.home, http_config(self.tmp, f"http://127.0.0.1:{port}/hook"))
        out = self.flush()
        self.assertEqual(out["sends"][0]["outcome"], "retry")
        row = self.rows()[0]
        self.assertEqual((row["state"], row["attempts"], row["wakes"]), ("pending", 1, 0))
        hook = self.webhook(port=port)
        time.sleep(0.4)
        self.flush()
        self.assertEqual(len(hook.requests), 1)
        self.assertEqual(self.rows()[0]["state"], "accepted")

    def test_lost_response_is_unconfirmed_and_reminded_within_bounds(self) -> None:
        self.finish_task("k-n7")
        hook = self.webhook(["drop"])
        write_config(self.home, http_config(self.tmp, hook.url))
        out = self.flush()
        self.assertEqual(out["sends"][0]["outcome"], "ambiguous")
        self.assertEqual(self.rows()[0]["state"], "unconfirmed")
        self.flush()
        self.assertEqual(len(hook.requests), 1)
        time.sleep(2.2)
        self.flush()
        self.assertEqual(len(hook.requests), 2)
        time.sleep(2.2)
        self.flush()
        self.assertEqual(len(hook.requests), 2)
        self.assertEqual(self.rows()[0]["state"], "unacknowledged")

    def test_server_restart_between_two_events(self) -> None:
        port = free_port()
        hook = self.webhook(port=port)
        # A long ack timeout: the first, accepted event must not come due for its reminder here.
        write_config(self.home, http_config(self.tmp, hook.url, ack_timeout_s=120))
        first = self.finish_task("k-n8a")
        wait_until(lambda: len(hook.requests) == 1, timeout=15)  # the keeper starts a pass itself
        hook.stop()
        self.servers.remove(hook)
        second = self.finish_task("k-n8b")
        wait_until(lambda: any(r["event_id"] == second["event"]["event_id"] and r["attempts"] >= 1
                               for r in self.rows()), timeout=15)
        restarted = self.webhook(port=port)
        time.sleep(0.4)
        self.flush()
        self.assertEqual([b["cli_relay"]["events"][0]["event_id"] for b in restarted.bodies()],
                         [second["event"]["event_id"]], "only the event that was not accepted is sent again")
        self.assertEqual({r["state"] for r in self.rows()}, {"accepted"})
        self.assertNotEqual(first["event"]["event_id"], second["event"]["event_id"])

    def test_rate_limited_then_accepted(self) -> None:
        self.finish_task("k-n9")
        hook = self.webhook(["429"])
        write_config(self.home, http_config(self.tmp, hook.url))
        out = self.flush()
        self.assertEqual((out["sends"][0]["outcome"], out["sends"][0]["status"]), ("retry", 429))
        self.flush()
        self.assertEqual(len(hook.requests), 1, "Retry-After is honoured")
        time.sleep(1.1)
        self.flush()
        self.assertEqual(len(hook.requests), 2)
        self.assertEqual(self.rows()[0]["state"], "accepted")

    def test_retries_are_bounded(self) -> None:
        self.finish_task("k-n10")
        hook = self.webhook(["500"] * 10)
        write_config(self.home, http_config(self.tmp, hook.url, max_attempts=3))
        for _ in range(8):
            self.flush()
            time.sleep(0.6)
        self.assertEqual(len(hook.requests), 3)
        row = self.rows()[0]
        self.assertEqual((row["state"], row["attempts"]), ("failed", 3))


class DuplicatesAndAcks(NotifierCase):
    def test_concurrent_passes_and_duplicate_submits_send_once(self) -> None:
        t = self.finish_task("k-n11")
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url))
        procs = [subprocess.Popen([sys.executable, "-m", "cli_relay", "--home", str(self.home), "notify", "flush"],
                                  env=base_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for _ in range(4)]
        for p in procs:
            p.wait(timeout=60)
        run_cli(self.home, "submit", "--key", "k-n11", "--engine", "fixture", "--cwd", str(self.work), "--prompt",
                "p k-n11", "--option", "duration_s=0.05")
        self.flush()
        self.assertEqual(len(hook.requests), 1)
        self.assertEqual([r["event_id"] for r in self.rows()], [t["event"]["event_id"]])

    def test_a_lost_consumer_ack_gets_a_bounded_reminder(self) -> None:
        t = self.finish_task("k-n12")
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url))
        self.flush()
        time.sleep(2.2)  # the consumer handled it, but its ACK never reached the ledger
        self.flush()
        self.assertEqual(len(hook.requests), 2)
        code, first = self.ack(t["task_id"], t["event"])
        code2, again = self.ack(t["task_id"], t["event"])  # the reply of the first ACK was lost: ACK again
        self.assertEqual((code, code2, first["duplicate"], again["duplicate"]), (0, 0, False, True))
        self.flush()
        self.assertEqual(self.rows()[0]["state"], "acked")
        time.sleep(2.2)
        self.flush()
        self.assertEqual(len(hook.requests), 2)

    def test_unacknowledged_events_are_never_cleared(self) -> None:
        t = self.finish_task("k-n13")
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url, max_wakes=1))
        self.flush()
        time.sleep(2.2)  # accepted (HTTP 200) but the Bot run failed (e.g. rate limit), no ACK
        self.flush()
        self.assertEqual(self.rows()[0]["state"], "unacknowledged")
        pending = run_cli(self.home, "events", "--consumer", "bot")[1]["events"]
        self.assertEqual([e["event_id"] for e in pending], [t["event"]["event_id"]])
        self.assertEqual(run_cli(self.home, "notify", "retry", "--event", t["event"]["event_id"])[0], 0)
        self.flush()
        self.assertEqual(len(hook.requests), 2, "only an explicit retry sends it again")


class TwoHosts(NotifierCase):
    def test_each_host_names_itself_and_acks_never_cross(self) -> None:
        hook = self.webhook()
        other = self.tmp / "home-b"
        self.assertEqual(run_cli(other, "init")[0], 0)
        a = self.finish_task("k-n14a")
        b = self.finish_task("k-n14b", home=other)
        write_config(self.home, http_config(self.tmp, hook.url, source_id="host-a"))
        write_config(other, http_config(self.tmp, hook.url, source_id="host-b"))
        self.flush()
        self.flush(home=other)
        seen = {(x["cli_relay"]["source_id"], x["cli_relay"]["ledger_id"], x["cli_relay"]["events"][0]["event_id"])
                for x in hook.bodies()}
        lid_a = json.loads((self.home / "LEDGER.json").read_text())["ledger_id"]
        lid_b = json.loads((other / "LEDGER.json").read_text())["ledger_id"]
        self.assertEqual(seen, {("host-a", lid_a, a["event"]["event_id"]), ("host-b", lid_b, b["event"]["event_id"])})
        code, out = self.ack(a["task_id"], a["event"], "bot", other, "--ledger-id", lid_a)
        self.assertEqual((code, out["error"]), (6, "wrong_ledger"))
        code, out = self.ack(a["task_id"], a["event"], "bot", other)
        self.assertEqual(code, 6, "host B has no such event")
        self.assertEqual(self.ack(a["task_id"], a["event"], "bot", self.home, "--ledger-id", lid_a,
                                  "--source-id", "host-a")[0], 0)
        self.flush()
        self.flush(home=other)
        self.assertEqual(self.rows()[0]["state"], "acked")
        self.assertEqual(self.rows(home=other)[0]["state"], "accepted")
        events_a = run_cli(self.home, "events", "--consumer", "x", "--include-acked")[1]
        self.assertEqual((events_a["source_id"], events_a["ledger_id"]), ("host-a", lid_a))
        self.assertEqual(events_a["events"][0]["route"], "bot")


class CommandAdapterLikeNotifySh(NotifierCase):
    def fake_notify_sh(self) -> Path:
        script = self.tmp / "notify.sh"
        calls = self.tmp / "calls.jsonl"
        script.write_text("#!/bin/sh\n"
                          f'"{sys.executable}" -c \'import json,sys; open(sys.argv[1],"a").write(json.dumps(sys.argv[2:])+"\\n")\' '
                          f"'{calls}' \"$@\"\nexit 0\n")
        script.chmod(0o755)
        return script

    def test_exit_0_is_a_hand_off_not_an_acceptance(self) -> None:
        t = self.finish_task("k-n15", 'result="line one\\nline \\"two\\" $HOME"')
        script = self.fake_notify_sh()
        cfg = http_config(self.tmp, "http://unused.invalid/")
        cfg["routes"]["bot"]["adapter"] = {"type": "command", "argv": [str(script)], "job": "CLI_RELAY"}
        write_config(self.home, cfg)
        self.flush()
        calls = [json.loads(x) for x in (self.tmp / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        job, stage, status, summary, artifact = calls[0]
        self.assertEqual((job, stage, status), ("CLI_RELAY", "host-a/bot", "succeeded"))
        self.assertIn(t["event"]["event_id"], summary)
        self.assertNotIn("\n", summary)
        self.assertNotIn('"', summary)
        self.assertTrue(artifact.startswith("cli-relay:host-a:"))
        row = self.rows()[0]
        self.assertEqual((row["state"], row["transport_accepted"]), ("handed_off", False))
        time.sleep(2.2)
        self.flush()
        self.assertEqual(len((self.tmp / "calls.jsonl").read_text().splitlines()), 2, "bounded reminder")


class TriggerSecretsAndConfig(NotifierCase):
    def test_the_keeper_starts_a_pass_when_the_task_ends(self) -> None:
        hook = self.webhook()
        write_config(self.home, http_config(self.tmp, hook.url))
        t = self.finish_task("k-n16")
        wait_until(lambda: len(hook.requests) == 1, timeout=15)
        self.assertEqual(hook.bodies()[0]["cli_relay"]["events"][0]["event_id"], t["event"]["event_id"])

    def test_the_token_never_appears_in_output_or_logs(self) -> None:
        hook = self.webhook(["500", "ok"])
        write_config(self.home, http_config(self.tmp, hook.url))
        self.finish_task("k-n17")
        wait_until(lambda: len(hook.requests) >= 1, timeout=15)
        time.sleep(0.4)
        outputs = [json.dumps(self.flush()), json.dumps(self.rows()),
                   json.dumps(run_cli(self.home, "events", "--consumer", "bot")[1])]
        logs = [p.read_text(errors="replace") for p in self.home.glob("*.log")]
        for text in outputs + logs:
            self.assertNotIn(TOKEN, text)

    def test_invalid_configuration_is_refused_and_nothing_is_sent(self) -> None:
        self.finish_task("k-n18")
        write_config(self.home, {"source_id": "host-a", "routes": {"bot": {"consumer": "bot", "adapter": {
            "type": "command", "argv": ["relative/notify.sh"]}}}})
        code, out = run_cli(self.home, "notify", "flush")
        self.assertEqual((code, out["error"]), (2, "usage"))

    def test_a_route_must_exist_in_the_configuration(self) -> None:
        write_config(self.home, http_config(self.tmp, "http://unused.invalid/"))
        code, out = run_cli(self.home, "submit", "--key", "k-n19", "--engine", "fixture", "--cwd", str(self.work),
                            "--prompt", "p", "--notify-route", "nobody")
        self.assertEqual(code, 2, out)


class MigrationAndRollback(unittest.TestCase):
    """A v0.1 ledger gets the notifier tables only through `migrate`, after a consistent backup;
    events from before the migration are not notified unless --backfill; v0.1.0 still reads it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-mig-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        conn = sqlite3.connect(str(self.home / "ledger.db"), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA +
                           f"\nINSERT INTO meta (k, v) VALUES ('schema_version', '{SCHEMA_VERSION}');\nCOMMIT;\n")
        conn.close()
        (self.home / "LEDGER.json").write_text(json.dumps({"format": "cli-relay-ledger", "schema_version": 1,
                                                           "ledger_id": "l-v01ledger", "created_at": now_iso()}))
        (self.tmp / "work").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def task(self, key: str) -> str:
        code, sub = run_cli(self.home, "submit", "--key", key, "--engine", "fixture", "--cwd", str(self.tmp / "work"),
                            "--prompt", "p", "--option", "duration_s=0.05")
        self.assertEqual(code, 0, sub)
        run_cli(self.home, "wait", sub["task_id"], "--timeout", "30")
        return sub["task_id"]

    def test_unmigrated_ledger_refuses_notifier_commands(self) -> None:
        self.task("k-m0")
        code, out = run_cli(self.home, "notify", "flush")
        self.assertEqual(code, 6)  # no notifier.json yet: refused before anything else
        hook = FakeWebhook()
        try:
            write_config(self.home, http_config(self.tmp, hook.url))
            code, out = run_cli(self.home, "notify", "flush")
            self.assertEqual((code, out["error"]), (6, "not_migrated"))
            self.assertEqual(hook.requests, [])
        finally:
            hook.stop()

    def test_migrate_backs_up_then_notifies_only_new_events(self) -> None:
        old_task = self.task("k-m1")
        code, out = run_cli(self.home, "migrate")
        self.assertEqual(code, 0, out)
        self.assertTrue(Path(out["backup"]).is_file())
        backup = sqlite3.connect(out["backup"])
        self.assertEqual(backup.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        backup.close()
        self.assertFalse(run_cli(self.home, "migrate")[1]["migrated"], "idempotent")
        hook = FakeWebhook()
        try:
            write_config(self.home, http_config(self.tmp, hook.url))
            new_task = self.task("k-m2")
            wait_until(lambda: len(hook.requests) == 1, timeout=15)
            run_cli(self.home, "notify", "flush")
            sent = {b["cli_relay"]["events"][0]["task_id"] for b in hook.bodies()}
            self.assertEqual(sent, {new_task}, "the event from before the migration is not notified")
            self.assertNotIn(old_task, sent)
        finally:
            hook.stop()

    def test_backfill_includes_earlier_events(self) -> None:
        old_task = self.task("k-m3")
        self.assertEqual(run_cli(self.home, "migrate", "--backfill")[0], 0)
        hook = FakeWebhook()
        try:
            write_config(self.home, http_config(self.tmp, hook.url))
            run_cli(self.home, "notify", "flush")
            self.assertEqual([b["cli_relay"]["events"][0]["task_id"] for b in hook.bodies()], [old_task])
        finally:
            hook.stop()

    def test_v010_code_still_reads_a_migrated_ledger(self) -> None:
        old = self.tmp / "v010"
        tar = self.tmp / "v010.tar"
        made = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "-o", str(tar), "baff533"],
                              capture_output=True)
        if made.returncode != 0:
            self.skipTest("v0.1.0 (baff533) is not in this checkout's history")
        subprocess.run([sys.executable, "-m", "tarfile", "-e", str(tar), str(old)], check=True)
        task_id = self.task("k-m4")
        self.assertEqual(run_cli(self.home, "migrate")[0], 0)
        env = base_env({"PYTHONPATH": str(old)})
        p = subprocess.run([sys.executable, "-m", "cli_relay", "--home", str(self.home), "status", task_id],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["task"]["state"], "succeeded")
        p = subprocess.run([sys.executable, "-m", "cli_relay", "--home", str(self.home), "events", "--consumer", "c"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((p.returncode, len(json.loads(p.stdout)["events"])), (0, 1))


if __name__ == "__main__":
    unittest.main()

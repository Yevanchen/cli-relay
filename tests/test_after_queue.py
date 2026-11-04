"""Issue #21: dependency admission and scoped FIFO queues never start rejected/cancelled work."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys

from helpers import RelayTestCase, base_env, db_rows, side_effects, wait_until
from test_terminal_observations import notification

from cli_relay import procs
from cli_relay.ledger import FORMAT, SCHEMA, Ledger, payload_hash


class AfterAndQueue(RelayTestCase):
    def args(self, key, *, after=None, require=None, queue=None, scope="default", duration=0, result=None,
             exit_code=None):
        args = ["submit", "--key", key, "--scope", scope, "--engine", "fixture", "--cwd", str(self.work),
                "--prompt", "do the thing", "--option", f"duration_s={duration}",
                "--option", "side_effect_file=" + json.dumps(str(self.effects))]
        for flag, value in (("after", after), ("after-require", require), ("queue", queue)):
            if value is not None:
                args += ["--" + flag, value]
        for name, value in (("result", result), ("exit_code", exit_code)):
            if value is not None:
                args += ["--option", f"{name}={json.dumps(value)}"]
        return args

    def scheduled(self, key, *, env=None, expect=0, **kwargs):
        return self.cli(*self.args(key, **kwargs), env=env, expect=expect)

    def finish(self, sub, outcome="succeeded"):
        done = self.cli("wait", sub["task_id"], "--timeout", "25", "--interval", "0.1")
        self.assertFalse(done["timed_out"], done)
        self.assertEqual(done["task"]["state"], outcome, done)
        return done

    def waiting(self, sub):
        return wait_until(lambda: (lambda row: row if row["state"] == "waiting" and
                                  json.loads(row["evidence_json"] or "{}").get("waiting_on") else None)(
                                      self.attempt_row(sub["task_id"])))

    def assert_never_started(self, sub):
        row = self.attempt_row(sub["task_id"])
        for field in ("cli_pid", "spawn_started_at", "started_at"):
            self.assertIsNone(row[field], field)
        self.assertNotIn(row["attempt_id"], [e["attempt_id"] for e in side_effects(self.effects)])

    def assert_blocked(self, sub, dependency, *, status, outcome="succeeded", require="ok"):
        done = self.finish(sub, "blocked")
        expected = {"dependency_task_id": dependency["task_id"], "dependency_outcome": outcome,
                    "dependency_business_status": status, "require": require}
        event = next(e["body"] for e in done["events"] if e["kind"] == "terminal")
        for record in (done["attempts"][0]["evidence"], event,
                       notification(done)["cli_relay"]["events"][0]):
            self.assertEqual(record["reason_code"], "dependency_not_satisfied")
            self.assertEqual(record["details"], expected)
            self.assertIn(f"dependency {dependency['task_id']} business_status={status}", record["summary"])
            self.assertNotIn("\n", record["summary"])
        self.assertIn(f"dependency {dependency['task_id']} business_status={status}", notification(done)["summary"])
        self.assert_never_started(sub)
        return done

    def test_ok_dependency_runs_after_terminal_without_notify(self):
        dep = self.scheduled("zh", duration=0.6, result="status=done",
                             env=base_env({"CLI_RELAY_TEST_CRASH": "after_commit"}))
        sub = self.scheduled("en", after=dep["task_id"])
        self.waiting(sub)
        done = self.finish(sub)
        self.assertGreaterEqual(done["attempts"][0]["started_at"], self.attempt_row(dep["task_id"])["ended_at"])
        self.assertEqual(len(side_effects(self.effects)), 2)

    def test_partial_default_blocks_without_starting_engine(self):
        dep = self.scheduled("zh", result="WHAT_IS_DIFY_DONE status=partial ok=9 fail=4 skip=4")
        self.finish(dep)
        self.assert_blocked(self.scheduled("en", after=dep["task_id"]), dep, status="partial")

    def test_partial_opt_in_runs_and_key_reference_is_canonical(self):
        dep = self.scheduled("friday:zh", result="status=partial")
        self.finish(dep)
        sub = self.scheduled("en", after="friday:zh", require="ok_or_partial")
        self.assertEqual(sub["after"], dep["task_id"])
        self.finish(sub)
        duplicate = self.scheduled("en", after=dep["task_id"], require="ok_or_partial")
        self.assertEqual((duplicate["status"], duplicate["task_id"]), ("duplicate", sub["task_id"]))
        self.assertEqual(duplicate["payload_sha256"], sub["payload_sha256"])
        self.assertEqual(len(side_effects(self.effects)), 2)

    def test_failed_dependency_blocks_both_success_requirements(self):
        dep = self.scheduled("failed", exit_code=1, result="status=ok")
        self.finish(dep, "failed")
        for require in ("ok", "ok_or_partial"):
            with self.subTest(require=require):
                sub = self.scheduled(require, after=dep["task_id"], require=require)
                self.assert_blocked(sub, dep, status="failed", outcome="failed", require=require)

    def test_any_terminal_runs_after_failure(self):
        dep = self.scheduled("failed", duration=0.5, exit_code=1)
        sub = self.scheduled("after-failure", after="failed", require="any_terminal")
        self.waiting(sub)
        done = self.finish(sub)
        self.assertEqual(self.attempt_row(dep["task_id"])["outcome"], "failed")
        self.assertGreaterEqual(done["attempts"][0]["started_at"], self.attempt_row(dep["task_id"])["ended_at"])

    def test_approval_failed_and_unreported_business_statuses_block(self):
        for status in ("stopped_for_approval", "failed", "unreported"):
            with self.subTest(status=status):
                dep = self.scheduled("dep-" + status, result="status=" + status if status != "unreported" else "done")
                self.finish(dep)
                sub = self.scheduled("after-" + status, after=dep["task_id"], require="ok_or_partial")
                self.assert_blocked(sub, dep, status=status, require="ok_or_partial")

    def test_cancelled_and_blocked_dependencies_never_pass_success_gate(self):
        dep = self.scheduled("pending", duration=20)
        for outcome in ("cancelled", "blocked"):
            waiting = self.scheduled("dep-" + outcome, after=dep["task_id"])
            self.waiting(waiting)
            if outcome == "cancelled":
                self.cli("cancel", waiting["task_id"])
            else:
                row = self.attempt_row(waiting["task_id"])
                os.kill(row["keeper_pid"], signal.SIGKILL)
                wait_until(lambda: procs.same_process_alive(row["keeper_pid"], row["keeper_token"]) is False)
                self.cli("reconcile", waiting["task_id"])
            for require in ("ok", "ok_or_partial"):
                sub = self.scheduled(outcome + require, after=waiting["task_id"], require=require)
                self.assert_blocked(sub, waiting, status="failed", outcome=outcome, require=require)

    def test_unknown_ref_and_self_reference_reject_without_creating_tasks(self):
        for ref in ("missing", "t-" + "0" * 32, "self"):
            out = self.scheduled("self", after=ref, expect=6)
            self.assertEqual(out["error"], "dependency_not_found")
        self.assertEqual(db_rows(self.home, "SELECT COUNT(*) FROM tasks")[0][0], 0)
        self.assertEqual(db_rows(self.home, "SELECT COUNT(*) FROM submissions")[0][0], 0)

    def test_refs_are_scoped_for_both_key_and_task_id(self):
        dep = self.scheduled("zh", scope="one", result="status=ok")
        for ref in ("zh", dep["task_id"]):
            out = self.scheduled("en", scope="two", after=ref, expect=6)
            self.assertEqual(out["error"], "dependency_not_found")
        self.finish(self.scheduled("en", scope="one", after="zh"))

    def test_invalid_flags_and_queue_names_create_no_work(self):
        for flags in (("--after-require", "ok"), ("--after-require", "no"),
                      ("--after", "a", "--after", "b")):
            self.cli(*self.args("invalid"), *flags, expect=2)
        for name in ("", "a/b", "two words", "x" * 65, "queue\n", "队列"):
            with self.subTest(name=name):
                self.scheduled("invalid", queue=name, expect=2)
        self.assertEqual(db_rows(self.home, "SELECT COUNT(*) FROM tasks")[0][0], 0)
        self.finish(self.scheduled("valid", queue="A-Z_0.9-" + "x" * 56))

    def test_queue_serializes_fifo_even_when_first_keeper_is_delayed(self):
        first = self.scheduled("one", queue="shared", duration=0.2,
                               env=base_env({"CLI_RELAY_TEST_PAUSE": "after_claim:0.7"}))
        second = self.scheduled("two", queue="shared", duration=0.1)
        third = self.scheduled("three", queue="shared")
        self.waiting(second)
        self.waiting(third)
        for sub in (first, second, third):
            self.finish(sub)
        rows = [self.attempt_row(t["task_id"]) for t in (first, second, third)]
        for before, after in zip(rows, rows[1:]):
            self.assertGreaterEqual(after["started_at"], before["ended_at"])
        self.assertEqual([e["attempt_id"] for e in side_effects(self.effects)], [r["attempt_id"] for r in rows])

    def test_concurrent_queue_submits_serialize(self):
        children = [subprocess.Popen([sys.executable, "-m", "cli_relay", "--home", str(self.home),
                                      *self.args(f"parallel-{i}", queue="shared", duration=0.3)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=base_env())
                    for i in range(4)]
        submissions = []
        for child in children:
            out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, err)
            submissions.append(json.loads(out))
        for sub in submissions:
            self.finish(sub)
        rows = db_rows(self.home, "SELECT a.* FROM tasks t JOIN attempts a ON a.task_id=t.task_id "
                       "ORDER BY t.created_at, t.rowid")
        for before, after in zip(rows, rows[1:]):
            self.assertGreaterEqual(after["started_at"], before["ended_at"])

    def test_queue_is_isolated_by_scope_and_name(self):
        first = self.scheduled("one", queue="shared", duration=20)
        wait_until(lambda: self.attempt_row(first["task_id"])["cli_pid"])
        for kwargs in ({"queue": "other"}, {"queue": "shared", "scope": "other"}):
            sub = self.scheduled(str(kwargs), **kwargs)
            self.finish(sub)
        self.assertIsNone(self.attempt_row(first["task_id"])["ended_at"])

    def test_status_events_and_wait_show_dependency_and_queue_details(self):
        dep = self.scheduled("zh", duration=20)
        first = self.scheduled("queue-holder", queue="shared", duration=20)
        wait_until(lambda: self.attempt_row(first["task_id"])["cli_pid"])
        sub = self.scheduled("en", after=dep["task_id"], queue="shared")
        self.waiting(sub)
        done = self.cli("wait", sub["task_id"], "--timeout", "0")
        self.assertTrue(done["timed_out"])
        self.assertEqual(done["task"]["state"], "waiting")
        waiting = done["task"]["waiting_on"]
        self.assertEqual(waiting, done["attempts"][0]["waiting_on"])
        self.assertEqual(waiting["dependency"]["dependency_task_id"], dep["task_id"])
        self.assertEqual(waiting["dependency"]["require"], "ok")
        self.assertEqual(waiting["queue"], {"name": "shared", "position": 2, "ahead_task_ids": [first["task_id"]]})
        events = self.cli("events", "--consumer", "reader", "--task", sub["task_id"])["events"]
        self.assertEqual([e["kind"] for e in events], ["waiting"])
        self.assertEqual(events[0]["body"]["waiting_on"], waiting)
        self.assertTrue(done["attempts"][0]["keeper"]["alive"])
        self.assert_never_started(sub)
        self.cli("resume", sub["task_id"], "--prompt", "again", expect=6)

    def test_both_gates_must_pass_and_status_updates_queue_position(self):
        dep = self.scheduled("zh", duration=0.5, result="status=ok")
        first = self.scheduled("first", queue="shared", duration=20)
        sub = self.scheduled("en", after=dep["task_id"], queue="shared")
        self.waiting(sub)
        self.finish(dep)
        done = self.status(sub["task_id"])
        self.assertNotIn("dependency", done["task"]["waiting_on"])
        self.assertEqual(done["task"]["waiting_on"]["queue"]["position"], 2)
        self.assert_never_started(sub)
        self.cli("cancel", first["task_id"])
        self.finish(first, "cancelled")
        self.finish(sub)

    def test_cancel_waiting_never_starts_engine_even_after_gate_opens(self):
        first = self.scheduled("first", queue="shared", duration=20)
        wait_until(lambda: self.attempt_row(first["task_id"])["cli_pid"])
        sub = self.scheduled("cancel", queue="shared")
        self.waiting(sub)
        out = self.cli("cancel", sub["task_id"])
        self.assertEqual(out["result"], "cancelled_before_start")
        self.finish(sub, "cancelled")
        self.cli("cancel", first["task_id"])
        self.finish(first, "cancelled")
        later = self.scheduled("later", queue="shared")
        self.finish(later)
        self.assert_never_started(sub)

    def test_dead_waiting_keeper_is_blocked_without_recovery_spawn(self):
        dep = self.scheduled("zh", duration=20)
        sub = self.scheduled("en", after=dep["task_id"], env=base_env({"CLI_RELAY_TEST_CRASH": "while_waiting"}))
        row = self.waiting(sub)
        wait_until(lambda: procs.same_process_alive(row["keeper_pid"], row["keeper_token"]) is False)
        result = self.cli("reconcile")
        action = next(a for a in result["actions"] if a["task_id"] == sub["task_id"])
        self.assertEqual(action["action"], "blocked_waiting_keeper_lost")
        done = self.finish(sub, "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["reason_code"], "keeper_lost_while_waiting")
        self.assertEqual(done["events"][-1]["body"]["reason_code"], "keeper_lost_while_waiting")
        self.cli("reconcile", sub["task_id"])
        self.assertEqual(len(db_rows(self.home, "SELECT * FROM attempts WHERE task_id=?", (sub["task_id"],))), 1)
        self.assert_never_started(sub)

    def test_cancel_during_claim_does_not_enter_preflight_or_build(self):
        sub = self.scheduled("cancel-at-claim", queue="shared",
                             env=base_env({"CLI_RELAY_TEST_PAUSE": "after_claim:1"}))
        row = wait_until(lambda: (lambda a: a if a["state"] == "waiting" else None)(
            self.attempt_row(sub["task_id"])))
        self.cli("cancel", sub["task_id"])
        self.finish(sub, "cancelled")
        wait_until(lambda: procs.same_process_alive(row["keeper_pid"], row["keeper_token"]) is False)
        self.assertFalse((self.home / "runs" / sub["task_id"] / "1" / "prompt.txt").exists())
        self.assert_never_started(sub)

    def test_unknown_queue_holder_keeps_slot_but_any_terminal_dependency_can_run(self):
        ledger = Ledger(self.home)
        self.addCleanup(ledger.close)
        payload = {"engine": "fixture", "cwd": str(self.work), "prompt": "p", "options": {}, "queue": "shared"}
        _, task, _ = ledger.submit(scope="default", key="unknown", payload=payload, prompt="p", native_session_id=None)
        att = ledger.current_attempt(task["task_id"])
        ledger.mark_unknown(att["attempt_id"], from_states=("pending",), evidence={})
        queued = self.scheduled("queued", queue="shared")
        self.waiting(queued)
        self.finish(self.scheduled("after-unknown", after=task["task_id"], require="any_terminal"))
        self.assert_never_started(queued)
        self.assertEqual(self.status(queued["task_id"])["task"]["waiting_on"]["queue"]["ahead_task_ids"],
                         [task["task_id"]])
        ledger.finish(att["attempt_id"], from_states=("unknown",), outcome="failed")
        self.finish(queued)

    def test_new_options_are_idempotency_payload_only_when_supplied(self):
        sub = self.scheduled("plain")
        self.finish(sub)
        expected = {"engine": "fixture", "cwd": str(self.work.resolve()), "prompt": "do the thing",
                    "options": {"duration_s": 0, "side_effect_file": str(self.effects)}}
        self.assertEqual(sub["payload_sha256"], payload_hash(expected))
        self.assertEqual(json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0][0]), expected)
        self.assertEqual(self.scheduled("plain")["status"], "duplicate")
        dep = self.scheduled("dep", result="status=ok")
        for name, kwargs, changed in (("after", {"after": dep["task_id"]}, {"after": dep["task_id"], "require": "any_terminal"}),
                                      ("queue", {"queue": "one"}, {"queue": "two"})):
            queued = self.scheduled(name, **kwargs)
            self.assertEqual(self.scheduled(name, **kwargs)["status"], "duplicate")
            self.scheduled(name, **changed, expect=3)
            self.finish(queued)

    def test_resume_rejoins_queue_and_rechecks_dependency(self):
        dep = self.scheduled("dep", result="status=ok")
        sub = self.scheduled("en", after=dep["task_id"], queue="shared")
        self.finish(sub)
        holder = self.scheduled("holder", queue="shared", duration=20)
        wait_until(lambda: self.attempt_row(holder["task_id"])["cli_pid"])
        self.cli("resume", sub["task_id"], "--prompt", "continue")
        wait_until(lambda: self.attempt_row(sub["task_id"], 2)["state"] == "waiting")
        self.assertIsNone(self.attempt_row(sub["task_id"], 2)["cli_pid"])
        self.cli("cancel", holder["task_id"])
        self.finish(holder, "cancelled")
        self.finish(sub)

    def test_old_ledger_opens_and_accepts_dependencies_without_migration(self):
        self.home = self.tmp / "legacy"
        self.home.mkdir()
        conn = sqlite3.connect(str(self.home / "ledger.db"))
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('schema_version','1')")
        conn.commit()
        before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        conn.close()
        (self.home / "LEDGER.json").write_text(json.dumps({"format": FORMAT, "schema_version": 1, "ledger_id": "l-old"}))
        dep = self.scheduled("old", result="status=ok")
        done = self.finish(dep)
        ledger = Ledger(self.home)
        with ledger.tx() as c:
            # An old completed attempt has no business-status observation: fail closed, no reinterpretation.
            c.execute("UPDATE attempts SET evidence_json='{}'")
        ledger.close()
        sub = self.scheduled("new", after=dep["task_id"])
        self.assert_blocked(sub, dep, status="unreported")
        self.finish(self.scheduled("any", after=dep["task_id"], require="any_terminal"))
        self.assertEqual(self.status(dep["task_id"])["events"], done["events"])
        conn = sqlite3.connect(str(self.home / "ledger.db"))
        self.assertEqual(conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(), before)
        conn.close()

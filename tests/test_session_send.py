"""Issue #18: deliver a message as the next turn of an existing Claude Code / Codex session."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from helpers import (RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config, production_args,
                     wait_until)
from test_task_local_model_config import recording_cli
from test_terminal_observations import notification

from cli_relay import engines, procs
from cli_relay.errors import UsageError
from cli_relay.ledger import Ledger, init_ledger, payload_hash

THREAD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"  # the thread id of the recording fake Codex


def rollout_path(codex_home: Path, thread: str = THREAD) -> Path:
    return codex_home / "sessions/2026/10/19" / f"rollout-2026-10-19T00-00-00-{thread}.jsonl"


def write_rollout(codex_home: Path, *, provider="openai", turns=("read-only",), thread=THREAD, cwd="/srv/app"):
    path = rollout_path(codex_home, thread)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [] if provider is None else [{"type": "session_meta", "payload": {"id": thread, "cwd": cwd,
                                                                           "model_provider": provider}}]
    rows += [{"type": "turn_context", "payload": {"sandbox_policy": {"type": kind}, "active_permission_profile": None}}
             for kind in turns]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def write_claude_session(config_dir: Path, sid: str, cwd: str) -> Path:
    path = config_dir / "projects" / "-srv-app" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "sessionId": sid, "cwd": cwd}) + "\n")
    return path


class SessionHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-session-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.env = isolated_env(self.tmp)
        self.claude, self.codex = Path(self.env["CLAUDE_CONFIG_DIR"]), Path(self.env["CODEX_HOME"])
        self.fixture = self.tmp / "fixture-sessions"
        self.fixture.mkdir()
        self.sid = str(uuid.uuid4())

    def preflight(self, engine, sid=None, **kw):
        return engines.session_preflight(engine, self.env, sid or self.sid, fixture_state_dir=str(self.fixture), **kw)

    def test_only_exact_uuids_are_accepted_and_normalized(self):
        self.assertEqual(engines.validate_session_id(" " + self.sid.upper() + "\n"), self.sid)
        for value in ("", "latest", "--last", "*", self.sid + "*", "../" + self.sid, self.sid[:-1], self.sid + "0",
                      self.sid.replace("-", ""), "{" + self.sid + "}", None, 1, [self.sid]):
            with self.subTest(value=value), self.assertRaises(UsageError):
                engines.validate_session_id(value)

    def test_records_are_found_per_engine_and_missing_ones_are_named(self):
        for engine in ("claude", "codex", "fixture"):
            problem = self.preflight(engine)
            self.assertEqual((problem["reason"], problem["session_id"]), ("session_not_found", self.sid))
            self.assertIn(self.sid, problem["searched"])
            self.assertIn("does not exist on this machine", engines.session_problem_message(problem))
        write_claude_session(self.claude, self.sid, "/srv/app")
        write_rollout(self.codex, thread=self.sid)
        (self.fixture / f"{self.sid}.json").write_text('{"turns": []}')
        for engine in ("claude", "codex", "fixture"):
            self.assertIsNone(self.preflight(engine), engine)
            record = engines.session_record(engine, self.env, self.sid, fixture_state_dir=str(self.fixture))
            self.assertTrue(record.is_file())
        self.assertEqual(engines.session_cwd("claude", engines.session_record(
            "claude", self.env, self.sid, fixture_state_dir="")), "/srv/app")
        self.assertEqual(engines.session_cwd("codex", rollout_path(self.codex, self.sid)), "/srv/app")

    def test_a_live_claude_process_holding_the_session_is_busy(self):
        write_claude_session(self.claude, self.sid, "/srv/app")
        sessions = self.claude / "sessions"
        sessions.mkdir()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (sessions / "1.json").write_text("not json")
        (sessions / "2.json").write_text(json.dumps({"pid": dead.pid, "sessionId": self.sid}))
        (sessions / "3.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": str(uuid.uuid4())}))
        (sessions / "4.json").write_text(json.dumps({"pid": 0, "sessionId": self.sid}))
        (sessions / "5.json").write_text(json.dumps({"pid": str(os.getpid()), "sessionId": self.sid}))
        self.assertIsNone(self.preflight("claude"))
        (sessions / "6.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": self.sid, "kind": "interactive",
                                                     "entrypoint": "cli", "status": "idle", "cwd": "/private"}))
        problem = self.preflight("claude")
        self.assertEqual({k: problem[k] for k in ("reason", "holder", "pid", "kind", "entrypoint", "status")},
                         {"reason": "session_busy", "holder": "claude_process", "pid": os.getpid(),
                          "kind": "interactive", "entrypoint": "cli", "status": "idle"})
        self.assertNotIn("cwd", problem)
        self.assertIn("open in a live Claude Code process", engines.session_problem_message(problem))
        self.assertIsNone(self.preflight("claude", check_holder=False))

    def test_stale_claude_registrations_do_not_hold_the_session(self):
        write_claude_session(self.claude, self.sid, "/srv/app")
        sessions = self.claude / "sessions"
        sessions.mkdir()
        record = sessions / "1.json"
        started = procs.start_epoch(os.getpid())
        self.assertIsNotNone(started)
        self.assertLessEqual(started, time.time())
        for started_ms, holds in ((started * 1000, True), (time.time() * 1000, True), (None, True),
                                  ((started - 3600) * 1000, False), (True, True), (2 ** 64, True)):
            with self.subTest(started_ms=started_ms):
                # A pid that started long after the session registered is a reused pid, not Claude.
                record.write_text(json.dumps({"pid": os.getpid(), "sessionId": self.sid, "startedAt": started_ms}))
                self.assertEqual(self.preflight("claude") is not None, holds)
        for pid in (2 ** 32, 2 ** 31, -1, 1.5):  # malformed pids never raise and never hold
            with self.subTest(pid=pid):
                record.write_text(json.dumps({"pid": pid, "sessionId": self.sid}))
                self.assertIsNone(self.preflight("claude"))

    def test_a_shared_lock_is_a_reader_not_a_writer(self):
        write_rollout(self.codex, thread=self.sid)
        (self.codex / "thread-writer-locks").mkdir()
        lock = self.codex / "thread-writer-locks" / f"{self.sid}.lock"
        lock.write_text("")
        with open(lock) as reader:
            fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertIsNone(self.preflight("codex"))

    def test_malformed_session_records_never_raise(self):
        path = rollout_path(self.codex, self.sid)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": ["not", "a", "dict"]}) + "\n")
        self.assertIsNone(engines.session_cwd("codex", path))
        self.assertEqual(self.preflight("codex")["reason"], "provider_not_subscription")

    def test_a_held_codex_writer_lock_is_busy_and_a_free_one_is_released(self):
        write_rollout(self.codex, thread=self.sid)
        locks = self.codex / "thread-writer-locks"
        locks.mkdir()
        lock = locks / f"{self.sid}.lock"
        lock.write_text("")
        self.assertIsNone(self.preflight("codex"))
        with open(lock) as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the probe released it, so this succeeds
            problem = self.preflight("codex")
            self.assertEqual((problem["reason"], problem["holder"], problem["lock"]),
                             ("session_busy", "codex_thread_writer_lock", "held"))
            self.assertIn("has an active writer", engines.session_problem_message(problem))
        self.assertIsNone(self.preflight("codex"))
        self.assertTrue(lock.exists())

    def test_codex_thread_must_be_created_by_the_openai_provider(self):
        for provider, reason in (("other", "provider_not_subscription"), (None, "provider_unverified")):
            with self.subTest(provider=provider):
                write_rollout(self.codex, provider=provider, thread=self.sid)
                problem = self.preflight("codex")
                self.assertEqual((problem["reason"], problem["session_id"]), (reason, self.sid))
                self.assertIn("openai provider", engines.session_problem_message(problem))

    def test_permission_check_of_a_sent_turn_covers_only_its_own_turns(self):
        path = write_rollout(self.codex, turns=("workspace-write",), thread=self.sid)
        checkpoint = engines.codex_permission_checkpoint(self.env, self.sid)
        with path.open("a") as f:
            f.write(json.dumps({"type": "turn_context", "payload": {"sandbox_policy": {"type": "read-only"}}}) + "\n")

        def check(own):
            return engines.permission_violation("codex", self.env, self.sid, "read-only", resume_session_id=self.sid,
                                                permission_checkpoint=checkpoint, own_turns_only=own)
        self.assertEqual(check(False)["reason"], "sandbox_not_as_requested")  # unchanged for relay resumes
        self.assertIsNone(check(True))
        with path.open("a") as f:
            f.write(json.dumps({"type": "turn_context", "payload": {"sandbox_policy": {"type": "workspace-write"}}})
                    + "\n")
        self.assertEqual(check(True)["reason"], "sandbox_not_as_requested")

    def test_codex_active_writer_refusal_becomes_a_fixed_hint(self):
        stderr = self.tmp / "stderr.log"
        stderr.write_text("ERROR codex_core::session: Failed to create session: thread-store conflict: thread "
                          f"{self.sid} already has an active writer\n")
        warning = engines.codex_session_busy_warning(stderr)
        self.assertEqual(warning["code"], "session_busy")
        self.assertNotIn(self.sid, json.dumps(warning))
        stderr.write_text("other failure\n")
        self.assertIsNone(engines.codex_session_busy_warning(stderr))
        self.assertIsNone(engines.codex_session_busy_warning(self.tmp / "missing.log"))

    def test_request_details_name_the_target_only_for_sends(self):
        payload = {"engine": "fixture", "options": {}}
        self.assertNotIn("target_session_id", engines.request_details("fixture", payload))
        self.assertEqual(engines.request_details("fixture", {**payload, "target_session_id": self.sid})
                         ["target_session_id"], self.sid)


class SessionGate(unittest.TestCase):
    """The ledger admits one attempt per native session in a home, FIFO, across scopes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-gate-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        init_ledger(self.tmp / "home")
        self.ledger = Ledger(self.tmp / "home")
        self.addCleanup(self.ledger.close)
        self.sid = str(uuid.uuid4())

    def task(self, key, *, target=None, scope="default", engine="fixture", after=None):
        payload = {"engine": engine, "cwd": str(self.tmp), "prompt": key, "options": {},
                   **({"target_session_id": target} if target else {}), **({"after": after} if after else {})}
        _, task, _ = self.ledger.submit(scope=scope, key=key, payload=payload, prompt=key,
                                        native_session_id=str(uuid.uuid4()))
        return task["task_id"]

    def set_state(self, task_id, state, evidence=None):
        self.ledger.conn.execute("UPDATE attempts SET state=?, evidence_json=? WHERE task_id=?",
                                 (state, json.dumps(evidence) if evidence else None, task_id))

    def gate(self, task_id):
        return self.ledger.waiting_on(task_id).get("session")

    def test_holders_and_earlier_waiters_gate_a_send_fifo(self):
        holder = self.task("holder")
        first = self.task("first", target=self.sid, scope="a")
        second = self.task("second", target=self.sid, scope="a", after=first)  # the only way to queue at submit
        self.ledger.conn.execute("UPDATE attempts SET native_session_id=? WHERE task_id=?", (self.sid, holder))
        self.assertIsNone(self.gate(holder))  # not a send: a first run is never gated by a session
        self.assertEqual(self.gate(first), {"session_id": self.sid, "holder_task_ids": [], "ahead_task_ids": [holder]})
        self.assertEqual(self.gate(second)["ahead_task_ids"], [holder, first])
        for state in ("claimed", "running", "unknown"):
            with self.subTest(state=state):
                self.set_state(holder, state)
                self.assertEqual(self.gate(first),
                                 {"session_id": self.sid, "holder_task_ids": [holder], "ahead_task_ids": []})
        self.set_state(holder, "unknown", {"processes_stopped": True})
        self.assertIsNone(self.gate(first))
        self.set_state(holder, "finished")
        self.assertIsNone(self.gate(first))
        self.assertEqual(self.gate(second), {"session_id": self.sid, "holder_task_ids": [], "ahead_task_ids": [first]})
        self.set_state(first, "running")
        self.assertEqual(self.gate(second), {"session_id": self.sid, "holder_task_ids": [first], "ahead_task_ids": []})
        self.set_state(first, "finished")
        self.assertIsNone(self.gate(second))
        self.assertEqual(self.ledger.session_users("fixture", self.sid),
                         [{"task_id": second, "attempt_no": 1, "state": "pending"}])
        self.assertEqual(self.ledger.session_users("fixture", self.sid, exclude_task=second), [])
        self.assertEqual(self.ledger.session_users("claude", self.sid), [])  # ids are per engine

    def test_submit_and_resume_repeat_the_check_inside_their_transaction(self):
        from cli_relay.errors import Refused

        first = self.task("first", target=self.sid)
        with self.assertRaises(Refused) as caught:
            self.task("second", target=self.sid, scope="other")
        self.assertEqual((caught.exception.code, caught.exception.detail["holder_task_ids"]), ("session_busy", [first]))
        queued = self.ledger.submit(scope="default", key="queued", payload={
            "engine": "fixture", "cwd": str(self.tmp), "prompt": "q", "options": {}, "target_session_id": self.sid,
            "after": first}, prompt="q", native_session_id=None)[1]["task_id"]
        owner = self.task("owner")
        self.ledger.conn.execute("UPDATE attempts SET state='finished', native_session_id=? WHERE task_id=?",
                                 (self.sid, owner))
        with self.assertRaises(Refused) as caught:
            self.ledger.new_attempt(owner, expect_attempt_no=1, reason="resume", prompt="x",
                                    resume_session_id=self.sid, native_session_id=self.sid)
        self.assertEqual(caught.exception.detail["holder_task_ids"], [first, queued])
        self.assertEqual(self.ledger.get_task(owner)["current_attempt"], 1)  # rolled back, nothing created

    def test_send_attempt_records_and_hash_only_change_with_the_target(self):
        plain = self.task("plain")
        send = self.task("send", target=self.sid)
        rows = {r["task_id"]: r for r in db_rows(self.tmp / "home", "SELECT * FROM attempts")}
        self.assertEqual((rows[plain]["reason"], rows[plain]["resume_session_id"]), ("initial", None))
        self.assertEqual((rows[send]["reason"], rows[send]["resume_session_id"], rows[send]["native_session_id"]),
                         ("send", self.sid, self.sid))
        payload = {"engine": "fixture", "cwd": str(self.tmp), "prompt": "plain", "options": {}}
        self.assertEqual(self.ledger.get_task(plain)["payload_sha256"], payload_hash(payload))


class SessionSendRoundTrip(RelayTestCase):
    def send(self, key, sid, *extra, env=None, expect=0):
        return self.cli("submit", "--key", key, "--engine", "fixture", "--cwd", str(self.work), "--session", sid,
                        "--prompt", f"message {key}", *extra, env=env, expect=expect)

    def first_session(self, duration=0.1):
        sub = self.submit("first", prompt="remember APPLE", duration=duration)
        return sub, sub["native_session_id"]

    def test_message_runs_as_the_next_turn_and_flows_through_events_and_ack(self):
        first, sid = self.first_session()
        self.cli("wait", first["task_id"], "--timeout", "30")
        sub = self.send("send-1", sid.upper())
        self.assertEqual((sub["status"], sub["target_session_id"], sub["native_session_id"]), ("created", sid, sid))
        self.assertNotIn("target_session_id", first)
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        attempt = done["attempts"][0]
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual((attempt["reason"], attempt["resume_session_id"], attempt["native_session_id"]),
                         ("send", sid, sid))
        self.assertEqual(attempt["native"]["result"], "resumed turn 2; previous prompt: remember APPLE")
        self.assertEqual(done["task"]["target_session_id"], sid)
        body = done["events"][-1]["body"]
        self.assertEqual((body["kind"], body["target_session_id"], body["native_session_id"]), ("terminal", sid, sid))
        self.assertEqual(notification(done)["cli_relay"]["events"][0]["target_session_id"], sid)
        event = self.cli("events", "--consumer", "bot", "--task", sub["task_id"])["events"][-1]
        self.assertEqual(event["body"]["target_session_id"], sid)
        self.cli("ack", "--consumer", "bot", "--task", sub["task_id"], "--attempt", "1", "--event", event["event_id"])
        self.assertEqual(self.cli("events", "--consumer", "bot", "--task", sub["task_id"])["events"], [])
        self.assertEqual(self.cli("brief", "--consumer", "other", "--task", sub["task_id"])["events"][0]
                         ["target_session_id"], sid)
        # Same key and session: the same task; another session under that key: a conflict.
        self.assertEqual(self.send("send-1", sid)["status"], "duplicate")
        other = self.submit("other")
        self.cli("wait", other["task_id"], "--timeout", "30")
        self.send("send-1", other["native_session_id"], expect=3)
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks WHERE task_id=?",
                                    (sub["task_id"],))[0]["payload_json"])
        self.assertEqual(stored["target_session_id"], sid)
        self.assertEqual(sub["payload_sha256"], payload_hash(stored))
        # The send task resumes like any task; a fresh rerun is refused.
        self.cli("resume", sub["task_id"], "--prompt", "third")
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["attempts"][-1]["native"]["result"], "resumed turn 3; previous prompt: message send-1")
        refused = self.cli("resume", sub["task_id"], "--fresh", "--confirm-rerun", expect=6)
        self.assertEqual(refused["error"], "fresh_not_supported")

    def test_missing_and_malformed_sessions_create_nothing(self):
        out = self.send("missing", str(uuid.uuid4()), expect=6)
        self.assertEqual(out["error"], "session_not_found")
        self.assertIn("fixture-sessions", out["detail"]["searched"])
        for bad in ("latest", "--last", "*", "../x"):
            self.assertEqual(self.send("bad", bad, expect=2)["error"], "usage")
        self.assertEqual(db_rows(self.home, "SELECT task_id FROM tasks"), [])

    def test_a_busy_session_is_refused_unless_queued_behind_its_holder(self):
        first, sid = self.first_session()
        self.cli("wait", first["task_id"], "--timeout", "30")
        # A long holder, ended by a cancel, so a slow runner cannot finish it before the checks below.
        holder = self.send("holder", sid, "--option", "duration_s=60")
        wait_until(lambda: self.status(holder["task_id"])["task"]["state"] == "running")
        busy = self.send("busy", sid, expect=6)
        self.assertEqual((busy["error"], busy["detail"]["holder_task_ids"]), ("session_busy", [holder["task_id"]]))
        self.assertIn("--after <task id> --after-require any_terminal", busy["message"])
        resumed = self.cli("resume", first["task_id"], "--prompt", "x", expect=6)
        self.assertEqual((resumed["error"], resumed["detail"]["holder_task_ids"]), ("session_busy", [holder["task_id"]]))
        queued = self.send("queued", sid, "--after", holder["task_id"], "--after-require", "any_terminal")
        wait_until(lambda: self.status(queued["task_id"])["task"]["state"] == "waiting")
        gate = self.status(queued["task_id"])["task"]["waiting_on"]["session"]
        self.assertEqual(gate, {"session_id": sid, "holder_task_ids": [holder["task_id"]], "ahead_task_ids": []})
        # A second message must queue behind the first queued one, not behind the holder.
        self.assertEqual(self.send("second", sid, "--after", holder["task_id"], expect=6)["error"], "session_busy")
        self.cli("cancel", holder["task_id"])
        done = self.cli("wait", queued["task_id"], "--timeout", "60")
        held = self.status(holder["task_id"])
        self.assertEqual((held["task"]["state"], done["task"]["state"]), ("cancelled", "succeeded"))
        self.assertTrue(done["attempts"][0]["native"]["result"].startswith("resumed turn "))
        self.assertLess(held["attempts"][0]["ended_at"], done["attempts"][0]["started_at"])
        self.assertEqual(len(done["events"]), 2)  # one waiting snapshot, one terminal event

    def test_the_gate_alone_keeps_two_attempts_off_one_session(self):
        # A send created behind the CLI's back (as a lost race would) waits for the session, not
        # for a dependency: only the admission gate orders it.
        first, sid = self.first_session()
        self.cli("wait", first["task_id"], "--timeout", "30")
        holder = self.send("holder", sid, "--option", "duration_s=60")
        wait_until(lambda: self.status(holder["task_id"])["task"]["state"] == "running")
        ledger = Ledger(self.home)
        try:
            payload = {"engine": "fixture", "cwd": str(self.work.resolve()), "prompt": "raced", "options": {},
                       "target_session_id": sid}
            ledger.conn.execute("UPDATE attempts SET state='finished' WHERE task_id=?", (holder["task_id"],))
            _, raced, _ = ledger.submit(scope="other", key="raced", payload=payload, prompt="raced",
                                        native_session_id=None)  # the check passes: no holder in this instant
            ledger.conn.execute("UPDATE attempts SET state='running' WHERE task_id=?", (holder["task_id"],))
        finally:
            ledger.close()
        from cli_relay.spawn import spawn_keeper
        spawn_keeper(self.home, raced["task_id"], db_rows(self.home, "SELECT attempt_id FROM attempts WHERE task_id=?",
                                                          (raced["task_id"],))[0]["attempt_id"])
        wait_until(lambda: self.status(raced["task_id"])["task"]["state"] == "waiting")
        gate = self.status(raced["task_id"])["task"]["waiting_on"]
        self.assertEqual(set(gate), {"session"})
        self.assertEqual(gate["session"]["holder_task_ids"], [holder["task_id"]])
        self.cli("cancel", holder["task_id"])
        done = self.cli("wait", raced["task_id"], "--timeout", "60")
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertLess(self.status(holder["task_id"])["attempts"][0]["ended_at"], done["attempts"][0]["started_at"])

    def test_session_gone_before_the_keeper_runs_is_blocked_with_a_reason(self):
        first, sid = self.first_session()
        self.cli("wait", first["task_id"], "--timeout", "30")
        env = base_env({"CLI_RELAY_TEST_PAUSE": "after_claim:2"})
        sub = self.send("gone", sid, env=env)
        (self.home / "fixture-sessions" / f"{sid}.json").unlink()
        done = self.cli("wait", sub["task_id"], "--timeout", "30")
        self.assertEqual(done["task"]["state"], "blocked")
        evidence, body = done["attempts"][0]["evidence"], done["events"][-1]["body"]
        for record in (evidence, body):
            self.assertEqual((record["preflight"]["reason"], record["reason_code"]),
                             ("session_not_found", "session_not_found"))
            self.assertIn("does not exist on this machine", record["summary"])
        self.assertEqual(body["target_session_id"], sid)
        self.assertIn(f"{sub['task_id']}#1 session {sid} does not exist", notification(done)["summary"])
        self.assertEqual(done["attempts"][0]["cli"]["pid"], None)  # the CLI never started


@needs_clean_cli_config
class SessionSendRealAdapters(RelayTestCase):
    """Fake Claude and Codex CLIs (no model calls) behind the real preflight, keeper and checks."""

    def fake_env(self, engine, **extra):
        fake = recording_cli(self.tmp, engine)
        return base_env(isolated_env(self.tmp, {f"CLI_RELAY_{engine.upper()}_BIN": str(fake), **extra}))

    def send(self, engine, key, sid, env, *extra, expect=0):
        return self.cli("submit", *production_args(engine), "--key", key, "--engine", engine, "--cwd", str(self.work),
                        "--session", sid, "--prompt", "follow up", *extra, env=env, expect=expect)

    def calls(self):
        return [json.loads(line) for line in (self.tmp / "calls.jsonl").read_text().splitlines()]

    def test_claude_send_resumes_the_exact_session_and_refuses_an_open_one(self):
        env = self.fake_env("claude")
        config = Path(env["CLAUDE_CONFIG_DIR"])
        sid = str(uuid.uuid4())
        self.assertEqual(self.send("claude", "missing", sid, env, expect=6)["error"], "session_not_found")
        write_claude_session(config, sid, "/somewhere/else")
        (config / "sessions").mkdir()
        record = config / "sessions" / f"{os.getpid()}.json"
        record.write_text(json.dumps({"pid": os.getpid(), "sessionId": sid, "kind": "interactive", "status": "busy"}))
        busy = self.send("claude", "open", sid, env, expect=6)
        self.assertEqual((busy["error"], busy["detail"]["holder"], busy["detail"]["pid"]),
                         ("session_busy", "claude_process", os.getpid()))
        record.unlink()
        sub = self.send("claude", "send", sid, env)
        self.assertEqual([w["code"] for w in sub["warnings"]], ["session_cwd_differs"])
        self.assertEqual(sub["warnings"][0]["session_cwd"], "/somewhere/else")
        done = self.cli("wait", sub["task_id"], "--timeout", "30", env=env)
        self.assertEqual(done["task"]["state"], "succeeded")
        argv, = self.calls()
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(done["events"][-1]["body"]["target_session_id"], sid)

    def test_codex_send_checks_provider_lock_and_only_its_own_turns(self):
        env = self.fake_env("codex")
        codex_home = Path(env["CODEX_HOME"])
        write_rollout(codex_home, provider="other")
        self.assertEqual(self.send("codex", "foreign", THREAD, env, expect=6)["error"], "provider_not_subscription")
        # A thread a user ran in workspace-write; the relay's read-only turn is what it certifies.
        write_rollout(codex_home, turns=("workspace-write",), cwd=str(self.work.resolve()))
        locks = codex_home / "thread-writer-locks"
        locks.mkdir()
        (locks / f"{THREAD}.lock").write_text("")
        with open(locks / f"{THREAD}.lock") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            busy = self.send("codex", "locked", THREAD, env, expect=6)
            self.assertEqual((busy["error"], busy["detail"]["holder"]), ("session_busy", "codex_thread_writer_lock"))
        sub = self.send("codex", "send", THREAD, env)
        self.assertNotIn("warnings", sub)
        done = self.cli("wait", sub["task_id"], "--timeout", "30", env=env)
        self.assertEqual(done["task"]["state"], "succeeded", done["attempts"][0]["evidence"])
        argv, = self.calls()
        self.assertEqual(argv[argv.index("resume"):][-2:], [THREAD, "-"])
        evidence = done["attempts"][0]["evidence"]
        self.assertGreater(evidence["codex_permission_checkpoint"]["offset"], 0)
        self.assertEqual(done["events"][-1]["body"]["target_session_id"], THREAD)

    def test_keeper_rechecks_a_live_claude_process_before_the_turn(self):
        env = self.fake_env("claude", CLI_RELAY_TEST_PAUSE="after_claim:2")
        config = Path(env["CLAUDE_CONFIG_DIR"])
        sid = str(uuid.uuid4())
        write_claude_session(config, sid, str(self.work.resolve()))
        sub = self.send("claude", "late-open", sid, env)
        (config / "sessions").mkdir()
        (config / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": sid}))
        done = self.cli("wait", sub["task_id"], "--timeout", "30", env=env)
        self.assertEqual(done["task"]["state"], "blocked")
        body = done["events"][-1]["body"]
        self.assertEqual((body["preflight"]["reason"], body["preflight"]["holder"], body["reason_code"]),
                         ("session_busy", "claude_process", "session_busy"))
        self.assertFalse((self.tmp / "calls.jsonl").exists())  # the CLI never started
        # resume of that send refuses the open session up front, with the same reason.
        refused = self.cli("resume", sub["task_id"], "--prompt", "again", env=env, expect=6)
        self.assertEqual((refused["error"], refused["detail"]["holder"]), ("session_busy", "claude_process"))

    def test_codex_send_cannot_widen_a_relay_tasks_own_thread(self):
        env = self.fake_env("codex")
        owner = self.cli("submit", *production_args("codex"), "--key", "owner", "--engine", "codex",
                         "--cwd", str(self.work), "--prompt", "start", env=env)
        self.assertEqual(self.cli("wait", owner["task_id"], "--timeout", "30", env=env)["task"]["state"], "succeeded")
        wider = self.send("codex", "wider", THREAD, env, "--option", 'sandbox="workspace-write"', expect=6)
        self.assertEqual((wider["error"], wider["detail"]["owner_task_id"], wider["detail"]["sandbox"]),
                         ("session_permissions_wider", owner["task_id"], "workspace-write"))
        self.assertIn("would make that task's later resumes fail", wider["message"])
        same = self.send("codex", "same", THREAD, env)  # read-only, like the owner
        self.assertEqual(self.cli("wait", same["task_id"], "--timeout", "30", env=env)["task"]["state"], "succeeded")
        self.cli("resume", owner["task_id"], "--prompt", "continue", env=env)  # the owner's thread is still clean
        self.assertEqual(self.cli("wait", owner["task_id"], "--timeout", "30", env=env)["task"]["state"], "succeeded")
        for key, extra in (("full", ['sandbox="danger-full-access"', "allow_full_access=true"]),):
            refused = self.send("codex", key, THREAD, env, *[a for opt in extra for a in ("--option", opt)], expect=6)
            self.assertEqual(refused["error"], "session_permissions_wider")

    def test_codex_refusing_a_second_writer_is_reported_as_session_busy(self):
        fake = self.tmp / "codex"
        fake.write_text(f"#!{sys.executable}\nimport sys\n"
                        "if sys.argv[1] == 'login':\n    print('Logged in using ChatGPT'); sys.exit(0)\n"
                        f"sys.stderr.write('Error: thread/resume failed: thread {THREAD} already has an active writer"
                        " (code -32600)\\n'); sys.exit(1)\n")
        fake.chmod(0o755)
        env = base_env(isolated_env(self.tmp, {"CLI_RELAY_CODEX_BIN": str(fake)}))
        write_rollout(Path(env["CODEX_HOME"]))
        done = self.cli("wait", self.send("codex", "race", THREAD, env)["task_id"], "--timeout", "30", env=env)
        # The refused resume wrote no turn of its own, so its permissions are unverified: blocked.
        self.assertEqual(done["task"]["state"], "blocked")
        self.assertEqual(done["attempts"][0]["evidence"]["permission_check"]["missing"], "current_turn_context")
        codes = [w["code"] for w in done["events"][-1]["body"]["warnings"]]
        self.assertIn("session_busy", codes)
        self.assertIn("warnings=session_busy", notification(done)["summary"])


if __name__ == "__main__":
    unittest.main()

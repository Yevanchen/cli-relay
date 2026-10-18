"""SQLite ledger: tasks, attempts, events, acks.

Rules enforced here:
* every write runs in ``BEGIN IMMEDIATE`` (one writer at a time across processes);
* every attempt state change is a compare-and-set on the expected previous state;
* a terminal (or unknown) state and its event row are committed in one transaction;
* nothing is ever deleted (no TTL, no GC).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .errors import (
    AckMismatch,
    Conflict,
    InsufficientDisk,
    LedgerCorrupt,
    LedgerError,
    LedgerMissing,
    LedgerNotInitialized,
    LedgerWriteFailed,
    NotFound,
    Refused,
)

FORMAT = "cli-relay-ledger"
SCHEMA_VERSION = 1
MARKER = "LEDGER.json"
DB_NAME = "ledger.db"
DEFAULT_MIN_FREE_BYTES = 64 * 1024 * 1024
RESULT_EXCERPT_CHARS = 2000

ACTIVE_STATES = ("pending", "claimed", "running")
_TASK_STATE = {"pending": "accepted", "claimed": "running", "running": "running", "unknown": "unknown"}

SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  request_key TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  engine TEXT NOT NULL,
  cwd TEXT NOT NULL,
  state TEXT NOT NULL,
  current_attempt INTEGER NOT NULL,
  submit_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (scope, request_key)
);
CREATE TABLE attempts (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  attempt_no INTEGER NOT NULL,
  reason TEXT NOT NULL,
  state TEXT NOT NULL,
  outcome TEXT,
  prompt TEXT NOT NULL,
  resume_session_id TEXT,
  native_session_id TEXT,
  host TEXT,
  cwd TEXT NOT NULL,
  keeper_pid INTEGER,
  keeper_token TEXT,
  keeper_pgid INTEGER,
  cli_pid INTEGER,
  cli_token TEXT,
  spawn_started_at TEXT,
  exit_code INTEGER,
  exit_signal INTEGER,
  exit_known INTEGER NOT NULL DEFAULT 0,
  native_result_json TEXT,
  evidence_json TEXT,
  cancel_requested_at TEXT,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  started_at TEXT,
  ended_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE (task_id, attempt_no)
);
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  attempt_no INTEGER NOT NULL,
  kind TEXT NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (task_id, attempt_no, kind)
);
CREATE TABLE acks (
  event_id TEXT NOT NULL REFERENCES events(event_id),
  consumer TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  acked_at TEXT NOT NULL,
  PRIMARY KEY (event_id, consumer)
);
CREATE TABLE deliveries (
  event_id TEXT NOT NULL,
  consumer TEXT NOT NULL,
  count INTEGER NOT NULL,
  first_at TEXT NOT NULL,
  last_at TEXT NOT NULL,
  PRIMARY KEY (event_id, consumer)
);
CREATE TABLE submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  scope TEXT NOT NULL,
  request_key TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  result TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX attempts_by_state ON attempts (state);
CREATE INDEX events_by_task ON events (task_id, attempt_no);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_age_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        then = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def task_state_for(attempt_state: str, outcome: Optional[str]) -> str:
    if attempt_state == "finished":
        return outcome or "failed"
    return _TASK_STATE[attempt_state]


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write via temp file + fsync + rename + directory fsync. ``os.write`` may write fewer bytes
    than asked (quota, nearly full disk, signals), so it is repeated until everything is written;
    only a complete temp file is renamed into place."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        data = memoryview(text.encode("utf-8"))
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError(errno.EIO, "write made no progress", str(tmp))
            data = data[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        raise
    os.close(fd)
    os.replace(str(tmp), str(path))
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _mapped(exc: sqlite3.Error) -> LedgerError:
    text = str(exc)
    low = text.lower()
    if "not a database" in low or "malformed" in low or "corrupt" in low:
        return LedgerCorrupt("ledger database is corrupt", sqlite=text)
    return LedgerWriteFailed("ledger write failed", sqlite=text)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(r) if r is not None else None


def _merge_json(old: Optional[str], new: Optional[Dict[str, Any]]) -> Optional[str]:
    if not new:
        return old
    merged: Dict[str, Any] = {}
    if old:
        try:
            merged = json.loads(old)
        except ValueError:
            merged = {"_previous_unparsable": old}
    merged.update(new)
    return canonical_json(merged)


def init_ledger(home: Path) -> Dict[str, Any]:
    """Create a ledger in ``home``. Refuses to adopt a database without a marker and
    refuses to silently recreate a missing database next to an existing marker."""
    home = Path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker, db = home / MARKER, home / DB_NAME
    if marker.exists():
        if not db.exists():
            raise LedgerMissing("ledger marker exists but the database is missing; refusing to recreate it",
                                home=str(home))
        Ledger(home, integrity=True).close()
        return {"created": False, "home": str(home)}
    if db.exists():
        raise LedgerError("a database exists without a ledger marker; refusing to adopt it", home=str(home))
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # executescript() commits any open transaction first, so the transaction lives in the script.
        conn.executescript(
            "BEGIN IMMEDIATE;\n" + SCHEMA
            + f"\nINSERT INTO meta (k, v) VALUES ('schema_version', '{SCHEMA_VERSION}');"
            + f"\nINSERT INTO meta (k, v) VALUES ('created_at', '{now_iso()}');\nCOMMIT;\n")
    except sqlite3.Error as exc:
        _rollback(conn)
        raise _mapped(exc) from exc
    finally:
        conn.close()
    os.chmod(str(db), 0o600)
    atomic_write_text(marker, canonical_json({"format": FORMAT, "schema_version": SCHEMA_VERSION,
                                              "ledger_id": new_id("l"), "created_at": now_iso()}) + "\n")
    return {"created": True, "home": str(home)}


class Ledger:
    def __init__(self, home: Path, *, integrity: bool = False) -> None:
        self.home = Path(home)
        marker = self.home / MARKER
        if not marker.exists():
            raise LedgerNotInitialized("no ledger here; run `cli-relay init` first", home=str(self.home))
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LedgerCorrupt("ledger marker is unreadable", error=str(exc)) from exc
        if meta.get("format") != FORMAT:
            raise LedgerCorrupt("ledger marker has an unexpected format")
        self.db_path = self.home / DB_NAME
        if not self.db_path.exists():
            raise LedgerMissing("ledger database is missing; refusing to create an empty one", home=str(self.home))
        try:
            self.conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            row = self.conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        except sqlite3.Error as exc:
            mapped = _mapped(exc)
            if isinstance(mapped, LedgerCorrupt):
                raise mapped from exc
            raise LedgerError("ledger database cannot be opened", sqlite=str(exc)) from exc
        if row is None or str(row[0]) != str(SCHEMA_VERSION):
            raise LedgerCorrupt("ledger schema version mismatch", found=None if row is None else row[0])
        if integrity:
            self.check_integrity()

    # ------------------------------------------------------------------ plumbing
    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def check_integrity(self) -> None:
        try:
            rows = self.conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.Error as exc:
            raise LedgerCorrupt("integrity check could not run", sqlite=str(exc)) from exc
        if not rows or rows[0][0] != "ok":
            raise LedgerCorrupt("integrity check failed", result=[r[0] for r in rows[:5]])

    def check_disk(self) -> None:
        try:
            min_free = int(os.environ.get("CLI_RELAY_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES))
        except ValueError:
            min_free = DEFAULT_MIN_FREE_BYTES
        free = shutil.disk_usage(str(self.home)).free
        if free < min_free:
            raise InsufficientDisk("free disk space is below the threshold; refusing new work",
                                   free_bytes=free, min_free_bytes=min_free)

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise _mapped(exc) from exc
        try:
            yield conn
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(conn)
            raise _mapped(exc) from exc
        except BaseException:
            _rollback(conn)
            raise

    def run_dir(self, task_id: str, attempt_no: int) -> Path:
        return self.home / "runs" / task_id / str(attempt_no)

    # ------------------------------------------------------------------ reads
    def get_task(self, task_id: str) -> Dict[str, Any]:
        row = _row(self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
        if row is None:
            raise NotFound("no such task", task_id=task_id)
        return row

    def find_task(self, scope: str, key: str) -> Dict[str, Any]:
        row = _row(self.conn.execute("SELECT * FROM tasks WHERE scope=? AND request_key=?",
                                     (scope, key)).fetchone())
        if row is None:
            raise NotFound("no task for this key", scope=scope, key=key)
        return row

    def attempts(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no", (task_id,))
        return [dict(r) for r in rows.fetchall()]

    def get_attempt(self, attempt_id: str) -> Dict[str, Any]:
        row = _row(self.conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone())
        if row is None:
            raise NotFound("no such attempt", attempt_id=attempt_id)
        return row

    def current_attempt(self, task_id: str) -> Dict[str, Any]:
        task = self.get_task(task_id)
        row = _row(self.conn.execute("SELECT * FROM attempts WHERE task_id=? AND attempt_no=?",
                                     (task_id, task["current_attempt"])).fetchone())
        assert row is not None, "task without its current attempt"
        return row

    def open_attempts(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM attempts WHERE state IN ('pending','claimed','running','unknown') "
            "ORDER BY created_at")
        return [dict(r) for r in rows.fetchall()]

    def events_of_task(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY created_at, event_id", (task_id,))
        out = []
        for r in rows.fetchall():
            ev = dict(r)
            ev["acked_by"] = [a[0] for a in self.conn.execute(
                "SELECT consumer FROM acks WHERE event_id=? ORDER BY consumer", (ev["event_id"],)).fetchall()]
            out.append(ev)
        return out

    # ------------------------------------------------------------------ submit
    def submit(self, *, scope: str, key: str, payload: Dict[str, Any], prompt: str,
               native_session_id: Optional[str]) -> Tuple[str, Dict[str, Any], str]:
        """Returns (status, task, payload_sha256) with status in created|duplicate|conflict.
        The caller may only report "accepted" after this returns (i.e. after COMMIT)."""
        sha = payload_hash(payload)
        now = now_iso()
        with self.tx() as c:
            existing = c.execute("SELECT * FROM tasks WHERE scope=? AND request_key=?", (scope, key)).fetchone()
            if existing is not None:
                status = "duplicate" if existing["payload_sha256"] == sha else "conflict"
                c.execute("INSERT INTO submissions (task_id, scope, request_key, payload_sha256, result, at) "
                          "VALUES (?,?,?,?,?,?)", (existing["task_id"], scope, key, sha, status, now))
                if status == "duplicate":
                    c.execute("UPDATE tasks SET submit_count=submit_count+1, updated_at=? WHERE task_id=?",
                              (now, existing["task_id"]))
                task_id = existing["task_id"]
            else:
                status = "created"
                task_id, attempt_id = new_id("t"), new_id("a")
                c.execute(
                    "INSERT INTO tasks (task_id, scope, request_key, payload_sha256, payload_json, engine, cwd, "
                    "state, current_attempt, submit_count, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,'accepted',1,1,?,?)",
                    (task_id, scope, key, sha, canonical_json(payload), payload["engine"], payload["cwd"], now, now))
                c.execute(
                    "INSERT INTO attempts (attempt_id, task_id, attempt_no, reason, state, prompt, native_session_id, "
                    "cwd, created_at, updated_at) VALUES (?,?,1,'initial','pending',?,?,?,?,?)",
                    (attempt_id, task_id, prompt, native_session_id, payload["cwd"], now, now))
                c.execute("INSERT INTO submissions (task_id, scope, request_key, payload_sha256, result, at) "
                          "VALUES (?,?,?,?,?,?)", (task_id, scope, key, sha, status, now))
        task = self.get_task(task_id)
        if status == "conflict":
            raise Conflict("this key is already bound to a different payload",
                           task_id=task_id, scope=scope, key=key,
                           existing_payload_sha256=task["payload_sha256"], submitted_payload_sha256=sha)
        return status, task, sha

    # ------------------------------------------------------------------ keeper transitions (CAS)
    def claim(self, attempt_id: str, *, keeper_pid: int, keeper_token: Optional[str], keeper_pgid: int,
              host: str) -> bool:
        now = now_iso()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE attempts SET state='claimed', keeper_pid=?, keeper_token=?, keeper_pgid=?, host=?, "
                "claimed_at=?, updated_at=? WHERE attempt_id=? AND state='pending'",
                (keeper_pid, keeper_token, keeper_pgid, host, now, now, attempt_id))
            if cur.rowcount != 1:
                return False
            att = c.execute("SELECT task_id, attempt_no FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            c.execute("UPDATE tasks SET state='running', updated_at=? WHERE task_id=? AND current_attempt=?",
                      (now, att["task_id"], att["attempt_no"]))
        return True

    def release_claim(self, attempt_id: str, *, keeper_pid: int, evidence: Dict[str, Any]) -> bool:
        """Undo a claim whose keeper died before it recorded 'spawning' (so no CLI was started)."""
        now = now_iso()
        with self.tx() as c:
            att = c.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if att is None:
                return False
            cur = c.execute(
                "UPDATE attempts SET state='pending', keeper_pid=NULL, keeper_token=NULL, keeper_pgid=NULL, "
                "claimed_at=NULL, evidence_json=?, updated_at=? WHERE attempt_id=? AND state='claimed' "
                "AND keeper_pid=? AND spawn_started_at IS NULL",
                (_merge_json(att["evidence_json"], evidence), now, attempt_id, keeper_pid))
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE tasks SET state='accepted', updated_at=? WHERE task_id=? AND current_attempt=?",
                      (now, att["task_id"], att["attempt_no"]))
        return True

    def mark_spawning(self, attempt_id: str, *, keeper_pid: int) -> bool:
        now = now_iso()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE attempts SET spawn_started_at=?, updated_at=? WHERE attempt_id=? AND state='claimed' "
                "AND keeper_pid=? AND spawn_started_at IS NULL AND cancel_requested_at IS NULL",
                (now, now, attempt_id, keeper_pid))
            return cur.rowcount == 1

    def mark_running(self, attempt_id: str, *, keeper_pid: int, cli_pid: int, cli_token: Optional[str]) -> bool:
        now = now_iso()
        with self.tx() as c:
            cur = c.execute(
                "UPDATE attempts SET state='running', cli_pid=?, cli_token=?, started_at=?, updated_at=? "
                "WHERE attempt_id=? AND state='claimed' AND keeper_pid=?",
                (cli_pid, cli_token, now, now, attempt_id, keeper_pid))
            return cur.rowcount == 1

    def set_native_session(self, attempt_id: str, session_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE attempts SET native_session_id=?, updated_at=? WHERE attempt_id=?",
                      (session_id, now_iso(), attempt_id))

    def add_evidence(self, attempt_id: str, evidence: Dict[str, Any]) -> None:
        with self.tx() as c:
            att = c.execute("SELECT evidence_json FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if att is not None:
                c.execute("UPDATE attempts SET evidence_json=?, updated_at=? WHERE attempt_id=?",
                          (_merge_json(att["evidence_json"], evidence), now_iso(), attempt_id))

    def cancel_requested(self, attempt_id: str) -> bool:
        row = self.conn.execute("SELECT cancel_requested_at FROM attempts WHERE attempt_id=?",
                                (attempt_id,)).fetchone()
        return bool(row and row[0])

    # ------------------------------------------------------------------ terminal / unknown (same transaction as the event)
    def _close_in_tx(self, c: sqlite3.Connection, att: sqlite3.Row, *, new_state: str, kind: str,
                     outcome: Optional[str], exit_code: Optional[int], exit_signal: Optional[int],
                     exit_known: bool, native: Optional[Dict[str, Any]], evidence: Optional[Dict[str, Any]],
                     source: str) -> str:
        now = now_iso()
        if kind == "terminal" and att["cancel_requested_at"] and outcome != "cancelled":
            # Decided inside the closing transaction: a cancel committed after the closer chose its
            # outcome (but before this commit) is still visible here, and one committed after this
            # commit finds the attempt finished and is refused as not cancellable.
            evidence = dict(evidence or {}, cancel_lost_race=True)
        cur = c.execute(
            "UPDATE attempts SET state=?, outcome=?, exit_code=?, exit_signal=?, exit_known=?, "
            "native_result_json=COALESCE(?, native_result_json), evidence_json=?, "
            "ended_at=COALESCE(ended_at, ?), updated_at=? WHERE attempt_id=? AND state=?",
            (new_state, outcome, exit_code, exit_signal, 1 if exit_known else 0,
             canonical_json(native) if native is not None else None,
             _merge_json(att["evidence_json"], evidence), now, now, att["attempt_id"], att["state"]))
        if cur.rowcount != 1:
            raise LedgerWriteFailed("attempt changed concurrently", attempt_id=att["attempt_id"])
        task = c.execute("SELECT current_attempt FROM tasks WHERE task_id=?", (att["task_id"],)).fetchone()
        if task["current_attempt"] == att["attempt_no"]:
            c.execute("UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
                      (task_state_for(new_state, outcome), now, att["task_id"]))
        excerpt = None
        if native and isinstance(native.get("result"), str):
            excerpt = native["result"][:RESULT_EXCERPT_CHARS]
        body = {
            "task_id": att["task_id"],
            "attempt_no": att["attempt_no"],
            "attempt_id": att["attempt_id"],
            "kind": kind,
            "state": task_state_for(new_state, outcome),
            "outcome": outcome,
            "exit": {"code": exit_code, "signal": exit_signal, "known": bool(exit_known)},
            "native_session_id": att["native_session_id"],
            "native_ok": None if native is None else native.get("ok"),
            "result_excerpt": excerpt,
            "source": source,
        }
        event_id = new_id("e")
        c.execute("INSERT INTO events (event_id, task_id, attempt_no, kind, body_json, created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (event_id, att["task_id"], att["attempt_no"], kind, canonical_json(body), now))
        return event_id

    def finish(self, attempt_id: str, *, from_states: Sequence[str], outcome: str,
               exit_code: Optional[int] = None, exit_signal: Optional[int] = None, exit_known: bool = False,
               native: Optional[Dict[str, Any]] = None, evidence: Optional[Dict[str, Any]] = None,
               source: str = "keeper") -> Optional[str]:
        with self.tx() as c:
            att = c.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if att is None or att["state"] not in from_states:
                return None
            return self._close_in_tx(c, att, new_state="finished", kind="terminal", outcome=outcome,
                                     exit_code=exit_code, exit_signal=exit_signal, exit_known=exit_known,
                                     native=native, evidence=evidence, source=source)

    def mark_unknown(self, attempt_id: str, *, from_states: Sequence[str], evidence: Dict[str, Any]) -> Optional[str]:
        with self.tx() as c:
            att = c.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if att is None or att["state"] not in from_states:
                return None
            return self._close_in_tx(c, att, new_state="unknown", kind="unknown", outcome=None,
                                     exit_code=None, exit_signal=None, exit_known=False, native=None,
                                     evidence=evidence, source="reconcile")

    # ------------------------------------------------------------------ cancel / resume
    def request_cancel(self, task_id: str) -> Tuple[str, Dict[str, Any], Optional[str]]:
        """Returns (result, attempt, event_id). A pending attempt is closed directly (nothing ran)."""
        now = now_iso()
        with self.tx() as c:
            task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise NotFound("no such task", task_id=task_id)
            att = c.execute("SELECT * FROM attempts WHERE task_id=? AND attempt_no=?",
                            (task_id, task["current_attempt"])).fetchone()
            if att["state"] == "pending":
                event_id = self._close_in_tx(c, att, new_state="finished", kind="terminal", outcome="cancelled",
                                             exit_code=None, exit_signal=None, exit_known=False, native=None,
                                             evidence={"cancelled_before_start": True}, source="cancel")
                return "cancelled_before_start", dict(att), event_id
            if att["state"] in ("claimed", "running"):
                c.execute("UPDATE attempts SET cancel_requested_at=COALESCE(cancel_requested_at, ?), updated_at=? "
                          "WHERE attempt_id=?", (now, now, att["attempt_id"]))
                return "cancel_requested", dict(att), None
            return "not_cancellable", dict(att), None

    def new_attempt(self, task_id: str, *, expect_attempt_no: int, reason: str, prompt: str,
                    resume_session_id: Optional[str], native_session_id: Optional[str]) -> Dict[str, Any]:
        now = now_iso()
        with self.tx() as c:
            task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise NotFound("no such task", task_id=task_id)
            if task["current_attempt"] != expect_attempt_no:
                raise Refused("concurrent_resume", "another resume already created a newer attempt",
                              current_attempt=task["current_attempt"])
            att = c.execute("SELECT * FROM attempts WHERE task_id=? AND attempt_no=?",
                            (task_id, expect_attempt_no)).fetchone()
            if att["state"] in ACTIVE_STATES:
                raise Refused("attempt_active", "the current attempt has not finished", state=att["state"])
            next_no = expect_attempt_no + 1
            attempt_id = new_id("a")
            c.execute(
                "INSERT INTO attempts (attempt_id, task_id, attempt_no, reason, state, prompt, resume_session_id, "
                "native_session_id, cwd, created_at, updated_at) VALUES (?,?,?,?,'pending',?,?,?,?,?,?)",
                (attempt_id, task_id, next_no, reason, prompt, resume_session_id, native_session_id, task["cwd"],
                 now, now))
            cur = c.execute("UPDATE tasks SET current_attempt=?, state='accepted', updated_at=? "
                            "WHERE task_id=? AND current_attempt=?", (next_no, now, task_id, expect_attempt_no))
            if cur.rowcount != 1:
                raise Refused("concurrent_resume", "another resume already created a newer attempt")
        return self.get_attempt(attempt_id)

    # ------------------------------------------------------------------ delivery
    def events_for(self, consumer: str, *, task_id: Optional[str] = None,
                   include_acked: bool = False) -> List[Dict[str, Any]]:
        sql = ("SELECT e.*, t.current_attempt AS current_attempt, "
               "(SELECT acked_at FROM acks a WHERE a.event_id=e.event_id AND a.consumer=?) AS acked_at "
               "FROM events e JOIN tasks t ON t.task_id=e.task_id WHERE (? IS NULL OR e.task_id=?) ")
        params: List[Any] = [consumer, task_id, task_id]
        if not include_acked:
            # Filtered in SQL (primary-key lookup in acks): an empty poll reads no acknowledged history.
            sql += "AND NOT EXISTS (SELECT 1 FROM acks a WHERE a.event_id=e.event_id AND a.consumer=?) "
            params.append(consumer)
        sql += "ORDER BY e.created_at, e.event_id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def record_deliveries(self, consumer: str, event_ids: Sequence[str]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        if not event_ids:
            return counts
        now = now_iso()
        with self.tx() as c:
            for eid in event_ids:
                c.execute("INSERT INTO deliveries (event_id, consumer, count, first_at, last_at) VALUES (?,?,1,?,?) "
                          "ON CONFLICT(event_id, consumer) DO UPDATE SET count=count+1, last_at=excluded.last_at",
                          (eid, consumer, now, now))
                counts[eid] = c.execute("SELECT count FROM deliveries WHERE event_id=? AND consumer=?",
                                        (eid, consumer)).fetchone()[0]
        return counts

    def ack(self, *, task_id: str, attempt_no: int, event_id: str, consumer: str) -> Dict[str, Any]:
        with self.tx() as c:
            ev = c.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if ev is None:
                raise AckMismatch("no such event", event_id=event_id)
            if ev["task_id"] != task_id or ev["attempt_no"] != attempt_no:
                raise AckMismatch("event does not belong to this task/attempt", event_id=event_id,
                                  event_task_id=ev["task_id"], event_attempt_no=ev["attempt_no"],
                                  given_task_id=task_id, given_attempt_no=attempt_no)
            cur = c.execute("INSERT OR IGNORE INTO acks (event_id, consumer, task_id, attempt_no, acked_at) "
                            "VALUES (?,?,?,?,?)", (event_id, consumer, task_id, attempt_no, now_iso()))
            duplicate = cur.rowcount == 0
            acked_at = c.execute("SELECT acked_at FROM acks WHERE event_id=? AND consumer=?",
                                 (event_id, consumer)).fetchone()[0]
        return {"event_id": event_id, "consumer": consumer, "task_id": task_id, "attempt_no": attempt_no,
                "duplicate": duplicate, "acked_at": acked_at}

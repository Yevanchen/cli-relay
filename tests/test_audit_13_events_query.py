"""Audit #13: polling for unacknowledged events must not read the whole acknowledged history."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import run_cli

from cli_relay.ledger import Ledger


class _CountingCursor:
    def __init__(self, cur, counter):
        self._cur, self._counter = cur, counter

    def fetchall(self):
        rows = self._cur.fetchall()
        self._counter[0] += len(rows)
        return rows

    def fetchone(self):
        row = self._cur.fetchone()
        self._counter[0] += 1 if row is not None else 0
        return row

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _CountingConnection:
    def __init__(self, conn, counter):
        self._conn, self._counter = conn, counter

    def execute(self, *args):
        return _CountingCursor(self._conn.execute(*args), self._counter)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class EventsQueryCost(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-a13-"))
        self.home = self.tmp / "home"
        self.assertEqual(run_cli(self.home, "init")[0], 0)
        conn = sqlite3.connect(str(self.home / "ledger.db"))
        now = "2026-01-01T00:00:00.000Z"
        conn.execute("INSERT INTO tasks VALUES ('t-1','default','k','h','{}','fixture','/','succeeded',1,1,?,?)",
                     (now, now))
        rows = [(f"e-{i:05d}", "t-1", i + 1, "terminal", "{}", now) for i in range(3000)]
        conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO acks VALUES (?,?,?,?,?)", [(r[0], "grok", "t-1", r[2], now) for r in rows])
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_poll_reads_no_acknowledged_rows(self) -> None:
        ledger = Ledger(self.home)
        counter = [0]
        ledger.conn = _CountingConnection(ledger.conn, counter)
        self.assertEqual(ledger.events_for("grok"), [])
        self.assertLess(counter[0], 10, f"an empty poll fetched {counter[0]} rows")
        self.assertEqual(len(ledger.events_for("phone")), 3000, "another consumer still sees everything")


if __name__ == "__main__":
    unittest.main()

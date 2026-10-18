"""Shared helpers. Every test runs the real CLI as separate OS processes against its own
temporary ledger, and only ever signals processes recorded in that ledger (pid + start time)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cli_relay import procs  # noqa: E402


def base_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLI_RELAY_")}
    env["PYTHONPATH"] = str(REPO)
    env.update(extra or {})
    return env


def run_cli(home: Path, *args: str, env: Optional[Dict[str, str]] = None,
            timeout: float = 60.0) -> Tuple[int, Dict[str, Any]]:
    proc = subprocess.run([sys.executable, "-m", "cli_relay", "--home", str(home), *args],
                          capture_output=True, text=True, env=env or base_env(), timeout=timeout,
                          stdin=subprocess.DEVNULL)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise AssertionError(f"expected one JSON line, got {proc.stdout!r} / stderr {proc.stderr!r}")
    return proc.returncode, json.loads(lines[0])


def wait_until(pred: Callable[[], Any], timeout: float = 30.0, interval: float = 0.1) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(interval)


def side_effects(path: Path) -> List[Dict[str, Any]]:
    try:
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def db_rows(home: Path, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    conn = sqlite3.connect(str(home / "ledger.db"), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class RelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-test-"))
        self.home = self.tmp / "home"
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.effects = self.tmp / "effects.jsonl"
        code, out = run_cli(self.home, "init")
        self.assertEqual(code, 0, out)

    def tearDown(self) -> None:
        self.kill_recorded_processes()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def kill_recorded_processes(self) -> None:
        if not (self.home / "ledger.db").exists():
            return
        try:
            rows = db_rows(self.home, "SELECT keeper_pid, keeper_token, cli_pid, cli_token FROM attempts")
        except sqlite3.Error:
            return
        for r in rows:
            for pid, tok in ((r["cli_pid"], r["cli_token"]), (r["keeper_pid"], r["keeper_token"])):
                if pid and tok and procs.same_process_alive(pid, tok):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass

    def cli(self, *args: str, env: Optional[Dict[str, str]] = None, expect: Optional[int] = 0) -> Dict[str, Any]:
        code, out = run_cli(self.home, *args, env=env)
        if expect is not None:
            self.assertEqual(code, expect, out)
        return out

    def submit(self, key: str, *, prompt: str = "do the thing", duration: float = 0.2, scope: str = "default",
               extra_options: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
               expect: Optional[int] = 0) -> Dict[str, Any]:
        args = ["submit", "--key", key, "--scope", scope, "--engine", "fixture", "--cwd", str(self.work),
                "--prompt", prompt, "--option", f"duration_s={duration}",
                "--option", f"side_effect_file={json.dumps(str(self.effects))}"]
        for opt in extra_options or []:
            args += ["--option", opt]
        return self.cli(*args, env=env, expect=expect)

    def status(self, task_id: str, *, reconcile: bool = True) -> Dict[str, Any]:
        args = ["status", task_id] + ([] if reconcile else ["--no-reconcile"])
        return self.cli(*args)

    def attempt_row(self, task_id: str, attempt_no: int = 1) -> sqlite3.Row:
        return db_rows(self.home, "SELECT * FROM attempts WHERE task_id=? AND attempt_no=?", (task_id, attempt_no))[0]

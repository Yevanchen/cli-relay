#!/usr/bin/env python3
"""Long-run soak driver for cli-relay (cheap deterministic fixture tasks only).

  soak.py start   --dir DIR [--hours 25]  detach and run in the background; prints the pid
  soak.py run     --dir DIR [--hours 25]  run in the foreground; exit status 0 only on PASS
  soak.py status  --dir DIR               print the latest checkpoint, and the summary once finished
  soak.py verdict --dir DIR               re-evaluate a finished soak with this version's verdict rules
                                          (reads only; exit status 0 only on PASS)

Scenario (all times scale with --hours; numbers below are for 25 h):
  * L: a long task whose fixture sleeps for ~98% of the run - no implicit deadline may cut it;
  * K: a task whose keeper is SIGKILLed early while its CLI keeps running (reconciled later);
  * U: a task whose keeper crashes before spawn (unknown, never auto re-run), explicitly re-run once;
  * every ~30 min a short task, plus a duplicate submit of the previous key;
  * the consumer is offline for the first half, then reads and ACKs every ~hour, repeating one ACK;
  * at ~25% the driver restarts itself (exec) from its state file (control-process restart);
  * a checkpoint line every ~10 min with invariant checks; wall-clock gaps (sleep) are recorded.
The driver never changes system settings and only signals processes recorded in its own ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cli_relay import procs  # noqa: E402


def _seconds_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds()


def relay(home: Path, *args: str, env_extra: Dict[str, str] = None) -> Tuple[int, Dict[str, Any]]:
    env = dict(os.environ, PYTHONPATH=str(REPO))
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-m", "cli_relay", "--home", str(home), *args], capture_output=True,
                       text=True, env=env, timeout=300, stdin=subprocess.DEVNULL)
    try:
        return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return p.returncode, {"ok": False, "error": "unparsable", "stderr": p.stderr[-500:]}


class Soak:
    def __init__(self, root: Path, hours: float) -> None:
        self.root = root
        self.home = root / "ledger"
        self.work = root / "work"
        self.effects = root / "effects"
        self.state_path = root / "state.json"
        self.ckpt_path = root / "checkpoints.jsonl"
        if self.state_path.exists():
            self.s = json.loads(self.state_path.read_text())
        else:
            total = hours * 3600.0
            self.s = {"started_wall": time.time(), "total_s": total, "done": {}, "keys": {}, "events": {},
                      "short_index": 0, "next_short_at": 0.0, "next_ack_at": total * 0.5, "next_ckpt_at": 0.0,
                      "restarts": 0, "gaps": [], "notes": [], "ack_duplicates": 0}
        self.total = self.s["total_s"]

    # -- plumbing
    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1, sort_keys=True))
        os.replace(tmp, self.state_path)

    def rel(self) -> float:
        return time.time() - self.s["started_wall"]

    def note(self, msg: str) -> None:
        self.s["notes"].append({"at_s": round(self.rel(), 1), "msg": msg})

    def submit(self, key: str, duration: float, expected_effects: int, env_extra: Dict[str, str] = None) -> str:
        eff = self.effects / f"{key}.jsonl"
        code, out = relay(self.home, "submit", "--key", key, "--engine", "fixture", "--cwd", str(self.work),
                          "--prompt", f"soak {key}", "--option", f"duration_s={duration}",
                          "--option", f"side_effect_file={json.dumps(str(eff))}", env_extra=env_extra)
        if code != 0:
            raise RuntimeError(f"submit {key} failed: {out}")
        self.s["keys"].setdefault(key, {"task_id": out["task_id"], "expected_effects": expected_effects,
                                        "duration_s": duration})
        return out["task_id"]

    def effects_of(self, key: str) -> int:
        try:
            return sum(1 for ln in (self.effects / f"{key}.jsonl").read_text().splitlines() if ln.strip())
        except FileNotFoundError:
            return 0

    def rows(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        conn = sqlite3.connect(str(self.home / "ledger.db"), timeout=60)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # -- scenario steps
    def setup(self) -> None:
        for d in (self.work, self.effects):
            d.mkdir(parents=True, exist_ok=True)
        relay(self.home, "init")
        self.submit("soak-long", self.total * 0.98, 1)
        self.submit("soak-keeper-kill", max(5.0, self.total * 0.08), 1)
        self.submit("soak-unknown", 1.0, 1, env_extra={"CLI_RELAY_TEST_CRASH": "before_spawn"})
        self.note("setup: long, keeper-kill and crash-before-spawn tasks submitted")

    def kill_keeper(self) -> bool:
        task_id = self.s["keys"]["soak-keeper-kill"]["task_id"]
        r = self.rows("SELECT * FROM attempts WHERE task_id=? AND attempt_no=1", (task_id,))[0]
        if not r["cli_pid"]:
            return False
        if procs.same_process_alive(r["keeper_pid"], r["keeper_token"]) is True:
            os.kill(r["keeper_pid"], signal.SIGKILL)
            self.note(f"SIGKILL keeper of soak-keeper-kill (CLI alive: "
                      f"{procs.same_process_alive(r['cli_pid'], r['cli_token'])})")
        return True

    def rerun_unknown(self) -> bool:
        task_id = self.s["keys"]["soak-unknown"]["task_id"]
        _, st = relay(self.home, "status", task_id)
        if st.get("task", {}).get("state") != "unknown":
            return False
        if self.effects_of("soak-unknown") != 0:
            self.note("VIOLATION: unknown task produced a side effect before the explicit re-run")
        code, out = relay(self.home, "resume", task_id, "--fresh", "--confirm-rerun")
        self.note(f"explicit re-run of the unknown task: ok={out.get('ok')} attempt={out.get('attempt_no')}")
        return code == 0

    def short_tasks(self) -> None:
        i = self.s["short_index"]
        key = f"soak-short-{i:04d}"
        self.submit(key, min(60.0, max(0.05, self.total * 0.0007)), 1)
        if i > 0:
            prev = f"soak-short-{i - 1:04d}"
            code, out = relay(self.home, "submit", "--key", prev, "--engine", "fixture", "--cwd", str(self.work),
                              "--prompt", f"soak {prev}", "--option",
                              f"duration_s={self.s['keys'][prev]['duration_s']}", "--option",
                              f"side_effect_file={json.dumps(str(self.effects / (prev + '.jsonl')))}")
            if out.get("status") != "duplicate":
                self.note(f"VIOLATION: duplicate submit of {prev} returned {out.get('status')}")
        self.s["short_index"] = i + 1

    def consume(self) -> None:
        code, out = relay(self.home, "events", "--consumer", "soak-consumer")
        events = out.get("events", [])
        for ev in events:
            c, res = relay(self.home, "ack", "--consumer", "soak-consumer", "--task", ev["task_id"], "--attempt",
                           str(ev["attempt_no"]), "--event", ev["event_id"])
            if c != 0:
                self.note(f"VIOLATION: ack failed for {ev['event_id']}: {res}")
        if events:
            ev = events[0]
            c, res = relay(self.home, "ack", "--consumer", "soak-consumer", "--task", ev["task_id"], "--attempt",
                           str(ev["attempt_no"]), "--event", ev["event_id"])
            if res.get("duplicate") is True:
                self.s["ack_duplicates"] += 1
            else:
                self.note("VIOLATION: repeated ack was not reported as duplicate")
        self.note(f"consumer read {len(events)} unacked events and acked them")

    def check(self) -> Dict[str, Any]:
        relay(self.home, "reconcile")
        _, out = relay(self.home, "events", "--consumer", "soak-auditor", "--include-acked", "--no-reconcile")
        current = {e["event_id"]: json.dumps(e["body"], sort_keys=True) for e in out.get("events", [])}
        lost = [eid for eid in self.s["events"] if eid not in current]
        changed = [eid for eid, body in self.s["events"].items() if eid in current and current[eid] != body]
        for eid, body in current.items():
            self.s["events"].setdefault(eid, body)
        over = {k: self.effects_of(k) for k, v in self.s["keys"].items() if self.effects_of(k) > v["expected_effects"]}
        states = {k: (self.rows("SELECT state FROM tasks WHERE task_id=?", (v["task_id"],)) or [[None]])[0][0]
                  for k, v in self.s["keys"].items() if not k.startswith("soak-short-")}
        unknown_attempts = len(self.rows("SELECT * FROM attempts WHERE task_id=?",
                                         (self.s["keys"]["soak-unknown"]["task_id"],)))
        unacked = len(self.rows("SELECT e.event_id FROM events e LEFT JOIN acks a ON a.event_id=e.event_id "
                                "AND a.consumer='soak-consumer' WHERE a.event_id IS NULL"))
        return {"at_s": round(self.rel(), 1), "events_total": len(current), "events_lost": lost,
                "event_bodies_changed": changed, "keys_over_executed": over, "states": states,
                "unknown_task_attempts": unknown_attempts, "unacked_by_consumer": unacked,
                "short_tasks": self.s["short_index"], "restarts": self.s["restarts"], "gaps": len(self.s["gaps"]),
                "ack_duplicates_confirmed": self.s["ack_duplicates"]}

    def checkpoint(self, final: bool = False) -> Dict[str, Any]:
        c = self.check()
        c["final"] = final
        with open(self.ckpt_path, "a") as f:
            f.write(json.dumps(c, sort_keys=True) + "\n")
        return c

    def finish(self) -> None:
        deadline = time.time() + max(120.0, self.total * 0.05)
        while time.time() < deadline:
            states = [r[0] for r in self.rows("SELECT state FROM tasks")]
            if all(s not in ("accepted", "running") for s in states):
                break
            relay(self.home, "reconcile")
            time.sleep(5)
        self.consume()
        c = self.checkpoint(final=True)
        (self.root / "summary.json").write_text(json.dumps(self.evaluate(c), indent=2, sort_keys=True))

    def evaluate(self, c: Dict[str, Any], elapsed_s: Optional[float] = None) -> Dict[str, Any]:
        """The verdict, from facts only (reads, no writes). PASS needs every submitted key to reach
        `succeeded` with exactly its expected number of executions, the run to have lasted its
        planned time and the long task to have run for its full configured duration."""
        keys = self.s["keys"]
        state = {k: (self.rows("SELECT state FROM tasks WHERE task_id=?", (v["task_id"],)) or [[None]])[0][0]
                 for k, v in keys.items()}
        effects = {k: self.effects_of(k) for k in keys}

        def attempt(key: str, no: int = 1) -> Dict[str, Any]:
            rows = self.rows("SELECT * FROM attempts WHERE task_id=? AND attempt_no=?", (keys[key]["task_id"], no))
            return dict(rows[0]) if rows else {}

        long_row, kill_row, unknown_first = attempt("soak-long"), attempt("soak-keeper-kill"), attempt("soak-unknown")
        long_ran_s = _seconds_between(long_row.get("started_at"), long_row.get("ended_at"))
        elapsed_s = self.rel() if elapsed_s is None else elapsed_s
        shorts = [k for k in keys if k.startswith("soak-short-")]
        violations = [n for n in self.s["notes"] if n["msg"].startswith("VIOLATION")]
        verdicts = {
            "no_lost_or_changed_events": not c["events_lost"] and not c["event_bodies_changed"],
            "every_key_executed_exactly_as_expected": all(effects[k] == v["expected_effects"] for k, v in keys.items()),
            "every_task_succeeded": all(s == "succeeded" for s in state.values()),
            "ran_for_the_planned_time": elapsed_s >= 0.99 * self.total,
            "long_task_ran_its_full_duration": state.get("soak-long") == "succeeded" and long_ran_s is not None
            and long_ran_s >= 0.95 * keys["soak-long"]["duration_s"],
            "short_tasks_submitted_and_tracked": len(shorts) >= 1 and len(shorts) == self.s["short_index"],
            "keeper_killed_task_reconciled_once": state.get("soak-keeper-kill") == "succeeded"
            and kill_row.get("exit_known") == 0 and effects.get("soak-keeper-kill") == 1,
            "unknown_task_rerun_only_explicitly": unknown_first.get("state") == "unknown"
            and c["unknown_task_attempts"] == 2 and state.get("soak-unknown") == "succeeded"
            and effects.get("soak-unknown") == 1,
            "all_events_acked_at_end": c["unacked_by_consumer"] == 0,
            "control_restart_exercised": self.s["restarts"] >= 1,
            "repeated_ack_reported_duplicate": self.s["ack_duplicates"] >= 1,
            "no_violation_notes": not violations,
        }
        return {"verdict": "PASS" if all(verdicts.values()) else "FAIL", "verdicts": verdicts,
                "planned_hours": self.total / 3600.0, "elapsed_hours": round(elapsed_s / 3600.0, 3),
                "long_task_planned_hours": round(keys["soak-long"]["duration_s"] / 3600.0, 3),
                "long_task_ran_hours": None if long_ran_s is None else round(long_ran_s / 3600.0, 3),
                "short_tasks": len(shorts),
                "keys_not_as_expected": {k: {"state": state[k], "executions": effects[k],
                                             "expected_executions": v["expected_effects"]}
                                         for k, v in keys.items()
                                         if state[k] != "succeeded" or effects[k] != v["expected_effects"]},
                "gaps": self.s["gaps"], "final_checkpoint": c, "notes": self.s["notes"][-50:]}

    def run(self) -> None:
        if not self.s["done"].get("setup"):
            self.setup()
            self.s["done"]["setup"] = True
            self.save()
        tick = min(5.0, max(0.5, self.total / 2000.0))
        last_wall, last_mono = time.time(), time.monotonic()
        while True:
            now_wall, now_mono = time.time(), time.monotonic()
            gap = (now_wall - last_wall) - (now_mono - last_mono)
            if gap > 30 or (now_mono - last_mono) > max(60.0, tick * 20):
                self.s["gaps"].append({"at_s": round(self.rel(), 1), "wall_s": round(now_wall - last_wall, 1),
                                       "monotonic_s": round(now_mono - last_mono, 1)})
            last_wall, last_mono = now_wall, now_mono
            r = self.rel()
            if r >= self.total:
                break
            if not self.s["done"].get("kill_keeper") and r >= self.total * 0.004:
                self.s["done"]["kill_keeper"] = self.kill_keeper()
            if not self.s["done"].get("rerun_unknown") and r >= self.total * 0.01:
                self.s["done"]["rerun_unknown"] = self.rerun_unknown()
            if r >= self.s["next_short_at"]:
                self.short_tasks()
                self.s["next_short_at"] = r + max(10.0, self.total * 0.02)
            if r >= self.s["next_ack_at"]:
                self.consume()
                self.s["next_ack_at"] = r + max(5.0, self.total * 0.04)
            if r >= self.s["next_ckpt_at"]:
                self.checkpoint()
                self.s["next_ckpt_at"] = r + max(5.0, self.total * 0.007)
            if self.s["restarts"] == 0 and r >= self.total * 0.25:
                self.s["restarts"] = 1
                self.note("control restart: driver re-execs itself from its state file")
                self.save()
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "run",
                                          "--dir", str(self.root)])
            self.save()
            time.sleep(tick)
        self.save()
        self.finish()
        self.save()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("start", "run", "status", "verdict"))
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--hours", type=float, default=25.0)
    a = ap.parse_args()
    root = a.dir.resolve()
    if a.action == "verdict":
        try:
            lines = root.joinpath("checkpoints.jsonl").read_text().splitlines()
            final = [c for c in (json.loads(ln) for ln in lines if ln.strip()) if c.get("final")]
        except (OSError, ValueError):
            final = []
        if not final or not (root / "state.json").exists():
            print(json.dumps({"ok": False, "error": "no finished soak in this directory"}))
            return 1
        result = Soak(root, a.hours).evaluate(final[-1], elapsed_s=final[-1]["at_s"])
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["verdict"] == "PASS" else 1
    if a.action == "status":
        out: Dict[str, Any] = {"dir": str(root)}
        pid_file = root / "soak.pid"
        if pid_file.exists():
            info = json.loads(pid_file.read_text())
            out["pid"] = info["pid"]
            out["running"] = procs.same_process_alive(info["pid"], info["start_token"]) is True
        try:
            out["last_checkpoint"] = json.loads(root.joinpath("checkpoints.jsonl").read_text().splitlines()[-1])
        except (OSError, IndexError, ValueError):
            out["last_checkpoint"] = None
        summary = root / "summary.json"
        out["summary"] = json.loads(summary.read_text()) if summary.exists() else None
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    root.mkdir(parents=True, exist_ok=True)
    if a.action == "start":
        if (root / "state.json").exists():
            print(json.dumps({"ok": False, "error": "a soak already exists in this directory"}))
            return 1
        pid = os.fork()
        if pid == 0:
            os.setsid()
            if os.fork() == 0:
                log = os.open(str(root / "soak.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                nul = os.open(os.devnull, os.O_RDONLY)
                os.dup2(nul, 0)
                os.dup2(log, 1)
                os.dup2(log, 2)
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "run", "--dir",
                                          str(root), "--hours", str(a.hours)])
            os._exit(0)
        os.waitpid(pid, 0)
        for _ in range(100):
            if (root / "soak.pid").exists():
                break
            time.sleep(0.1)
        print((root / "soak.pid").read_text() if (root / "soak.pid").exists() else json.dumps({"ok": False}))
        return 0
    me = os.getpid()  # unchanged across the driver's own exec-restart
    (root / "soak.pid").write_text(json.dumps({"pid": me, "start_token": procs.start_token(me)}))
    Soak(root, a.hours).run()
    summary = json.loads((root / "summary.json").read_text())
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

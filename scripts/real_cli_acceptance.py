#!/usr/bin/env python3
"""Minimal real closed loops through the relay with an official CLI (manual, never in CI).

For one engine (claude or codex) this runs, via the relay's own JSON CLI:
  0. doctor --engine: the subscription auth path must be confirmed, otherwise nothing runs;
  1. a tiny successful task;
  2. a cheap failing task (unknown model name) to check failure parsing;
  3. an explicit resume of the exact native session from step 1;
  4. events -> ack for every terminal event.
It records commands, ids, timings and verdicts as JSON. Model usage: two tiny prompts plus
one request that fails on an unknown model, all on the existing subscription.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parent.parent
PHRASE = "relay-ok"


def relay(home: Path, *args: str, timeout: float = 900) -> Tuple[int, Dict[str, Any], List[str]]:
    cmd = [sys.executable, "-m", "cli_relay", "--home", str(home), *args]
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1]), ["cli-relay", *args]


def excerpt(att: Dict[str, Any]) -> Any:
    native = att.get("native") or {}
    text = native.get("result")
    return text[:200] if isinstance(text, str) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=("claude", "codex"))
    ap.add_argument("--home", required=True, type=Path)
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    a.workdir.mkdir(parents=True, exist_ok=True)
    if a.engine == "codex" and not (a.workdir / ".git").exists():
        subprocess.run(["git", "init", "-q", str(a.workdir)], check=True)
    report: Dict[str, Any] = {"engine": a.engine, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": []}
    stamp = time.strftime("%Y%m%d%H%M%S")

    def step(name: str, verdict: str, **data: Any) -> None:
        report["steps"].append({"name": name, "verdict": verdict, **data})

    relay(a.home, "init")
    code, doc, cmd = relay(a.home, "doctor", "--engine", a.engine)
    step("preflight", "PASS" if code == 0 else "BLOCKED", command=cmd, engine_check=doc.get("engine"))
    if code != 0:
        report["result"] = "NOT RUN (auth path not confirmed as subscription)"
        a.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    opts = ["--option", 'sandbox="read-only"'] if a.engine == "codex" else []
    t0 = time.monotonic()
    code, sub, cmd = relay(a.home, "submit", "--key", f"real-{a.engine}-ok-{stamp}", "--engine", a.engine,
                           "--cwd", str(a.workdir), "--prompt", f"Reply with exactly: {PHRASE}", *opts)
    _, done, wcmd = relay(a.home, "wait", sub["task_id"], "--timeout", "600")
    att = done["attempts"][0]
    ok = (done["task"]["state"] == "succeeded" and PHRASE in (excerpt(att) or "")
          and att["exit"] == {"code": 0, "signal": None, "known": True} and bool(att["native_session_id"]))
    if a.engine == "claude":
        ok = ok and att["native_session_id"] == sub["native_session_id"]
    step("success", "PASS" if ok else "FAIL", command=cmd, task_id=sub["task_id"], state=done["task"]["state"],
         native_session_id=att["native_session_id"], preassigned_session_id=sub.get("native_session_id"),
         exit=att["exit"], native_ok=(att.get("native") or {}).get("ok"), result_excerpt=excerpt(att),
         seconds=round(time.monotonic() - t0, 1))
    ok_task, ok_sid = sub["task_id"], att["native_session_id"]

    t0 = time.monotonic()
    model = ["--option", 'model="cli-relay-no-such-model"']
    code, sub, cmd = relay(a.home, "submit", "--key", f"real-{a.engine}-fail-{stamp}", "--engine", a.engine,
                           "--cwd", str(a.workdir), "--prompt", "Reply with exactly: unused", *opts, *model)
    _, done, _ = relay(a.home, "wait", sub["task_id"], "--timeout", "600")
    att = done["attempts"][0]
    failed_ok = done["task"]["state"] == "failed" and att["exit"]["known"] and att["exit"]["code"] not in (0, None)
    step("failure_parsing", "PASS" if failed_ok else "FAIL", command=cmd, task_id=sub["task_id"],
         state=done["task"]["state"], exit=att["exit"], native=att.get("native"),
         seconds=round(time.monotonic() - t0, 1))

    t0 = time.monotonic()
    code, res, cmd = relay(a.home, "resume", ok_task, "--prompt",
                           "What exact phrase did you reply with in your previous message? "
                           "Reply with only that phrase.")
    _, done, _ = relay(a.home, "wait", ok_task, "--timeout", "600")
    att2 = done["attempts"][-1]
    resumed_ok = (code == 0 and res.get("resume_session_id") == ok_sid and done["task"]["state"] == "succeeded"
                  and PHRASE in (excerpt(att2) or "") and att2["native_session_id"] == ok_sid)
    step("exact_resume", "PASS" if resumed_ok else "FAIL", command=cmd, task_id=ok_task,
         attempt_no=att2["attempt_no"], resume_session_id=res.get("resume_session_id"),
         native_session_id=att2["native_session_id"], exit=att2["exit"], result_excerpt=excerpt(att2),
         seconds=round(time.monotonic() - t0, 1))

    _, evs, cmd = relay(a.home, "events", "--consumer", "acceptance")
    acked = []
    for ev in evs["events"]:
        c, out, _ = relay(a.home, "ack", "--consumer", "acceptance", "--task", ev["task_id"], "--attempt",
                          str(ev["attempt_no"]), "--event", ev["event_id"])
        acked.append({"event_id": ev["event_id"], "task_id": ev["task_id"], "attempt_no": ev["attempt_no"],
                      "outcome": ev["body"]["outcome"], "acked": c == 0})
    _, after, _ = relay(a.home, "events", "--consumer", "acceptance")
    step("events_ack", "PASS" if acked and all(x["acked"] for x in acked) and not after["events"] else "FAIL",
         command=cmd, events=acked)
    verdicts = [s["verdict"] for s in report["steps"]]
    report["result"] = "PASS" if all(v == "PASS" for v in verdicts) else "FAIL"
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    a.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({"engine": a.engine, "result": report["result"], "verdicts": verdicts}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

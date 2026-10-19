#!/usr/bin/env python3
"""Minimal real closed loops through the relay with an official CLI (manual, never in CI).

For one engine (claude or codex) this runs, via the relay's own JSON CLI:
  0. doctor --engine --cwd: the subscription auth path must be confirmed, otherwise nothing runs;
  1. a tiny successful task;
  2. a cheap failing task (unknown model name) to check failure parsing;
  3. an explicit resume of the exact native session from step 1;
  4. events -> ack: exactly this run's terminal events (one per attempt) are present, and only
     those are acknowledged; events of other tasks in the same ledger are left alone.
For Codex it also reads two fields of the thread's own record (provider and per-turn sandbox),
nothing else. It records commands, ids, timings and verdicts as JSON. Model usage: two tiny
prompts plus one request that fails on an unknown model, all on the existing subscription.

`--engine fixture` runs the same steps against the deterministic fixture (no model), which is
how CI tests this script; `--sabotage-resume` (fixture only) replaces the exact resume with a
fresh re-run so that the FAIL path can be tested.

`--notify` also configures the notifier of the run's ledger against a local fake webhook (an
HTTP server on 127.0.0.1 started by this script; no real Bot is contacted) and adds a step:
  5. each of the three terminal events was sent exactly once, naming this ledger and route,
     with the transport accepted; after the consumer ACKs of step 4 every notification is acked.

Exit status: 0 PASS, 1 FAIL, 3 NOT RUN (subscription auth path not confirmed).
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cli_relay import engines  # noqa: E402

PHRASE = "relay-ok"
CONSUMER = "acceptance"


class LocalWebhook:
    """Records POSTs and answers 200; stands in for the Bot's webhook."""

    def __init__(self) -> None:
        self.bodies: List[Dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def configure(self, home: Path) -> None:
        (home / "acceptance-webhook-url").write_text(f"http://127.0.0.1:{self.server.server_address[1]}/hook\n")
        (home / "acceptance-webhook-token").write_text("local-acceptance-token\n")
        (home / "notifier.json").write_text(json.dumps({
            "source_id": "acceptance", "default_route": "local",
            "routes": {"local": {"consumer": CONSUMER, "chat": "local fake webhook", "adapter": {
                "type": "http", "url_file": str(home / "acceptance-webhook-url"),
                "token_file": str(home / "acceptance-webhook-token"), "timeout_s": 5}}},
            "reminder": {"ack_timeout_s": 3600, "max_wakes": 2}}))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def relay(home: Path, *args: str, timeout: float = 900) -> Tuple[int, Dict[str, Any], List[str]]:
    cmd = [sys.executable, "-m", "cli_relay", "--home", str(home), *args]
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1]), ["cli-relay", *args]


def excerpt(att: Dict[str, Any]) -> Any:
    native = att.get("native") or {}
    text = native.get("result")
    return text[:200] if isinstance(text, str) else None


def codex_thread_record(thread_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Provider and per-turn sandbox from the thread's own rollout; no other field is read."""
    path = engines.codex_rollout(dict(os.environ), thread_id) if thread_id else None
    if path is None:
        return None
    provider, sandboxes = None, []
    for ev in engines.iter_json_lines(path):
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if ev.get("type") == "session_meta":
            provider = payload.get("model_provider")
        elif ev.get("type") == "turn_context":
            policy = payload.get("sandbox_policy")
            sandboxes.append(policy.get("type") if isinstance(policy, dict) else None)
    return {"model_provider": provider, "turn_sandboxes": sandboxes}


def relay_commit() -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=("claude", "codex", "fixture"))
    ap.add_argument("--home", required=True, type=Path)
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sabotage-resume", action="store_true", help="(fixture only) self-test of the FAIL path")
    ap.add_argument("--notify", action="store_true", help="also check the notifier against a local fake webhook")
    a = ap.parse_args()
    if a.sabotage_resume and a.engine != "fixture":
        ap.error("--sabotage-resume is only for --engine fixture")
    a.workdir.mkdir(parents=True, exist_ok=True)
    if a.engine == "codex" and not (a.workdir / ".git").exists():
        subprocess.run(["git", "init", "-q", str(a.workdir)], check=True)
    report: Dict[str, Any] = {"engine": a.engine, "relay_commit": relay_commit(),
                              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": []}
    stamp = time.strftime("%Y%m%d%H%M%S") + f"-{os.getpid()}"

    def step(name: str, verdict: str, **data: Any) -> None:
        report["steps"].append({"name": name, "verdict": verdict, **data})

    def write_report() -> None:
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        a.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    relay(a.home, "init")
    hook = None
    if a.notify:
        hook = LocalWebhook()
        hook.configure(a.home)
    code, doc, cmd = relay(a.home, "doctor", "--engine", a.engine, "--cwd", str(a.workdir))
    step("preflight", "PASS" if code == 0 else "BLOCKED", command=cmd, engine_check=doc.get("engine"))
    if code != 0:
        report["result"] = "NOT RUN (auth path not confirmed as subscription)"
        if hook:
            hook.stop()
        write_report()
        print(json.dumps({"engine": a.engine, "result": report["result"]}))
        return 3

    if a.engine == "codex":
        opts, ok_opts, fail_opts = ["--option", 'sandbox="read-only"'], [], ["--option", 'model="cli-relay-no-such-model"']
    elif a.engine == "claude":
        opts, ok_opts, fail_opts = [], [], ["--option", 'model="cli-relay-no-such-model"']
    else:
        opts, ok_opts, fail_opts = [], ["--option", f'result="{PHRASE}"'], ["--option", "exit_code=1"]
    preassigned = a.engine in ("claude", "fixture")

    t0 = time.monotonic()
    code, sub, cmd = relay(a.home, "submit", "--key", f"real-{a.engine}-ok-{stamp}", "--engine", a.engine,
                           "--cwd", str(a.workdir), "--prompt", f"Reply with exactly: {PHRASE}", *opts, *ok_opts)
    _, done, _ = relay(a.home, "wait", sub["task_id"], "--timeout", "600")
    att = done["attempts"][0]
    ok = (done["task"]["state"] == "succeeded" and PHRASE in (excerpt(att) or "")
          and att["exit"] == {"code": 0, "signal": None, "known": True} and bool(att["native_session_id"]))
    if preassigned:
        ok = ok and att["native_session_id"] == sub["native_session_id"]
    record = codex_thread_record(att["native_session_id"]) if a.engine == "codex" else None
    if a.engine == "codex":
        ok = ok and record is not None and record["model_provider"] == "openai"
    step("success", "PASS" if ok else "FAIL", command=cmd, task_id=sub["task_id"], state=done["task"]["state"],
         native_session_id=att["native_session_id"], preassigned_session_id=sub.get("native_session_id"),
         exit=att["exit"], native_ok=(att.get("native") or {}).get("ok"), result_excerpt=excerpt(att),
         thread_record=record, seconds=round(time.monotonic() - t0, 1))
    ok_task, ok_sid = sub["task_id"], att["native_session_id"]

    t0 = time.monotonic()
    code, sub, cmd = relay(a.home, "submit", "--key", f"real-{a.engine}-fail-{stamp}", "--engine", a.engine,
                           "--cwd", str(a.workdir), "--prompt", "Reply with exactly: unused", *opts, *fail_opts)
    _, done, _ = relay(a.home, "wait", sub["task_id"], "--timeout", "600")
    att = done["attempts"][0]
    failed_ok = done["task"]["state"] == "failed" and att["exit"]["known"] and att["exit"]["code"] not in (0, None)
    step("failure_parsing", "PASS" if failed_ok else "FAIL", command=cmd, task_id=sub["task_id"],
         state=done["task"]["state"], exit=att["exit"], native=att.get("native"),
         seconds=round(time.monotonic() - t0, 1))
    fail_task = sub["task_id"]

    t0 = time.monotonic()
    if a.sabotage_resume:
        code, res, cmd = relay(a.home, "resume", ok_task, "--fresh", "--confirm-rerun")
    else:
        code, res, cmd = relay(a.home, "resume", ok_task, "--prompt",
                               "What exact phrase did you reply with in your previous message? "
                               "Reply with only that phrase.")
    _, done, _ = relay(a.home, "wait", ok_task, "--timeout", "600")
    att2 = done["attempts"][-1]
    resumed_ok = (code == 0 and res.get("resume_session_id") == ok_sid and done["task"]["state"] == "succeeded"
                  and PHRASE in (excerpt(att2) or "") and att2["native_session_id"] == ok_sid)
    record = codex_thread_record(ok_sid) if a.engine == "codex" else None
    if a.engine == "codex":
        # The resumed turn must run in the sandbox the task was submitted with.
        resumed_ok = resumed_ok and record is not None and record["turn_sandboxes"][-1:] == ["read-only"]
    step("exact_resume", "PASS" if resumed_ok else "FAIL", command=cmd, task_id=ok_task,
         attempt_no=att2["attempt_no"], resume_session_id=res.get("resume_session_id"),
         native_session_id=att2["native_session_id"], exit=att2["exit"], result_excerpt=excerpt(att2),
         thread_record=record, seconds=round(time.monotonic() - t0, 1))

    # Exactly one terminal event per attempt of this run; acknowledge those and nothing else.
    expected = sorted([(ok_task, 1), (fail_task, 1), (ok_task, 2)])
    _, evs, cmd = relay(a.home, "events", "--consumer", CONSUMER)
    mine = [ev for ev in evs["events"] if ev["task_id"] in (ok_task, fail_task)]
    seen = sorted((ev["task_id"], ev["attempt_no"]) for ev in mine)
    acked = []
    for ev in mine:
        c, _, _ = relay(a.home, "ack", "--consumer", CONSUMER, "--task", ev["task_id"], "--attempt",
                        str(ev["attempt_no"]), "--event", ev["event_id"])
        acked.append({"event_id": ev["event_id"], "task_id": ev["task_id"], "attempt_no": ev["attempt_no"],
                      "kind": ev["kind"], "outcome": ev["body"]["outcome"], "acked": c == 0})
    _, after, _ = relay(a.home, "events", "--consumer", CONSUMER)
    left_mine = [ev["event_id"] for ev in after["events"] if ev["task_id"] in (ok_task, fail_task)]
    events_ok = (seen == expected and all(x["acked"] and x["kind"] == "terminal" for x in acked)
                 and not left_mine)
    step("events_ack", "PASS" if events_ok else "FAIL", command=cmd, expected=[list(e) for e in expected],
         seen=[list(e) for e in seen], events=acked, other_events_left_unacked=len(after["events"]) - len(left_mine))

    if hook:
        # Each terminal event was sent by the pass its keeper started. Further passes record the
        # consumer ACKs of the previous step; a pass still sending is left to finish first.
        mine_ids = {x["event_id"] for x in acked}
        ledger_id = json.loads((a.home / "LEDGER.json").read_text(encoding="utf-8"))["ledger_id"]
        rows: Dict[str, Dict[str, Any]] = {}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            relay(a.home, "notify", "flush")
            _, st, _ = relay(a.home, "notify", "status")
            rows = {r["event_id"]: r for r in st["notifications"] if r["event_id"] in mine_ids}
            if set(rows) == mine_ids and all(r["state"] == "acked" for r in rows.values()):
                break
            time.sleep(1)
        sent = sorted(e["event_id"] for b in hook.bodies for e in b["cli_relay"]["events"])
        named = all((b["cli_relay"]["source_id"], b["cli_relay"]["ledger_id"], b["cli_relay"]["route"])
                    == ("acceptance", ledger_id, "local") for b in hook.bodies)
        notify_ok = (bool(mine_ids) and sent == sorted(mine_ids) and named and set(rows) == mine_ids
                     and all(r["state"] == "acked" and r["transport_accepted"] and r["consumer_acked"]
                             for r in rows.values()))
        step("notify", "PASS" if notify_ok else "FAIL", webhook_requests=len(hook.bodies), events_sent=len(sent),
             distinct_events_sent=len(set(sent)), named_source_ledger_route=named,
             states=sorted((r["state"], r["transport_accepted"], r["consumer_acked"]) for r in rows.values()))
        hook.stop()

    verdicts = [s["verdict"] for s in report["steps"]]
    report["result"] = "PASS" if all(v == "PASS" for v in verdicts) else "FAIL"
    write_report()
    print(json.dumps({"engine": a.engine, "result": report["result"], "verdicts": verdicts}))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

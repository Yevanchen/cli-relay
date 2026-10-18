"""Deterministic stand-in for a coding CLI, used by tests, CI and the soak run.

It speaks the same newline-delimited JSON shape as ``claude -p --output-format stream-json``
(an ``init`` event first, a ``result`` event last), keeps per-session history in a state
directory so exact resume can be checked, and appends one line per real execution to an
optional side-effect file so tests can count how often work actually ran.

Fixture-only semantics: a reused ``--session-id`` is rejected and resuming an unknown session
fails; neither is a claim about the real CLIs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stdout.flush()


def _append_durably(path: str, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(path: Path, turns: List[str]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"turns": turns}), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="cli-relay-fixture")
    p.add_argument("--state-dir", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--session-id")
    g.add_argument("--resume")
    p.add_argument("--duration", type=float, default=0.0)
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--result")
    p.add_argument("--side-effect-file")
    p.add_argument("--no-result", action="store_true")
    p.add_argument("--api-key-source", default="none")
    a = p.parse_args(argv)

    prompt = sys.stdin.read()
    state = Path(a.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    sid = a.session_id or a.resume
    session_file = state / f"{sid}.json"
    turns: List[str] = []
    if a.resume:
        if not session_file.exists():
            _emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": sid,
                   "result": f"No conversation found with session ID: {sid}"})
            return 1
        turns = json.loads(session_file.read_text(encoding="utf-8"))["turns"]
    elif session_file.exists():
        _emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": sid,
               "result": f"Session ID {sid} is already in use"})
        return 1

    _emit({"type": "system", "subtype": "init", "session_id": sid, "apiKeySource": a.api_key_source,
           "permissionMode": "default", "model": "fixture"})
    deadline = time.monotonic() + max(0.0, a.duration)
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(left, 1.0))
    if a.side_effect_file:
        _append_durably(a.side_effect_file, json.dumps({
            "session_id": sid, "attempt_id": os.environ.get("CLI_RELAY_ATTEMPT_ID"), "pid": os.getpid(),
            "at": time.time(), "resume": bool(a.resume)}) + "\n")
    turns.append(prompt)
    _save(session_file, turns)
    if a.no_result:
        return a.exit_code
    if a.result is not None:
        text = a.result
    elif a.resume:
        text = f"resumed turn {len(turns)}; previous prompt: {turns[-2][:60]}"
    else:
        text = "ok"
    _emit({"type": "result", "subtype": "success" if a.exit_code == 0 else "error_during_execution",
           "is_error": a.exit_code != 0, "result": text, "session_id": sid, "num_turns": len(turns)})
    return a.exit_code


if __name__ == "__main__":
    sys.exit(main())

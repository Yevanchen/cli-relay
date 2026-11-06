"""Own one disposable HTTP worker, so DNS/TLS/trickled reads have a total deadline.

Only this newly created transport worker is stopped on expiry. It never owns an engine,
ledger or ACK. No adapter output, URLs, tokens or exception text cross the result pipe.
"""
from __future__ import annotations

import multiprocessing
import time
from typing import Any, Dict

POLL_S = 0.05
CLEANUP_S = 0.2


def timing(start_wall: float, start_mono: float, timeout: float) -> Dict[str, Any]:
    wall, mono = time.time() - start_wall, time.monotonic() - start_mono
    return {"configured_timeout_s": timeout, "wall_elapsed_s": round(wall, 6),
            "monotonic_elapsed_s": round(mono, 6), "clock_divergence_s": round(wall - mono, 6),
            "clock_divergence_observed": abs(wall - mono) > 1.0,
            "suspend_verified": False}


def _worker(pipe, may_send, adapter, payload, env):
    from .notify import _http_exchange
    phase = "starting"

    def update(value):
        nonlocal phase
        if value == "request":
            may_send.value = 1  # set before request bytes; supervisor reads only after stopping us
        phase = value
        pipe.send(("phase", value))

    try:
        result = _http_exchange(adapter, payload, env, update)
    except Exception as exc:
        result = {"outcome": "ambiguous", "why": "adapter_error", "error": type(exc).__name__}
    try:
        pipe.send(("result", {**result, "phase": phase}))
    finally:
        pipe.close()


def _stop(process):
    if process.is_alive():
        process.terminate()
    process.join(CLEANUP_S)
    if process.is_alive():
        process.kill()
        process.join(CLEANUP_S)
    return not process.is_alive()


def run_http(adapter, payload, env):
    timeout = float(adapter["timeout_s"])
    start_wall, start_mono = time.time(), time.monotonic()
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    may_send = ctx.Value('b', 0, lock=False)
    process = ctx.Process(target=_worker, args=(writer, may_send, adapter, payload, env), daemon=True)
    phase, result = "starting", None
    try:
        process.start()
        writer.close()
        while result is None:
            remaining = min(timeout - (time.time() - start_wall), timeout - (time.monotonic() - start_mono))
            if remaining <= 0:
                stopped = _stop(process)
                before_send = stopped and not may_send.value
                result = {"outcome": "retry" if before_send else "ambiguous",
                          "why": "connect_failed" if before_send else "response_lost",
                          "error": "TimeoutError", "phase": phase, "deadline_exceeded": True,
                          "worker_stopped": stopped}
                if not stopped:
                    result["why"] = "transport_worker_stop_unverified"
                break
            if reader.poll(min(POLL_S, remaining)):
                try:
                    kind, value = reader.recv()
                except EOFError:
                    break
                if kind == "phase":
                    phase = value
                elif kind == "result":
                    result = value
            elif not process.is_alive():
                break
        if result is None:
            result = {"outcome": "ambiguous", "why": "transport_worker_lost", "phase": phase}
    finally:
        # A late DNS/connect/response cannot continue and send after we report expiry.
        if process.pid is not None:
            _stop(process)
        reader.close()
        writer.close()
    return {**result, "timing": timing(start_wall, start_mono, timeout)}

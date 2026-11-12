"""Home-wide admission settings and unprivileged, bounded memory observations.

Configuration shares notifier.json's source_id and JSON-in-home convention. Reading it
here must not open another ledger: admission already holds the SQLite write transaction.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import UsageError

MIB = 1024 * 1024
DEFAULTS = {"hard_max": 4, "fallback_max": 2, "min_available_mib": 1536}
ENV_SETTINGS = {
    "hard_max": "CLI_RELAY_LOCAL_HARD_MAX",
    "fallback_max": "CLI_RELAY_LOCAL_FALLBACK_MAX",
    "min_available_mib": "CLI_RELAY_LOCAL_MIN_AVAILABLE_MIB",
}
SOURCE_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")


def _bad(message: str) -> UsageError:
    return UsageError("local concurrency config: " + message)


def _settings(raw: Any, where: str) -> Dict[str, int]:
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise _bad(f"{where} must be an object with keys {sorted(DEFAULTS)}")
    for key, value in raw.items():
        minimum = 0 if key == "min_available_mib" else 1
        if type(value) is not int or value < minimum:
            raise _bad(f"{where}.{key} must be an integer >= {minimum}")
    return dict(raw)


def validate_config(raw: Any) -> Dict[str, Any]:
    """Validate partial overrides without filling defaults (machine overrides inherit)."""
    if not isinstance(raw, dict) or set(raw) - (set(DEFAULTS) | {"sources"}):
        raise _bad("local_concurrency has unknown keys or is not an object")
    settings: Dict[str, Any] = _settings({k: v for k, v in raw.items() if k != "sources"}, "local_concurrency")
    if "sources" in raw:
        sources = raw["sources"]
        if not isinstance(sources, dict):
            raise _bad("local_concurrency.sources must be an object keyed by source_id")
        for source, overrides in sources.items():
            if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
                raise _bad("source_id must be 1-32 chars of [A-Za-z0-9._-]")
            _settings(overrides, "local_concurrency.sources entry")
        settings["sources"] = {source: dict(overrides) for source, overrides in sources.items()}
    return settings


def load_config(home: Path, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    env = os.environ if env is None else env
    try:
        raw = json.loads((Path(home) / "notifier.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        raise _bad("notifier.json is unreadable or not JSON") from exc
    if not isinstance(raw, dict):
        raise _bad("notifier.json must be an object")
    source_id = raw.get("source_id")
    if source_id is not None and (not isinstance(source_id, str) or not SOURCE_RE.fullmatch(source_id)):
        raise _bad("source_id must be 1-32 chars of [A-Za-z0-9._-]")
    local = validate_config(raw.get("local_concurrency", {}))
    settings = dict(DEFAULTS)
    origins = {key: "default" for key in DEFAULTS}
    for layer, overrides in (("notifier.json", {k: v for k, v in local.items() if k != "sources"}),
                             ("source_id", local.get("sources", {}).get(source_id, {}))):
        settings.update(overrides)
        origins.update({key: layer for key in overrides})
    for key, name in ENV_SETTINGS.items():
        if name in env:
            try:
                if not re.fullmatch(r"[0-9]+", env[name]):
                    raise ValueError
                value = int(env[name])
            except ValueError as exc:
                raise _bad(f"{name} must be a decimal integer") from exc
            settings.update(_settings({key: value}, name))
            origins[key] = name
    return {**settings, "source_id": source_id, "setting_sources": origins}


def _vm_stat_available(output: str) -> int:
    page = re.search(r"page size of ([0-9]+) bytes", output)
    if page is None or int(page[1]) <= 0:
        raise ValueError("missing vm_stat page size")
    counts = []
    for field in ("free", "inactive", "purgeable"):
        match = re.search(rf"^Pages {field}:\s*([0-9]+)\.?\s*$", output, re.MULTILINE)
        if match is None:
            raise ValueError("missing vm_stat memory field")
        counts.append(int(match[1]))
    return sum(counts) * int(page[1])


def _meminfo_available(output: str) -> int:
    match = re.search(r"^MemAvailable:\s*([0-9]+)\s+kB\s*$", output, re.MULTILINE)
    if match is None:
        raise ValueError("missing MemAvailable")
    return int(match[1]) * 1024


@dataclass(frozen=True)
class Memory:
    available_bytes: Optional[int]
    source: str
    error: Optional[str] = None


def read_available_memory() -> Memory:
    source = "unsupported"
    try:
        if sys.platform == "darwin":
            source = "vm_stat:free+inactive+purgeable"
            result = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, check=True,
                                    stdin=subprocess.DEVNULL, timeout=1,
                                    env={**os.environ, "LC_ALL": "C"})
            return Memory(_vm_stat_available(result.stdout), source)
        if sys.platform.startswith("linux"):
            source = "/proc/meminfo:MemAvailable"
            return Memory(_meminfo_available(Path("/proc/meminfo").read_text(encoding="ascii")), source)
        return Memory(None, source, "unsupported_platform")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # No command output or configuration values in diagnostics. Never fail admission
        # just because a platform probe failed (including timeout and malformed output).
        return Memory(None, source, type(exc).__name__)


def snapshot(home: Path, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    try:
        config = load_config(home, env)
    except UsageError as exc:
        # A bad edit must not crash a waiting keeper or hide stored task results.
        # Queue until corrected rather than silently disregarding a configured cap.
        return {"ok": False, "max_active": 0, "mode": "invalid_config",
                "reason": "invalid_concurrency_config", "message": exc.message}
    memory = read_available_memory()
    minimum = config["min_available_mib"] * MIB
    if memory.available_bytes is None:
        limit = min(config["hard_max"], config["fallback_max"])
        mode, reason = "fixed_fallback", "memory_unavailable"
    elif memory.available_bytes < minimum:
        limit = 1  # Preserve progress even when the machine has no spare memory.
        mode, reason = "memory_limited", "low_available_memory"
    else:
        limit = config["hard_max"]
        mode, reason = "memory", None
    return {"ok": True, **config, "max_active": limit, "min_available_bytes": minimum,
            "available_bytes": memory.available_bytes, "memory_source": memory.source,
            "memory_error": memory.error, "mode": mode, "reason": reason}

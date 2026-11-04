"""Business self-reports, separate from the CLI process outcome.

Only the final non-empty result line and an explicitly requested JSON file report status.
Prose is never interpreted. Keep the aliases here so dispatchers can import the same mapping.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import UsageError

FIELDS = ("business_status", "business_status_source", "denials", "business_status_counts")
MAX_RESULT_FILE_BYTES = 1 << 20
_ALIASES = {"done": "ok", "ok": "ok", "partial": "partial", "failed": "failed", "fail": "failed",
            "stopped_for_approval": "stopped_for_approval", "needs_approval": "stopped_for_approval"}
_SEVERITY = {"unreported": -1, "ok": 0, "partial": 1, "stopped_for_approval": 2, "failed": 3}
_STATUS_TOKEN = re.compile(r"(?<!\S)status=(\w+)(?!\S)")
_COUNT_TOKEN = re.compile(r"(?<!\S)(ok|fail|skip)=([0-9]+)(?!\S)")


def normalize_status(status: str) -> str:
    """Map a reported status word to ok/partial/stopped_for_approval/failed/unreported."""
    return _ALIASES.get(status, "unreported")


def validate_result_file(value: str, cwd: Optional[str]) -> str:
    """A literal cwd-relative file path; reject traversal and existing symlink escapes."""
    if (cwd is None or not value or value != value.strip()
            or not re.fullmatch(r"[A-Za-z0-9_./ -]+", value)
            or ".." in value.split("/") or Path(value).is_absolute()):
        raise UsageError("result_file requires a relative file path inside the task cwd, without ..")
    path = Path(value)
    try:
        root = Path(cwd).resolve()
        resolved = (root / path).resolve()
        resolved.relative_to(root)
        if resolved == root or (resolved.exists() and not resolved.is_file()):
            raise ValueError("not a file")
    except (ValueError, OSError, RuntimeError) as exc:
        raise UsageError("result_file must stay inside the task cwd, including symlinks") from exc
    return path.as_posix()


def _file_status(value: str, cwd: str, warnings: List[Dict[str, Any]]) -> Optional[str]:
    def invalid(reason):
        warnings.append({"code": "business_status_result_file_invalid", "reason": reason})

    try:
        relative = validate_result_file(value, cwd)
        root = Path(cwd).resolve()
        path = (root / relative).resolve()
        path.relative_to(root)
    except (UsageError, ValueError, OSError, RuntimeError):
        invalid("unsafe_path")
        return None
    try:
        # A FIFO/device must never block the ledger's closing transaction. Re-check the open
        # file and cap the read as well as its stat size in case it grows while being read.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode):
                invalid("not_regular")
                return None
            if info.st_size > MAX_RESULT_FILE_BYTES:
                invalid("too_large")
                return None
            data = f.read(MAX_RESULT_FILE_BYTES + 1)
        if len(data) > MAX_RESULT_FILE_BYTES:
            invalid("too_large")
            return None
    except FileNotFoundError:
        return None  # an optional file that does not exist reports nothing
    except OSError:
        invalid("unreadable")
        return None
    try:
        result = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        invalid("invalid_json")
        return None
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        invalid("invalid_status")
        return None
    return result["status"]


def evaluate(engine: str, payload: Dict[str, Any], outcome: str,
             native: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Collect terminal evidence without changing outcome or assuming business completion.

    Reports use the more severe known status (failed > approval > partial > ok); ties prefer
    the status line. Non-success outcomes mean failed, except for a reported approval stop.
    On success, Claude denials turn ok or unreported into partial. No report means unreported.
    """
    options = payload.get("options") or {}
    warnings: List[Dict[str, Any]] = []
    reports: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    text = (native or {}).get("result")
    if isinstance(text, str) and text.strip():
        line = text.rstrip().splitlines()[-1]
        match = _STATUS_TOKEN.search(line)
        if match:
            reports["status_line"] = match.group(1)
            for key, value in _COUNT_TOKEN.findall(line):
                try:
                    counts[key] = int(value)
                except ValueError:  # Python's integer digit limit: ignore an oversized counter
                    warnings.append({"code": "business_status_counter_invalid", "counter": key})
    if "result_file" in options:
        reported = _file_status(options["result_file"], payload["cwd"], warnings)
        if reported is not None:
            reports["result_file"] = reported
    for source, word in reports.items():
        reports[source] = normalize_status(word)
        if reports[source] == "unreported":
            warnings.append({"code": "business_status_unknown", "source": source})
    if len(set(reports.values())) > 1:
        warnings.append({"code": "business_status_conflict", **reports})
    source = max(reports, key=lambda key: _SEVERITY[reports[key]]) if reports else "none"
    status = reports.get(source, "unreported")
    available = engine in ("claude", "fixture") and native is not None
    names = (native or {}).get("permission_denials") or []
    denials = {"count": len(names) if available else None,
               "tools": sorted(set(names)) if available else [], "available": available}
    if outcome != "succeeded":
        if status != "stopped_for_approval":
            status, source = "failed", "outcome"
    elif denials["count"] and status in ("ok", "unreported"):
        status, source = "partial", "denials"
    if options.get("require_business_status") is True and status == "unreported":
        warnings.append({"code": "business_status_missing"})
    return {"business_status": status, "business_status_source": source, "denials": denials,
            **({"business_status_counts": counts} if counts else {}),
            **({"warnings": warnings} if warnings else {})}

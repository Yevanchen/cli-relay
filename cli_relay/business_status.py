"""Deterministic business reports. Process outcome and file verification remain separate.

Only a structured result and a final marker count; prose never reports success. The bounded,
allowlisted snapshot and file hash are stored in the attempt's terminal transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import UsageError

FIELDS = ("business_status", "business_status_source", "business_stop_reason", "business_reports",
          "denials", "business_status_counts", "artifact_verification", "result_evidence", "completion_report")
MAX_RESULT_FILE_BYTES = 1 << 20
MAX_ARTIFACTS = 128
STOP_ALIASES = {"blocked": "blocked", "needs_user": "needs_user", "needs_input": "needs_user",
                "login_required": "needs_user", "stopped_for_approval": "stopped_for_approval",
                "needs_approval": "stopped_for_approval"}
_ALIASES = {"done": "ok", "ok": "ok", "partial": "partial", "failed": "failed", "fail": "failed",
            **{key: "partial" for key in STOP_ALIASES}}
_SEVERITY = {"unreported": -1, "ok": 0, "partial": 1, "failed": 2}
_MARKER = re.compile(r"(?:[A-Z][A-Z0-9_]*_DONE )?status=(\w+)(?: (?:ok|fail|skip)=[0-9]+)*")
_COUNT_TOKEN = re.compile(r"(?<!\S)(ok|fail|skip)=([0-9]+)(?!\S)")


COMPLETION_PROMPT = """任务结束时，在最终回复中各单独一行，严格使用以下格式：
做完没：做完 / 没做完 / 部分（只选一个）
还差：具体剩余事项（没有写 无）
下一步谁动：Evan / 某个 bot 的名字 / 没有（只写实际负责人）
如果任务要求写 JSON 结果文件，也在其中写字符串字段 done、remaining、next_owner，值与这三行一致。
如果任务还要求以状态标记作为最后一行，把这三行放在状态标记之前。"""


def completion_report(text: Any) -> Dict[str, Any]:
    """Accept one unambiguous, bounded line for each field in the final response."""
    fields = {"做完没": "done", "还差": "remaining", "下一步谁动": "next_owner"}
    found: Dict[str, List[str]] = {name: [] for name in fields}
    if isinstance(text, str):
        for line in text.splitlines():
            for label in fields:
                if line.startswith(label + "："):
                    found[label].append(line[len(label) + 1:].strip())
    if (any(len(values) != 1 for values in found.values()) or
            any(not value or len(value) > 512 or not value.isprintable()
                for values in found.values() for value in values) or
            found["做完没"][0] not in ("做完", "没做完", "部分")):
        return {"state": "没说做完没"}
    return {"state": "已报告", **{key: found[label][0] for label, key in fields.items()}}


def normalize_status(status: str) -> str:
    return _ALIASES.get(status, "unreported")


def validate_result_file(value: str, cwd: Optional[str]) -> str:
    """Literal cwd-relative path, without traversal or symlink escapes."""
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


def result_file_details(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Metadata only. Terminal callers freeze this, readers must not refresh history."""
    value = payload.get("options", {}).get("result_file")
    if value is None:
        return None
    path = Path(os.path.abspath(Path(payload["cwd"]) / value))
    try:
        return {"path": str(path), "exists": path.is_file()}
    except (OSError, RuntimeError):
        return {"path": str(path), "exists": False}


def artifact_paths(values: Any, cwd: str) -> List[str]:
    if not isinstance(values, list) or len(values) > MAX_ARTIFACTS:
        raise UsageError("required_artifacts/artifacts must be a list of at most 128 task-local file paths")
    root = Path(cwd).resolve()
    paths = []
    for value in values:
        if (not isinstance(value, str) or not 1 <= len(value) <= 1024 or value != value.strip()
                or any(not ch.isprintable() for ch in value) or ".." in value.split("/")):
            raise UsageError("artifact paths must be printable task-local file paths without ..")
        path = Path(value)
        path = path if path.is_absolute() else root / path
        try:
            path.resolve().relative_to(root)
            if path.resolve() == root:
                raise ValueError("root")
        except (ValueError, OSError, RuntimeError) as exc:
            raise UsageError("artifact path escapes the task cwd, including symlinks") from exc
        paths.append(str(path))
    return sorted(set(paths))


def _read_result(payload: Dict[str, Any], warnings: List[Dict[str, Any]]):
    value = payload["options"]["result_file"]
    meta = result_file_details(payload) or {}
    evidence: Dict[str, Any] = {**meta, "sha256": None, "snapshot": None}

    def invalid(reason):
        evidence["validation"] = reason
        warnings.append({"code": "business_status_result_file_invalid", "reason": reason,
                         "path": meta.get("path")})
        return None, evidence

    try:
        relative = validate_result_file(value, payload["cwd"])
        path = (Path(payload["cwd"]).resolve() / relative).resolve()
        # Never block on a FIFO/device or follow a substituted final symlink.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode):
                return invalid("not_regular")
            if info.st_size > MAX_RESULT_FILE_BYTES:
                return invalid("too_large")
            data = f.read(MAX_RESULT_FILE_BYTES + 1)
        if len(data) > MAX_RESULT_FILE_BYTES:
            return invalid("too_large")
        evidence.update(exists=True, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    except FileNotFoundError:
        evidence["exists"] = False
        # Report the common path mismatch without reading/using the alternate result.
        candidate = Path(payload["cwd"]) / Path(value).name
        if candidate != Path(meta["path"]) and candidate.is_file():
            warnings.append({"code": "business_status_result_path_mismatch", "declared_path": meta["path"],
                             "candidate_path": str(candidate), "candidate_used": False})
        return invalid("missing")
    except (UsageError, ValueError, RuntimeError):
        return invalid("unsafe_path")
    except OSError:
        return invalid("unreadable")

    def unique(pairs):
        result = {}
        for key, val in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = val
        return result

    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        return invalid("invalid_json")
    if not isinstance(result, dict):
        return invalid("invalid_object")
    # No summary, prose, environment, denial arguments or arbitrary fields are captured.
    evidence["snapshot"] = {k: result[k][:128] if isinstance(result[k], str) else None
                            for k in ("status", "stop_reason") if k in result}
    evidence["validation"] = "parsed"
    return result, evidence


def evaluate(engine: str, payload: Dict[str, Any], outcome: str,
             native: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    options = payload.get("options") or {}
    warnings: List[Dict[str, Any]] = []
    reports: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    invalid_report = False
    text = (native or {}).get("result")
    if isinstance(text, str) and text.strip():
        line = text.rstrip().splitlines()[-1]
        match = _MARKER.fullmatch(line)
        if match:
            reports["status_line"] = {"raw_status": match.group(1)}
            for key, value in _COUNT_TOKEN.findall(line):
                try:
                    counts[key] = int(value)
                except ValueError:
                    warnings.append({"code": "business_status_counter_invalid", "counter": key})
    result = None
    result_evidence = None
    if "result_file" in options:
        result, result_evidence = _read_result(payload, warnings)
        invalid_report = result is None
        if result is not None:
            reports["result_file"] = {"raw_status": result.get("status"),
                                      "raw_stop_reason": result.get("stop_reason")}
    for source, report in reports.items():
        raw, reason = report["raw_status"], report.get("raw_stop_reason")
        status = normalize_status(raw) if isinstance(raw, str) and len(raw) <= 128 else "unreported"
        stop = STOP_ALIASES.get(raw) if isinstance(raw, str) else None
        if reason is not None:
            if isinstance(reason, str) and reason in STOP_ALIASES:
                stop = STOP_ALIASES[reason]
            else:
                status = "unreported"
                warnings.append({"code": "business_status_stop_reason_invalid", "source": source})
        if stop and status == "ok":
            status = "partial"
            warnings.append({"code": "business_status_stop_conflict", "source": source})
        report.update(raw_status=raw[:128] if isinstance(raw, str) else None,
                      raw_stop_reason=reason[:128] if isinstance(reason, str) else None,
                      status=status, stop_reason=stop)
        if status == "unreported":
            invalid_report = True
            warnings.append({"code": "business_status_unknown", "source": source})
    if (len({r["status"] for r in reports.values()}) > 1 or
            len({r["stop_reason"] for r in reports.values() if r["stop_reason"]}) > 1):
        warnings.append({"code": "business_status_conflict",
                         **{s: r["status"] for s, r in reports.items()}})
    # More severe wins; ties prefer the structured file. Invalid reports block promotion to ok.
    source = max(reports, key=lambda s: (_SEVERITY[reports[s]["status"]], s == "result_file")) if reports else "none"
    status = reports[source]["status"] if reports else "unreported"
    stops = [r["stop_reason"] for r in reports.values() if r["stop_reason"]]
    stop = next((s for s in ("stopped_for_approval", "needs_user", "blocked") if s in stops), None)
    if invalid_report and status == "ok":
        status, source = "unreported", "invalid_report"
    if stop and status == "ok":
        status = "partial"

    checks = []
    artifact_invalid = False
    for label, values in (("submitted_required", options.get("required_artifacts", [])),
                          ("required", (result or {}).get("required_artifacts", [])),
                          ("reported", (result or {}).get("artifacts", []))):
        try:
            for path in artifact_paths(values, payload.get("cwd", ".")):
                checks.append({"path": path, "exists": Path(path).is_file(), "declaration": label})
        except (UsageError, OSError, RuntimeError):
            artifact_invalid = True
            warnings.append({"code": "business_status_artifacts_invalid", "declaration": label})
    missing = [c for c in checks if not c["exists"]]
    if missing:
        warnings.append({"code": "business_status_artifacts_missing", "paths": [c["path"] for c in missing]})
    verification = {"state": "failed" if artifact_invalid or missing else ("passed" if checks else "not_declared"),
                    "method": "local_file_existence_only", "checks": checks}
    if result_evidence is not None:
        result_evidence["artifact_verification"] = verification
    if status == "ok" and verification["state"] == "failed":
        status, source = "partial", "artifacts"
    available = engine in ("claude", "fixture") and native is not None
    names = (native or {}).get("permission_denials") or []
    denials = {"count": len(names) if available else None,
               "tools": sorted(set(names)) if available else [], "available": available}
    if outcome != "succeeded":
        stop = stop or {"blocked": "blocked", "needs_input": "needs_user"}.get(outcome)
        status, source = ("partial", "outcome") if stop and status != "failed" else ("failed", "outcome")
    elif denials["count"] and status in ("ok", "unreported"):
        status, source = "partial", "denials"
    if status == "unreported" and options.get("require_business_status") is True:
        warnings.append({"code": "business_status_missing"})
    return {"completion_report": completion_report(text),
            "business_status": status, "business_status_source": source, "business_stop_reason": stop,
            "business_reports": reports, "artifact_verification": verification, "denials": denials,
            **({"result_evidence": result_evidence} if result_evidence is not None else {}),
            **({"business_status_counts": counts} if counts else {}),
            **({"warnings": warnings} if warnings else {})}

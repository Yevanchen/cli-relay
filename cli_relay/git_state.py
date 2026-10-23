"""Best-effort, read-only Git observations. Never decide an attempt's outcome."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, Optional

GIT_TIMEOUT_S = 2.0


def _read(cwd: str, *args: str) -> Optional[bytes]:
    # Do not let a parent Git process redirect these reads to another work tree or index.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", cwd, *args],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def snapshot(cwd: str) -> Optional[Dict[str, Any]]:
    """None if no work tree is detected; each unavailable observation is also None.

    --no-optional-locks prevents status from refreshing/writing the index. No filenames,
    command output or error text leave this helper. Staged, unstaged and untracked files count.
    """
    if _read(cwd, "rev-parse", "--is-inside-work-tree") != b"true\n":
        return None
    head = _read(cwd, "rev-parse", "--verify", "HEAD")
    head = head.strip() if head is not None else b""
    status = _read(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=none")
    return {"head": head.decode("ascii") if re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) else None,
            "dirty": bool(status) if status is not None else None}


def completion(cwd: str, before: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    after = snapshot(cwd)
    if before is None and after is None:
        return {}
    return {"git": {"head_before": (before or {}).get("head"), "head_after": (after or {}).get("head"),
                    "dirty": (after or {}).get("dirty")}}

"""Process identity: a pid is only trusted together with its OS-reported start time.

A bare pid can be recycled, so every liveness answer here is three-valued:
True (same process, alive), False (gone or recycled), None (cannot verify).
Callers must treat None conservatively (as "may still be running").
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional, Tuple

_PS_ENV = {"LC_ALL": "C", "PATH": "/bin:/usr/bin:/usr/sbin:/sbin"}


def _linux_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


_DARWIN_LIBC = None


def _darwin_proc_info(pid: int) -> Optional[Tuple[str, str]]:
    """Microsecond start time from sysctl(KERN_PROC_PID) -> kinfo_proc.

    ``ps -o lstart`` only has one-second resolution, so a pid recycled within the same second
    would look identical; the kernel's ``p_starttime`` (struct timeval) does not have that
    problem. The layout is validated by checking the pid field inside the returned struct.
    """
    global _DARWIN_LIBC
    import ctypes
    import ctypes.util
    import struct

    try:
        if _DARWIN_LIBC is None:
            _DARWIN_LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc = _DARWIN_LIBC
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID, pid
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value == 0:
            return ("", "")  # sentinel: no such process
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value < 44:
            return ("", "")
        tv_sec, tv_usec = struct.unpack_from("qi", buf.raw, 0)  # extern_proc.p_un.__p_starttime
        p_stat = buf.raw[36]                                     # extern_proc.p_stat
        p_pid = struct.unpack_from("i", buf.raw, 40)[0]          # extern_proc.p_pid
    except (OSError, AttributeError, ValueError, struct.error):
        return None
    if p_pid != pid:
        return None  # unexpected layout: let the caller fall back to ps
    return ("Z" if p_stat == 5 else "R", f"darwin:{tv_sec}.{tv_usec:06d}")  # 5 == SZOMB


def _proc_info(pid: int) -> Optional[Tuple[str, str]]:
    """Return (state, start_token) for a live or zombie pid, or None if absent/unreadable."""
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        info = _darwin_proc_info(pid)
        if info == ("", ""):
            return None
        if info is not None:
            return info
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read().decode("utf-8", "replace")
            # The comm field may contain spaces and parentheses: split after the last ')'.
            rest = data[data.rindex(")") + 2:].split()
            state, starttime = rest[0], rest[19]  # fields 3 and 22 of proc(5)
        except (OSError, ValueError, IndexError):
            return None
        return state, f"linux:{_linux_boot_id()}:{starttime}"
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=,lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            env=_PS_ENV,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip()
    if out.returncode != 0 or not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    return parts[0], "ps:" + " ".join(parts[1:])


def start_token(pid: int) -> Optional[str]:
    """Opaque OS start-time token for pid; None if the process is gone or unreadable."""
    info = _proc_info(pid)
    if info is None or info[0].startswith("Z"):
        return None
    return info[1]


def pid_exists(pid: Optional[int]) -> Optional[bool]:
    if not pid or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def same_process_alive(pid: Optional[int], token: Optional[str]) -> Optional[bool]:
    """True: pid is alive and is the recorded process. False: gone, zombie or recycled.
    None: cannot verify (no pid/token recorded, or the OS query failed)."""
    if not pid:
        return None
    exists = pid_exists(pid)
    if exists is False:
        return False
    info = _proc_info(pid)
    if info is None:
        # Raced with exit, or the query failed: re-check existence before answering.
        return False if pid_exists(pid) is False else None
    state, current = info
    if state.startswith("Z"):
        return False
    if token is None:
        return None
    return current == token


def group_alive(pgid: Optional[int]) -> Optional[bool]:
    """Whether any process is left in process group pgid (None if no pgid recorded)."""
    if not pgid or pgid <= 1:
        return None
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

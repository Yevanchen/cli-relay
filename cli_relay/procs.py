"""Process identity: a pid is only trusted together with its OS-reported start time.

A bare pid can be recycled, so every liveness answer here is three-valued:
True (same process, alive), False (gone or recycled), None (cannot verify).
Callers must treat None conservatively (as "may still be running").

Start times come from the kernel only (macOS: kinfo_proc, Linux: /proc). There is no
fallback to another source: a token of a different source or precision cannot be compared,
so a failed query answers None rather than "recycled".
"""

from __future__ import annotations

import ctypes.util
import os
import sys
from typing import List, Optional, Tuple


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
        return None  # unexpected layout: unverifiable
    return ("Z" if p_stat == 5 else "R", f"darwin:{tv_sec}.{tv_usec:06d}")  # 5 == SZOMB


def _proc_info(pid: int) -> Optional[Tuple[str, str]]:
    """Return (state, start_token) for a live or zombie pid, or None if absent/unreadable."""
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        info = _darwin_proc_info(pid)
        return None if info == ("", "") else info
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
    return None  # other platforms: no kernel start time, so identities stay unverifiable


def _compare_tokens(recorded: str, current: str) -> Optional[bool]:
    """Same process? None when the two tokens do not come from the same source and precision, or
    when either lacks a part: an identity with a missing part proves nothing, even if both sides
    are missing the same part."""
    source, _, rest = recorded.partition(":")
    cur_source, _, cur_rest = current.partition(":")
    if source != cur_source or not rest or not cur_rest:
        return None
    if source == "linux":
        boot, _, start = rest.rpartition(":")
        cur_boot, _, cur_start = cur_rest.rpartition(":")
        if not (boot and cur_boot and start and cur_start):
            return None  # e.g. the boot id could not be read
        if boot != cur_boot:
            return False  # a different boot means the recorded process is gone
        return start == cur_start
    return rest == cur_rest


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
    None: cannot verify (no pid/token recorded, the OS query failed, or the recorded token
    is not comparable with what the OS reports now)."""
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
    return _compare_tokens(token, current)


def _darwin_group_members(pgid: int) -> Optional[List[int]]:
    """Live (non-zombie) pids in process group pgid via sysctl(KERN_PROC_PGRP). The element size is
    taken from a single-process query, and the layout is checked by finding the caller in the list
    when it belongs to the group."""
    import ctypes
    import struct

    try:
        libc = _DARWIN_LIBC or ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        # The size of one kinfo_proc is what the kernel writes for one process (a size-only query
        # returns an estimate with room to spare).
        one = (ctypes.c_int * 4)(1, 14, 1, os.getpid())  # KERN_PROC_PID
        estimate = ctypes.c_size_t(0)
        if libc.sysctl(one, 4, None, ctypes.byref(estimate), None, 0) != 0 or estimate.value < 44:
            return None
        first = ctypes.create_string_buffer(estimate.value)
        elem = ctypes.c_size_t(estimate.value)
        if libc.sysctl(one, 4, first, ctypes.byref(elem), None, 0) != 0 or elem.value < 44:
            return None
        mib = (ctypes.c_int * 4)(1, 14, 2, pgid)  # CTL_KERN, KERN_PROC, KERN_PROC_PGRP, pgid
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
            return None
        buf = ctypes.create_string_buffer(size.value + elem.value * 16)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value % elem.value:
            return None
        members = []
        for off in range(0, size.value, elem.value):
            p_stat = buf.raw[off + 36]
            p_pid = struct.unpack_from("i", buf.raw, off + 40)[0]
            if p_pid <= 0:
                return None
            if p_stat != 5:  # SZOMB: already dead
                members.append(p_pid)
    except (OSError, AttributeError, ValueError, struct.error):
        return None
    if os.getpgid(0) == pgid and os.getpid() not in members:
        return None  # unexpected layout
    return members


def _linux_group_members(pgid: int) -> Optional[List[int]]:
    members = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                data = f.read().decode("utf-8", "replace")
            rest = data[data.rindex(")") + 2:].split()
            if int(rest[2]) == pgid and not rest[0].startswith("Z"):  # fields 5 (pgrp) and 3 (state)
                members.append(int(name))
        except (OSError, ValueError, IndexError):
            continue  # exited while listing
    return members


def group_members(pgid: int) -> Optional[List[int]]:
    """Live pids in process group pgid; None if they cannot be listed on this platform."""
    if pgid <= 1:
        return None
    if sys.platform == "darwin":
        return _darwin_group_members(pgid)
    if sys.platform.startswith("linux"):
        return _linux_group_members(pgid)
    return None


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

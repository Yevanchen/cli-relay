"""Process identity: pid + OS start time must tell apart processes started within one second."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

import helpers  # noqa: F401  (puts the repository on sys.path)

from cli_relay import procs


class ProcessIdentity(unittest.TestCase):
    def test_own_process_has_a_token(self) -> None:
        tok = procs.start_token(os.getpid())
        self.assertTrue(tok)
        self.assertIs(procs.same_process_alive(os.getpid(), tok), True)

    def test_processes_started_back_to_back_have_distinct_tokens(self) -> None:
        # Resolution: microseconds on macOS (kinfo_proc), clock ticks (typically 10 ms) on Linux.
        # Either is far below the time it takes the OS to recycle a pid; 50 ms apart must differ.
        import time
        a = subprocess.Popen(["/bin/sleep", "5"])
        time.sleep(0.05)
        b = subprocess.Popen(["/bin/sleep", "5"])
        try:
            ta, tb = procs.start_token(a.pid), procs.start_token(b.pid)
            self.assertTrue(ta and tb)
            self.assertNotEqual(ta, tb, "sub-second resolution is required to detect pid reuse")
            self.assertIs(procs.same_process_alive(a.pid, ta), True)
            self.assertIs(procs.same_process_alive(a.pid, tb), False, "another process's token must not match")
        finally:
            for p in (a, b):
                p.kill()
                p.wait()
        self.assertIs(procs.same_process_alive(a.pid, ta), False)
        self.assertIsNone(procs.start_token(a.pid))

    @unittest.skipUnless(sys.platform == "darwin", "macOS kernel struct check")
    def test_darwin_kinfo_layout_is_validated(self) -> None:
        info = procs._darwin_proc_info(os.getpid())
        self.assertIsNotNone(info)
        state, token = info
        self.assertTrue(token.startswith("darwin:"))
        self.assertEqual(state, "R")

    def test_zombie_is_not_alive(self) -> None:
        import time
        p = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        time.sleep(0.5)  # exited but not reaped: the pid still exists, as a zombie
        self.assertIs(procs.pid_exists(p.pid), True)
        self.assertIs(procs.same_process_alive(p.pid, "any-token"), False)
        p.wait()

    def test_group_probe(self) -> None:
        p = subprocess.Popen(["/bin/sleep", "5"], start_new_session=True)
        try:
            self.assertIs(procs.group_alive(p.pid), True)
        finally:
            p.kill()
            p.wait()
        self.assertIs(procs.group_alive(p.pid), False)


if __name__ == "__main__":
    unittest.main()

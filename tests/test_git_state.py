"""Git probes are bounded, read-only, optional observations (issue #11)."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

import helpers  # noqa: F401

from cli_relay import git_state


class GitProbes(unittest.TestCase):
    def test_argv_reads_disable_index_writes_and_do_not_inherit_git_routing(self):
        head = b"a" * 40
        outputs = [b"true\n", head + b"\n", b"?? private-name\0"]
        with patch.dict("os.environ", {"GIT_DIR": "/other", "GIT_INDEX_FILE": "/other/index"}), \
                patch("cli_relay.git_state.subprocess.run", side_effect=[
                    subprocess.CompletedProcess([], 0, stdout=out) for out in outputs]) as run:
            self.assertEqual(git_state.snapshot("/work dir"), {"head": head.decode(), "dirty": True})
        commands = []
        for call in run.call_args_list:
            argv = call.args[0]
            commands.append(argv[6:])
            self.assertEqual(argv[:6], ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", "/work dir"])
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 5)
            self.assertFalse(call.kwargs.get("shell", False))
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertFalse(any(key.startswith("GIT_") for key in call.kwargs["env"]))
        self.assertEqual(commands, [["rev-parse", "--is-inside-work-tree"], ["rev-parse", "--verify", "HEAD"],
                                    ["status", "--porcelain=v1", "-z", "--untracked-files=normal",
                                     "--ignore-submodules=none"]])

    def test_missing_git_nonzero_exit_and_timeouts_return_null(self):
        for failure in (FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired("git", 2)):
            with self.subTest(failure=type(failure).__name__), \
                    patch("cli_relay.git_state.subprocess.run", side_effect=failure):
                self.assertIsNone(git_state.snapshot("/work"))
        with patch("cli_relay.git_state.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 128, stdout=b"error")):
            self.assertIsNone(git_state.snapshot("/work"))

    def test_individual_observations_are_null_on_failure(self):
        for head, status, expected in ((None, None, {"head": None, "dirty": None}),
                                       (None, b"?? new\0", {"head": None, "dirty": True}),
                                       (b"not a hash", b"", {"head": None, "dirty": False})):
            with self.subTest(expected=expected), patch("cli_relay.git_state._read",
                                                       side_effect=[b"true\n", head, status]):
                self.assertEqual(git_state.snapshot("/work"), expected)

    def test_failed_end_probe_preserves_before_head_with_null_end_fields(self):
        before = {"head": "a" * 40, "dirty": False}
        with patch("cli_relay.git_state.snapshot", return_value=None):
            self.assertEqual(git_state.completion("/work", before),
                             {"git": {"head_before": "a" * 40, "head_after": None, "dirty": None}})
            self.assertEqual(git_state.completion("/work", None), {})

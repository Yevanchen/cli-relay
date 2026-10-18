"""Audit #2: a Codex resume must keep the sandbox the task was submitted with."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from cli_relay import engines


class CodexResumeKeepsSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-relay-a02-"))
        fake = self.tmp / "codex"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        self.env = {"PATH": "/usr/bin:/bin", "CLI_RELAY_CODEX_BIN": str(fake)}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def argv(self, resume, sandbox):
        attempt = {"native_session_id": None, "resume_session_id": resume, "attempt_id": "a-x"}
        payload = {"cwd": str(self.tmp), "options": {"sandbox": sandbox}}
        return engines.build("codex", payload=payload, attempt=attempt, run_dir=self.tmp,
                             fixture_state_dir=str(self.tmp), env=self.env).argv

    def test_resume_passes_the_stored_sandbox(self) -> None:
        for sandbox in ("read-only", "workspace-write"):
            first = self.argv(None, sandbox)
            resumed = self.argv("33333333-3333-4333-8333-333333333333", sandbox)
            self.assertEqual(first[first.index("--sandbox") + 1], sandbox)
            self.assertIn("--sandbox", resumed, "resume must not fall back to whatever the config says")
            self.assertEqual(resumed[resumed.index("--sandbox") + 1], sandbox)
            self.assertLess(resumed.index("--sandbox"), resumed.index("resume"),
                            "codex only accepts --sandbox as an exec option before the subcommand")
            self.assertIn(f'sandbox_mode="{sandbox}"', resumed, "and as a config override for the resumed thread")


if __name__ == "__main__":
    unittest.main()

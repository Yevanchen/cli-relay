"""Codex billing defaults and persisted opt-out, using synthetic environments and CLIs only."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config
from test_task_local_model_config import recording_cli
from test_terminal_observations import notification

from cli_relay import engines
from cli_relay.errors import UsageError
from cli_relay.ledger import payload_hash

CANARY = "synthetic-billing-value-never-record"
NAMES = sorted(engines.BILLING_ENV["codex"])
SNAPSHOT_FIELDS = ("billing_mode", "billing_env_policy", "billing_env_present",
                   "billing_env_removed", "billing_env_preserved")


class CodexBillingUnit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="cli-relay-billing-unit-")
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name).resolve()
        self.env = isolated_env(self.work, {"CLI_RELAY_CODEX_BIN": "/bin/echo",
            "CLI_RELAY_CLAUDE_BIN": "/bin/echo", **dict.fromkeys(NAMES, CANARY),
            "ANTHROPIC_API_KEY": CANARY, "RELAY_ENV_KEEP": "keep", "CLAUDECODE": "nested",
            "CLI_RELAY_TEST_CRASH": "unused"})

    def build(self, options, resume=None, engine="codex"):
        return engines.build(engine, payload={"cwd": str(self.work), "options": options},
            attempt={"native_session_id": "test-session", "resume_session_id": resume, "attempt_id": "a-test"},
            run_dir=self.work, fixture_state_dir=str(self.work / "sessions"), env=self.env)

    def preflight(self, options=None, auth="Logged in using ChatGPT", code=0):
        with patch.object(engines, "codex_config_problems", return_value=([], [])), \
                patch.object(engines.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["stub", "login", "status"], code, auth, "")) as run:
            pf = engines.preflight("codex", self.env, cwd=str(self.work), options=options)
        return pf, run.call_args.kwargs["env"]

    def test_mode_is_a_codex_only_closed_enum_without_echoing_invalid_values(self):
        for mode in engines.CODEX_BILLING_MODES:
            self.assertEqual(engines.validate_options("codex", {"billing_mode": mode}), {"billing_mode": mode})
        for value in (True, False, 1, 0, None, [], {}, "API", "", CANARY):
            with self.subTest(value=value), self.assertRaises(UsageError) as caught:
                engines.validate_options("codex", {"billing_mode": value})
            self.assertNotIn(CANARY, json.dumps(caught.exception.to_json()))
        for engine in ("claude", "fixture"):
            with self.assertRaises(UsageError):
                engines.validate_options(engine, {"billing_mode": "api"})

    def test_default_and_explicit_subscription_remove_all_recognized_names_even_empty(self):
        original = dict(self.env)
        for options in ({}, {"billing_mode": "subscription"}, {"unset_env": []}):
            for resume in (None, "old-session"):
                for value in (CANARY, ""):
                    with self.subTest(options=options, resume=resume, empty=value == ""):
                        self.env.update(dict.fromkeys(NAMES, value))
                        before = dict(self.env)
                        spec = self.build(options, resume)
                        snapshot = engines.billing_env_evidence("codex", self.env, options)
                        self.assertFalse(set(NAMES) & spec.env.keys())
                        self.assertEqual(snapshot["billing_env_present"], NAMES)
                        self.assertEqual(snapshot["billing_env_removed"], NAMES)
                        self.assertEqual(snapshot["billing_env_preserved"], [])
                        self.assertEqual(snapshot["billing_env_policy"], "subscription_isolation")
                        self.assertEqual(spec.env["ANTHROPIC_API_KEY"], CANARY)
                        self.assertEqual(spec.env["RELAY_ENV_KEEP"], "keep")
                        for name in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "PATH"):
                            self.assertEqual(spec.env[name], before[name])
                        self.assertNotIn("CLAUDECODE", spec.env)
                        self.assertNotIn("CLI_RELAY_TEST_CRASH", spec.env)
                        self.assertEqual(self.env, before)
        self.env.update(original)

    def test_subscription_policy_uses_the_preflight_billing_set(self):
        name = "SYNTHETIC_FUTURE_BILLING_ENV"
        self.env[name] = CANARY
        with patch.dict(engines.BILLING_ENV, {"codex": (*engines.BILLING_ENV["codex"], name)}):
            spec = self.build({})
            pf, auth_env = self.preflight()
        self.assertNotIn(name, spec.env)
        self.assertNotIn(name, auth_env)
        self.assertIn(name, pf.detail["billing_env_removed"])

    def test_explicit_api_preserves_billing_env_and_explicit_unset_still_wins_on_resume(self):
        original = dict(self.env)
        for resume in (None, "old-session"):
            api = self.build({"billing_mode": "api"}, resume)
            snapshot = engines.billing_env_evidence("codex", self.env, {"billing_mode": "api"})
            self.assertEqual({name: api.env[name] for name in NAMES}, dict.fromkeys(NAMES, CANARY))
            self.assertEqual(snapshot["billing_env_preserved"], NAMES)
            self.assertEqual(snapshot["billing_env_removed"], [])
            self.assertEqual(snapshot["billing_env_policy"], "explicit_api")
            options = {"billing_mode": "api", "unset_env": ["OPENAI_BASE_URL", "ANTHROPIC_API_KEY"]}
            cleaned = self.build(options, resume)
            snapshot = engines.billing_env_evidence("codex", self.env, options)
            self.assertNotIn("OPENAI_BASE_URL", cleaned.env)
            self.assertNotIn("ANTHROPIC_API_KEY", cleaned.env)
            self.assertEqual(cleaned.env["OPENAI_API_KEY"], CANARY)
            self.assertEqual(snapshot["billing_env_removed"], ["OPENAI_BASE_URL"])
            self.assertEqual(cleaned.argv, api.argv)
            self.assertNotIn(CANARY, json.dumps(snapshot))
        self.assertEqual(self.env, original)

    def test_preflight_and_auth_child_are_isolated_before_subscription_check(self):
        original = dict(self.env)
        pf, auth_env = self.preflight()
        self.assertTrue(pf.ok)
        self.assertFalse(set(NAMES) & auth_env.keys())
        self.assertEqual(pf.detail["billing_env_removed"], NAMES)
        self.assertEqual(auth_env["RELAY_ENV_KEEP"], "keep")
        self.assertNotIn(CANARY, json.dumps(pf.detail))
        self.assertEqual(self.env, original)

    def test_api_key_login_requires_explicit_task_selection(self):
        blocked, clean_env = self.preflight(auth="Logged in using an API key")
        self.assertEqual((blocked.ok, blocked.reason), (False, "auth_not_subscription"))
        self.assertFalse(set(NAMES) & clean_env.keys())
        allowed, api_env = self.preflight({"billing_mode": "api"}, auth="Logged in using an API key")
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.detail["auth"], "api_key")
        self.assertEqual(allowed.detail["billing_env_preserved"], NAMES)
        self.assertEqual({name: api_env[name] for name in NAMES}, dict.fromkeys(NAMES, CANARY))

    def test_explicit_api_does_not_approve_failed_or_unknown_login(self):
        for auth, code in (("Not logged in", 1), ("Logged in using an API key", 1), ("unknown", 0)):
            with self.subTest(auth=auth, code=code):
                self.assertFalse(self.preflight({"billing_mode": "api"}, auth, code)[0].ok)

    def test_both_modes_keep_configuration_and_permission_guards(self):
        for mode in engines.CODEX_BILLING_MODES:
            for problem in ("requirements", "model_provider", "model_providers.openai", "base_url",
                            "forced_login_method", "permissions", "user:unreadable"):
                with self.subTest(mode=mode, problem=problem), \
                        patch.object(engines, "codex_config_problems", return_value=([problem], [])), \
                        patch.object(engines.subprocess, "run") as run:
                    pf = engines.preflight("codex", self.env, options={"billing_mode": mode})
                    self.assertEqual((pf.ok, pf.reason), (False, "config_not_subscription"))
                    self.assertEqual(pf.detail["settings"], [problem])
                    run.assert_not_called()

    def test_absent_names_and_explicit_api_are_observably_different(self):
        for name in NAMES:
            self.env.pop(name)
        for mode in engines.CODEX_BILLING_MODES:
            pf, _ = self.preflight({"billing_mode": mode})
            snapshot = pf.detail
            self.assertEqual(snapshot["billing_mode"], mode)
            for field in ("billing_env_present", "billing_env_removed", "billing_env_preserved"):
                self.assertEqual(snapshot[field], [])

    def test_non_codex_defaults_and_parent_environment_are_unchanged(self):
        for engine in ("claude", "fixture"):
            spec = self.build({}, engine=engine)
            self.assertEqual({name: spec.env[name] for name in NAMES}, dict.fromkeys(NAMES, CANARY))
            self.assertFalse(set(SNAPSHOT_FIELDS) & spec.evidence.keys())
            pf = engines.preflight(engine, self.env)
            self.assertEqual(pf.ok, engine == "fixture")
            if engine == "claude":
                self.assertEqual(pf.reason, "billing_env_present")
            self.assertFalse(set(SNAPSHOT_FIELDS) & pf.detail.keys())
        # This interpreter receives a synthetic parent environment; never snapshot host secrets.
        probe = '''import os
from pathlib import Path
from unittest.mock import patch
from cli_relay import engines
before = dict(os.environ)
engines.build("codex", payload={"cwd": os.environ["HOME"], "options": {}},
    attempt={"resume_session_id": None}, run_dir=Path(os.environ["HOME"]),
    fixture_state_dir=os.environ["HOME"], env=dict(os.environ))
with patch.object(engines, "codex_config_problems", return_value=([], [])), \\
     patch.object(engines, "resolve_bin", return_value=None):
    engines.preflight("codex", dict(os.environ))
assert dict(os.environ) == before, "parent environment mutated"
'''
        run = subprocess.run([sys.executable, "-c", probe], env=base_env(self.env),
                             text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)


@needs_clean_cli_config
class CodexBillingRoundTrip(RelayTestCase):
    def fake_env(self, api_auth=False):
        fake = recording_cli(self.tmp, "codex")
        observed = self.tmp / "observed.jsonl"
        check = (f"names = {NAMES!r}\n"
                 "check = {'phase': 'auth' if args[0] == 'login' else 'run',\n"
                 "         'present': [name for name in names if name in os.environ],\n"
                 "         'kept': os.environ.get('RELAY_ENV_KEEP') == 'keep',\n"
                 "         'scrubbed': 'CLAUDECODE' not in os.environ}\n"
                 f"with open({str(observed)!r}, 'a') as f:\n    f.write(json.dumps(check) + '\\n')\n")
        script = fake.read_text().replace("args = sys.argv[1:]\n", "args = sys.argv[1:]\n" + check)
        if api_auth:
            script = script.replace("Logged in using ChatGPT", "Logged in using an API key")
        fake.write_text(script)
        return base_env(isolated_env(self.tmp, {"CLI_RELAY_CODEX_BIN": str(fake),
            **dict.fromkeys(NAMES, CANARY), "RELAY_ENV_KEEP": "keep", "CLAUDECODE": "nested"}))

    def submit_codex(self, key, env, *flags):
        return self.cli("submit", "--key", key, "--engine", "codex", "--cwd", str(self.work),
                        "--prompt", "synthetic task", *flags, env=env)

    def finish(self, sub):
        return self.cli("wait", sub["task_id"], "--timeout", "30")

    def assert_snapshot(self, done, mode, present, removed, preserved):
        expected = {"billing_mode": mode,
                    "billing_env_policy": "explicit_api" if mode == "api" else "subscription_isolation",
                    "billing_env_present": present, "billing_env_removed": removed, "billing_env_preserved": preserved}
        for record in (done["attempts"][-1]["evidence"], done["events"][-1]["body"],
                       notification(done)["cli_relay"]["events"][-1]):
            self.assertEqual({key: record[key] for key in SNAPSHOT_FIELDS}, expected)
        log = self.home / "runs" / done["task"]["task_id"] / str(done["attempts"][-1]["attempt_no"]) / "keeper.log"
        self.assertIn("billing environment: ", log.read_text())
        self.assertNotIn(CANARY, json.dumps(done))
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes(), str(path))

    def observations(self):
        return [json.loads(line) for line in (self.tmp / "observed.jsonl").read_text().splitlines()]

    def test_legacy_default_hash_is_preserved_and_first_resume_fresh_are_isolated(self):
        env = self.fake_env()
        env["OPENAI_API_KEY"] = ""  # presence, including empty values, is observable
        original = dict(env)
        sub = self.submit_codex("default", env)
        expected = {"engine": "codex", "cwd": str(self.work.resolve()), "prompt": "synthetic task", "options": {}}
        self.assertEqual(sub["payload_sha256"], payload_hash(expected))
        self.assertEqual(sub["billing_mode"], "subscription")
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(done["task"]["options"], {})
        self.assert_snapshot(done, "subscription", NAMES, NAMES, [])
        resumed = self.cli("resume", sub["task_id"], "--prompt", "continue", env=env)
        self.assertEqual(resumed["billing_mode"], "subscription")
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertTrue(done["attempts"][-1]["resume_session_id"])
        self.assert_snapshot(done, "subscription", NAMES, NAMES, [])
        self.cli("resume", sub["task_id"], "--fresh", "--confirm-rerun", env=env)
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_snapshot(done, "subscription", NAMES, NAMES, [])
        self.assertEqual(done["task"]["payload_sha256"], sub["payload_sha256"])
        self.assertEqual(self.observations(), [{"phase": phase, "present": [], "kept": True, "scrubbed": True}
                                              for phase in ("auth", "run") * 3])
        self.assertEqual(env, original)

    def test_api_selection_and_explicit_unset_are_persisted_and_reused_by_resume(self):
        env = self.fake_env(api_auth=True)
        sub = self.submit_codex("api", env, "--billing-mode", "api", "--unset-env", "OPENAI_BASE_URL")
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks WHERE task_id=?", (sub["task_id"],))[0][0])
        self.assertEqual(stored["options"], {"billing_mode": "api", "unset_env": ["OPENAI_BASE_URL"]})
        self.assertEqual(sub["payload_sha256"], payload_hash(stored))
        preserved = sorted(set(NAMES) - {"OPENAI_BASE_URL"})
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_snapshot(done, "api", NAMES, ["OPENAI_BASE_URL"], preserved)
        resumed = self.cli("resume", sub["task_id"], "--prompt", "continue", env=env)
        self.assertEqual(resumed["billing_mode"], "api")
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_snapshot(done, "api", NAMES, ["OPENAI_BASE_URL"], preserved)
        self.assertEqual(self.observations(), [{"phase": phase, "present": preserved, "kept": True, "scrubbed": True}
                                              for phase in ("auth", "run") * 2])
        duplicate = self.submit_codex("api", env, "--option", "billing_mode=api",
                                      "--option", 'unset_env=["OPENAI_BASE_URL"]')
        self.assertEqual((duplicate["status"], duplicate["payload_sha256"]), ("duplicate", sub["payload_sha256"]))
        self.cli("submit", "--key", "api", "--engine", "codex", "--cwd", str(self.work),
                 "--prompt", "synthetic task", env=env, expect=3)

    def test_invalid_or_conflicting_selection_is_refused_before_creating_a_task(self):
        args = ["submit", "--key", "invalid", "--engine", "codex", "--cwd", str(self.work), "--prompt", "work"]
        self.cli(*args, "--billing-mode", "api", "--option", "billing_mode=subscription", expect=2)
        for engine in ("claude", "fixture"):
            self.cli("submit", "--key", "invalid", "--engine", engine, "--cwd", str(self.work),
                     "--prompt", "work", "--billing-mode", "api", expect=2)
        self.assertEqual(db_rows(self.home, "SELECT task_id FROM tasks"), [])

    def test_doctor_reports_default_policy_with_names_only(self):
        env = self.fake_env()
        doctor = self.cli("doctor", "--engine", "codex", "--cwd", str(self.work), env=env)
        self.assertTrue(doctor["engine"]["ok"])
        self.assertEqual(doctor["engine"]["billing_mode"], "subscription")
        self.assertEqual(doctor["engine"]["billing_env_removed"], NAMES)
        self.assertNotIn(CANARY, json.dumps(doctor))

    def test_api_selection_keeps_runtime_provider_and_permission_checks(self):
        env = self.fake_env(api_auth=True)
        fake = Path(env["CLI_RELAY_CODEX_BIN"])
        original = fake.read_text()
        for label, old, new, reason in (
            ("provider", '"model_provider":"openai"', '"model_provider":"other"', "provider_check"),
            ("permission", '"type":"read-only"', '"type":"danger-full-access"', "permission_check"),
        ):
            # The fake CLI uses one deterministic thread id; each case needs its own rollout.
            env["CODEX_HOME"] = str(self.tmp / label / "codex-home")
            Path(env["CODEX_HOME"]).mkdir(parents=True)
            fake.write_text(original.replace(old, new))
            done = self.finish(self.submit_codex(label, env, "--billing-mode", "api"))
            self.assertEqual(done["task"]["state"], "blocked")
            self.assertIn(reason, done["attempts"][-1]["evidence"])
            self.assert_snapshot(done, "api", NAMES, [], NAMES)


if __name__ == "__main__":
    unittest.main()

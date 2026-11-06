"""Issue #23: task-local billing environment cleanup, with names-only durable evidence."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import RelayTestCase, base_env, db_rows, isolated_env, needs_clean_cli_config, wait_until, production_args
from test_task_local_model_config import recording_cli
from test_terminal_observations import notification

from cli_relay import engines, procs
from cli_relay.errors import UsageError
from cli_relay.ledger import payload_hash

CANARY = "billing-environment-value-never-recorded"
NAMES = list(engines.UNSETTABLE_ENV)


class UnsetEnvOptionsAndArgv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cli-relay-unset-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.env = base_env({**dict.fromkeys(NAMES, CANARY), "RELAY_ENV_KEEP": "keep",
                             "CLAUDECODE": "nested", "CLI_RELAY_TEST_CRASH": "unused",
                             "CLI_RELAY_CODEX_BIN": "/bin/echo", "CLI_RELAY_CLAUDE_BIN": "/bin/echo"})

    def build(self, engine, options, resume=None):
        return engines.build(engine, payload={"cwd": str(self.work), "options": options},
                             attempt={"native_session_id": "test-session", "resume_session_id": resume,
                                      "attempt_id": "a-test"}, run_dir=self.work,
                             fixture_state_dir=str(self.work / "sessions"), env=self.env)

    def test_all_billing_names_are_supported_sorted_and_deduplicated_for_every_engine(self):
        values = list(reversed(NAMES)) + NAMES[:2]
        for engine in engines.ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(engines.validate_options(engine, {"unset_env": values}), {"unset_env": NAMES})
                self.assertEqual(engines.validate_options(engine, {}), {})
                self.assertEqual(engines.validate_options(engine, {"unset_env": []}), {"unset_env": []})
        self.assertEqual(values, list(reversed(NAMES)) + NAMES[:2])

    def test_invalid_types_names_assignments_and_non_billing_names_are_rejected(self):
        invalid = [None, True, 1, "OPENAI_BASE_URL", {}, [None], [1], [True], [[]], [{}],
                   [""], ["1OPENAI"], ["OPENAI-BASE-URL"], [" OPENAI_BASE_URL"], ["OPENAI_BASE_URL "],
                   ["OPENAI_BASE_URL\n"], ["变量"], ["$(id)"], ["OPENAI_BASE_URL\0"],
                   ["OPENAI_BASE_URL=" + CANARY], ["HOME"], ["PATH"], ["CODEX_HOME"],
                   ["CLI_RELAY_KEEPER_DETACHED"], ["CUSTOM_VAR"]]
        for engine in engines.ENGINES:
            for value in invalid:
                with self.subTest(engine=engine, value=value), self.assertRaises(UsageError) as caught:
                    engines.validate_options(engine, {"unset_env": value})
                self.assertNotIn(CANARY, json.dumps(caught.exception.to_json()))

    def test_first_and_resumed_builds_keep_explicit_cleanup_and_codex_subscription_default(self):
        original = dict(self.env)
        for engine in engines.ENGINES:
            for resume in (None, "old-session"):
                with self.subTest(engine=engine, resume=resume):
                    default = self.build(engine, {}, resume)
                    if engine == "codex":
                        self.assertNotIn("OPENAI_BASE_URL", default.env)
                    else:
                        self.assertEqual(default.env["OPENAI_BASE_URL"], CANARY)
                    spec = self.build(engine, {"unset_env": ["OPENAI_BASE_URL"]}, resume)
                    self.assertNotIn("OPENAI_BASE_URL", spec.env)
                    if engine == "codex":
                        self.assertNotIn("OPENAI_API_KEY", spec.env)
                    else:
                        self.assertEqual(spec.env["OPENAI_API_KEY"], CANARY)
                    self.assertEqual(spec.env["RELAY_ENV_KEEP"], "keep")
                    self.assertNotIn("CLAUDECODE", spec.env)
                    self.assertNotIn("CLI_RELAY_TEST_CRASH", spec.env)
                    self.assertEqual(spec.argv, default.argv)
        self.assertEqual(self.env, original)

    def test_actual_fixture_child_and_exact_resume_observe_the_clean_environment(self):
        # Probe inside the fixture interpreter, then run its normal session/result protocol.
        probe = ("import os, sys\nfrom cli_relay.fixture import main\n"
                 f"assert not any(name in os.environ for name in {NAMES!r})\n"
                 "assert os.environ.get('RELAY_ENV_KEEP') == 'keep'\n"
                 "assert 'CLAUDECODE' not in os.environ\nraise SystemExit(main())\n")
        for turn, resume in enumerate((None, "test-session"), 1):
            spec = self.build("fixture", {"unset_env": NAMES}, resume)
            run = subprocess.run([spec.argv[0], "-c", probe, *spec.argv[3:]], input="work",
                                 text=True, capture_output=True, cwd=spec.cwd, env=spec.env, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout.splitlines()[-1])
            self.assertEqual((result["is_error"], result["num_turns"]), (False, turn))


@needs_clean_cli_config
class UnsetEnvPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cli-relay-unset-preflight-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.env = isolated_env(self.work, {
            f"CLI_RELAY_{engine.upper()}_BIN": str(recording_cli(self.work, engine))
            for engine in ("claude", "codex")})

    def test_each_billing_name_is_isolated_for_codex_and_still_blocks_claude_without_cleanup(self):
        for engine in ("claude", "codex"):
            for name in engines.BILLING_ENV[engine]:
                with self.subTest(engine=engine, name=name):
                    env = dict(self.env, **{name: CANARY})
                    blocked = engines.preflight(engine, env, cwd=str(self.work))
                    if engine == "codex":
                        self.assertTrue(blocked.ok)
                        self.assertEqual(blocked.detail["billing_env_removed"], [name])
                        continue
                    self.assertEqual((blocked.ok, blocked.reason), (False, "billing_env_present"))
                    self.assertEqual(blocked.detail["names"], [name])
                    fix = {"submit_flags": "--unset-env " + name, "env_prefix": "env -u " + name}
                    self.assertEqual(blocked.detail["fix"], fix)
                    for command in fix.values():
                        self.assertIn(command, blocked.detail["message"])
                    self.assertNotIn(CANARY, json.dumps(blocked.detail))
                    self.assertTrue(engines.preflight(engine, env, cwd=str(self.work),
                                                     options={"unset_env": [name]}).ok)
                    self.assertEqual(env[name], CANARY)

    def test_names_keep_original_order_and_partial_cleanup_does_not_bypass_guard(self):
        for engine in ("claude",):
            names = list(engines.BILLING_ENV[engine])
            env = dict(self.env, **dict.fromkeys(reversed(names), CANARY))
            blocked = engines.preflight(engine, env)
            self.assertEqual(blocked.detail["names"], names)
            self.assertEqual(blocked.detail["fix"]["submit_flags"],
                             " ".join("--unset-env " + name for name in names))
            self.assertEqual(blocked.detail["fix"]["env_prefix"],
                             "env " + " ".join("-u " + name for name in names))
            partial = engines.preflight(engine, env, options={"unset_env": names[:1]})
            self.assertEqual((partial.ok, partial.reason), (False, "billing_env_present"))
            self.assertEqual(partial.detail["names"], names[1:])

    def test_cleanup_cannot_override_billing_configuration(self):
        configs = {"claude": (Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json", '{"apiKeyHelper":"unused"}'),
                   "codex": (Path(self.env["CODEX_HOME"]) / "config.toml", 'model_provider = "other"\n')}
        for engine, (path, data) in configs.items():
            path.write_text(data)
            env = dict(self.env, **dict.fromkeys(engines.BILLING_ENV[engine], CANARY))
            pf = engines.preflight(engine, env, cwd=str(self.work), options={"unset_env": NAMES})
            self.assertEqual((pf.ok, pf.reason), (False, "config_not_subscription"))
            self.assertEqual(path.read_text(), data)


class UnsetEnvRoundTrip(RelayTestCase):
    def args(self, key, engine="fixture"):
        return ["submit", *production_args(engine), "--key", key, "--engine", engine, "--cwd", str(self.work), "--prompt", "do it"]

    def finish(self, sub, env=None):
        return self.cli("wait", sub["task_id"], "--timeout", "30", env=env)

    def assert_cleanup(self, done, names, present, index=-1):
        records = (done["attempts"][index]["evidence"], done["events"][index]["body"],
                   notification(done)["cli_relay"]["events"][0])
        for record in records:
            self.assertEqual(record["unset_env"], names)
            self.assertEqual(record["unset_env_present"], present)
        self.assertNotIn(CANARY, json.dumps(done))

    def test_omitting_cleanup_preserves_old_payload_hash_and_output_fields(self):
        sub = self.cli(*self.args("default"))
        done = self.finish(sub)
        expected = {"engine": "fixture", "cwd": str(self.work.resolve()), "prompt": "do it", "options": {}}
        stored = json.loads(db_rows(self.home, "SELECT payload_json FROM tasks")[0]["payload_json"])
        self.assertEqual(stored, expected)
        self.assertEqual(sub["payload_sha256"], payload_hash(expected))
        for record in (sub, done["task"], done["attempts"][0]["evidence"], done["events"][0]["body"],
                       notification(done)["cli_relay"]["events"][0]):
            self.assertNotIn("unset_env", record)
            self.assertNotIn("unset_env_present", record)

    def test_flags_and_json_normalize_duplicates_and_conflicting_lists_are_rejected(self):
        args = self.args("normalized")
        sub = self.cli(*args, "--unset-env", "OPENAI_BASE_URL", "--unset-env", "CODEX_API_KEY",
                       "--unset-env", "OPENAI_BASE_URL", "--option", 'unset_env=["CODEX_API_KEY","OPENAI_BASE_URL"]')
        names = ["CODEX_API_KEY", "OPENAI_BASE_URL"]
        self.assertEqual(sub["unset_env"], names)
        done = self.finish(sub)
        self.assertEqual(done["task"]["options"], {"unset_env": names})
        self.assert_cleanup(done, names, [])
        duplicate = self.cli(*args, "--option", 'unset_env=["OPENAI_BASE_URL","CODEX_API_KEY","CODEX_API_KEY"]')
        self.assertEqual((duplicate["status"], duplicate["payload_sha256"]), ("duplicate", sub["payload_sha256"]))
        self.cli(*args, "--unset-env", "OPENAI_BASE_URL", expect=3)
        self.cli(*args, expect=3)
        self.cli(*self.args("conflict"), "--unset-env", "OPENAI_BASE_URL", "--option", 'unset_env=[]', expect=2)
        explicit_empty = self.cli(*self.args("empty"), "--option", 'unset_env=[]')
        self.assert_cleanup(self.finish(explicit_empty), [], [])

    def test_invalid_inputs_do_not_create_tasks_or_echo_values(self):
        for name in ("", "1BAD", "BAD-NAME", "OPENAI_BASE_URL\n", "OPENAI_BASE_URL=" + CANARY,
                     "HOME", "CODEX_HOME", "CLI_RELAY_KEEPER_DETACHED"):
            out = self.cli(*self.args("invalid"), "--unset-env", name, expect=2)
            self.assertNotIn(CANARY, json.dumps(out))
        for value in ('"OPENAI_BASE_URL"', 'null', '{}', '[null]', '[true]', '[1]'):
            self.cli(*self.args("invalid"), "--option", "unset_env=" + value, expect=2)
        self.assertEqual(db_rows(self.home, "SELECT task_id FROM tasks"), [])

    def test_snapshot_tracks_presence_including_empty_values_on_resume_and_fresh_rerun(self):
        names = ["CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"]
        env = base_env({"OPENAI_BASE_URL": CANARY, "OPENAI_API_KEY": "", "RELAY_ENV_KEEP": "keep"})
        sub = self.cli(*self.args("resume"), "--option", "unset_env=" + json.dumps(names), env=env)
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_cleanup(done, names, ["OPENAI_API_KEY", "OPENAI_BASE_URL"])
        resumed = self.cli("resume", sub["task_id"], "--prompt", "continue")
        self.assertEqual(resumed["unset_env"], names)
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(done["attempts"][-1]["resume_session_id"], sub["native_session_id"])
        self.assert_cleanup(done, names, [])
        self.cli("resume", sub["task_id"], "--fresh", "--confirm-rerun", env=env)
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assert_cleanup(done, names, ["OPENAI_API_KEY", "OPENAI_BASE_URL"])
        self.assertEqual(done["task"]["payload_sha256"], sub["payload_sha256"])
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes(), str(path))

    def test_billing_blocks_without_flag_and_event_and_doctor_include_copyable_fixes(self):
        for engine in ("claude",):
            names = list(engines.BILLING_ENV[engine])
            env = base_env(dict.fromkeys(names, CANARY))
            done = self.finish(self.cli(*self.args(engine, engine), env=env))
            self.assertEqual(done["task"]["state"], "blocked")
            doctor = self.cli("doctor", "--engine", engine, "--cwd", str(self.work), env=env, expect=6)
            for pf in (done["attempts"][0]["evidence"]["preflight"], done["events"][0]["body"]["preflight"],
                       notification(done)["cli_relay"]["events"][0]["preflight"], doctor["engine"]):
                self.assertEqual((pf["reason"], pf["names"]), ("billing_env_present", names))
                for command in pf["fix"].values():
                    self.assertIn(command, pf["message"])
            self.assertNotIn(CANARY, json.dumps(done))
            self.assertNotIn(CANARY, json.dumps(doctor))

    def test_cleanup_evidence_survives_keeper_crash_and_reconciliation(self):
        env = base_env({"OPENAI_BASE_URL": CANARY, "CLI_RELAY_TEST_CRASH": "after_exit_before_commit"})
        sub = self.cli(*self.args("crash"), "--unset-env", "OPENAI_BASE_URL", env=env)
        wait_until(lambda: (lambda row: row["keeper_pid"] and procs.same_process_alive(
            row["keeper_pid"], row["keeper_token"]) is False)(self.attempt_row(sub["task_id"])))
        done = self.finish(sub)
        self.assertEqual(done["task"]["state"], "succeeded")
        self.assertEqual(done["events"][0]["body"]["source"], "reconcile")
        self.assert_cleanup(done, ["OPENAI_BASE_URL"], ["OPENAI_BASE_URL"])

    @needs_clean_cli_config
    def test_fake_engine_auth_and_cli_children_are_clean_on_first_run_and_exact_resume(self):
        for engine in ("claude", "codex"):
            fake = recording_cli(self.tmp, engine)
            observed = self.tmp / (engine + "-environment.jsonl")
            check = (f"checks = {{name: name in os.environ for name in {NAMES!r}}}\n"
                     "checks['kept'] = os.environ.get('RELAY_ENV_KEEP') == 'keep'\n"
                     "checks['scrubbed'] = 'CLAUDECODE' not in os.environ\n"
                     f"with open({str(observed)!r}, 'a') as f:\n    f.write(json.dumps(checks) + '\\n')\n")
            script = fake.read_text().replace("args = sys.argv[1:]\n", "args = sys.argv[1:]\n" + check)
            fake.write_text(script)
            env = base_env(isolated_env(self.tmp, {f"CLI_RELAY_{engine.upper()}_BIN": str(fake),
                **dict.fromkeys(NAMES, CANARY), "RELAY_ENV_KEEP": "keep", "CLAUDECODE": "nested"}))
            sub = self.cli(*self.args("clean-" + engine, engine),
                           "--option", "unset_env=" + json.dumps(NAMES), env=env)
            self.assertEqual(self.finish(sub)["task"]["state"], "succeeded")
            self.cli("resume", sub["task_id"], "--prompt", "continue", env=env)
            done = self.finish(sub)
            self.assertEqual(done["task"]["state"], "succeeded")
            self.assert_cleanup(done, NAMES, NAMES)
            checks = [json.loads(line) for line in observed.read_text().splitlines()]
            self.assertEqual(checks, [{**dict.fromkeys(NAMES, False), "kept": True, "scrubbed": True}] * 4)
            # Claude still needs explicit cleanup; Codex now isolates its own billing names.
            default = self.finish(self.cli(*self.args("default-" + engine, engine), env=env))
            self.assertEqual(default["task"]["state"], "succeeded" if engine == "codex" else "blocked")


if __name__ == "__main__":
    unittest.main()

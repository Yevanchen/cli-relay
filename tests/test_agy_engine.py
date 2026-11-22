"""Neon highest_quality, Carbon opt-in and print adapter. Synthetic contracts only.

The fake contract is deliberately unlike an asserted agy output format. It is injected only
inside these test processes; production has no switch to bypass the pending acceptance gates.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from helpers import REPO, base_env, isolated_env, wait_until
from cli_relay import business_status, cli, concurrency, engines, policy, procs, reconcile
from cli_relay.errors import UsageError
from cli_relay.ledger import Ledger, init_ledger


MODELS = [model for _, model, _ in policy.CARBON_TIERS['carbon']]
NEON = 'gemini-4-neon-xhigh'


def fake_version(text):
    return text.strip().removeprefix('TEST-AGY-VERSION ') if text.startswith('TEST-AGY-VERSION ') else None


def fake_models(text):
    data = json.loads(text)
    models = data.get('test_models')
    return models if isinstance(models, list) and all(isinstance(m, str) for m in models) else None


def fake_error(rc, stderr):
    first = stderr.splitlines()[0] if stderr else ''
    errors = {f'TEST-AGY-ERROR {name}': f'agy_{name}' for name in (
        'auth_required', 'subscription_unavailable', 'quota_exhausted', 'non_subscription_route', 'needs_input')}
    return errors.get(first)


FAKE_CONTRACT = engines.AgyContract(
    version='1.5.11', version_args=('--version',), models_args=('models',),
    parse_version=fake_version, parse_models=fake_models, classify_error=fake_error,
    subscription_verified=True)


def fake_cli(root):
    binary = root / 'fake-agy'
    binary.write_text(f'#!{sys.executable}\n' + r'''
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
record = {'argv': args, 'cwd': os.getcwd(), 'tty': sys.stdin.isatty()}
if args and args[0] == '--print':
    assert len(args) == 3 and args[1] == '--model'
    record['prompt'] = sys.stdin.read()
with open(os.environ['AGY_FAKE_LOG'], 'a') as stream:
    stream.write(json.dumps(record) + '\n')
if args == ['--version']:
    time.sleep(float(os.environ.get('AGY_FAKE_PROBE_DELAY', '0')))
    print(os.environ.get('AGY_FAKE_VERSION', 'TEST-AGY-VERSION 1.5.11'))
    if os.environ.get('AGY_FAKE_NOISE'):
        print('X' * 100000)
elif args == ['models']:
    print(os.environ.get('AGY_FAKE_MODELS', json.dumps({'test_models':
        ['gemini-4-neon-xhigh'] + ['gemini-4-carbon-' + e for e in ('low', 'medium', 'high')]})))
    print(os.environ.get('AGY_FAKE_PROBE_STDERR', ''), file=sys.stderr)
    sys.exit(int(os.environ.get('AGY_FAKE_PROBE_EXIT', '0')))
elif args and args[0] == '--print':
    if os.environ.get('AGY_FAKE_DESCENDANT'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
        Path(os.environ['AGY_FAKE_DESCENDANT']).write_text(str(child.pid))
    print(os.environ.get('AGY_FAKE_RESULT', '做完没：做完\n还差：无\n下一步谁动：没有\nstatus=ok\nCLI-RELAY-STATUS: complete'), flush=True)
    time.sleep(float(os.environ.get('AGY_FAKE_DURATION', '0')))
    print(os.environ.get('AGY_FAKE_STDERR', ''), file=sys.stderr)
    sys.exit(int(os.environ.get('AGY_FAKE_EXIT', '0')))
else:
    sys.exit(97)  # login/auth/install/unknown flags are never accepted
''')
    binary.chmod(0o755)
    return binary


class AgyCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='cli-relay-agy-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.work = self.root / 'work'
        self.work.mkdir()
        self.home = self.root / 'ledger'
        init_ledger(self.home)
        self.db = Ledger(self.home)
        self.addCleanup(self.db.close)
        self.binary = fake_cli(self.root)
        self.log = self.root / 'fake-calls.jsonl'
        self.env = base_env({'HOME': str(self.root / 'user'), 'CLI_RELAY_AGY_BIN': str(self.binary),
            'AGY_FAKE_LOG': str(self.log), 'PYTHONPATH': str(REPO) + os.pathsep + str(REPO / 'tests'),
            'CLI_RELAY_LOCAL_HARD_MAX': '4', 'CLI_RELAY_LOCAL_MIN_AVAILABLE_MIB': '0'})
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)

    def payload(self, **overrides):
        return {'engine': 'agy', 'task_class': 'carbon', 'resource_class': 'standard',
                'cwd': str(self.work), 'prompt': 'synthetic prompt',
                'options': {'model': MODELS[2], 'effort': 'high'}, **overrides}

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def print_calls(self):
        return [c for c in self.calls() if c['argv'][0] == '--print']

    def preflight(self, **env):
        return engines.preflight('agy', {**self.env, **env}, str(self.work), options=self.payload()['options'])

    def build(self, **attempt):
        return engines.build('agy', payload=self.payload(), attempt=attempt, run_dir=self.root,
                             fixture_state_dir=str(self.root), env=self.env)

    def spawn_keeper(self, home, task_id, attempt_id):
        code = ('from pathlib import Path; from cli_relay import engines; '
                'from test_agy_engine import FAKE_CONTRACT; engines.AGY_CONTRACT=FAKE_CONTRACT; '
                'from cli_relay.keeper import run; import sys; '
                'raise SystemExit(run(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))')
        with (self.root / 'test-keeper.log').open('ab') as output:
            child = subprocess.Popen([sys.executable, '-c', code, str(home), task_id, attempt_id],
                env={**self.env, 'CLI_RELAY_KEEPER_DETACHED': '1'}, cwd=self.work,
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        self.children.append(child)

    def command(self, *args, expect=0):
        output = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT), \
                patch.object(cli, 'spawn_keeper', self.spawn_keeper), patch.object(cli, 'Ledger', return_value=self.db), \
                contextlib.redirect_stdout(output):
            code = cli.main(['--home', str(self.home), *args])
        data = json.loads(output.getvalue())
        self.assertEqual(code, expect, data)
        return data

    def submit(self, key='carbon', *, task_class='carbon', effort=None, prompt='synthetic prompt',
               options=None, extra=(), expect=0):
        effort = effort or ('xhigh' if task_class == 'highest_quality' else 'high')
        model = NEON if task_class == 'highest_quality' else f'gemini-4-carbon-{effort}'
        opts = {'model': model, 'effort': effort, **(options or {})}
        args = ['submit', '--key', key, '--engine', 'agy', '--task-class', task_class,
                '--resource-class', 'standard', '--cwd', str(self.work), '--prompt', prompt]
        for name, value in opts.items():
            args += ['--option', name + '=' + json.dumps(value)]
        return self.command(*args, *extra, expect=expect)

    def settle(self, task_id, state=None):
        def poll():
            data = self.command('status', task_id)
            return data if (data['task']['state'] == state if state else
                            data['task']['state'] in ('succeeded', 'failed', 'blocked', 'cancelled', 'needs_input', 'unknown')) else None
        return wait_until(poll, timeout=30, interval=0.1)


class CarbonPolicy(AgyCase):
    def test_three_tiers_order_and_new_neon_default(self):
        self.assertEqual(list(policy.TIERS.items()), [
            ('simple', (('codex', 'gpt-6.1-terra', 'high'),)),
            ('complex', (('codex', 'gpt-6.1-sol', 'max'),)),
            ('highest_quality', (('agy', NEON, 'xhigh'),))])
        self.assertEqual(policy.TASK_CLASSES, ('simple', 'complex', 'highest_quality', 'carbon'))

    def test_exact_three_carbon_combinations_and_settings(self):
        for model, effort in zip(MODELS, ('low', 'medium', 'high')):
            payload = self.payload(options={'model': model, 'effort': effort})
            policy.validate_new(payload)
            engines.validate_options('agy', payload['options'])
            settings = policy.settings('agy', payload)['effective_settings']
            self.assertEqual((settings['engine'], settings['task_class'], settings['resource_class'],
                              settings['model'], settings['effort'], settings['verification']),
                             ('agy', 'carbon', 'standard', model, effort, 'invocation_only'))

    def test_invalid_models_efforts_classes_and_missing_fields(self):
        for model, effort in [(MODELS[2], 'medium'), (MODELS[2], 'xhigh'), (MODELS[2], 'max'),
                ('gemini-4-carbon', 'high'), ('gemini-pro-orchestrator', 'high'),
                ('gemini-4-carbon-high-alias', 'high'), ('unknown', 'low'), ('', 'high'),
                (None, 'high'), (MODELS[2], None)]:
            with self.subTest(model=model, effort=effort), self.assertRaises(UsageError):
                policy.validate_new(self.payload(options={'model': model, 'effort': effort}))
        for field in ('model', 'effort'):
            payload = self.payload()
            del payload['options'][field]
            with self.assertRaises(UsageError):
                policy.validate_new(payload)
        for field in ('task_class', 'resource_class'):
            payload = self.payload()
            del payload[field]
            with self.assertRaises(UsageError):
                policy.validate_new(payload)
        for override in [{'engine': e} for e in ('fixture', 'codex', 'claude')] + [
                {'task_class': t} for t in policy.TIERS] + [
                {'resource_class': 'computer_use'}, {'test_only_reason': 'no provider please'}]:
            with self.subTest(override=override), self.assertRaises(UsageError):
                policy.validate_new(self.payload(**override))
        policy.validate_new({'engine': 'fixture', 'options': {}})

    def test_options_reject_billing_tools_sandbox_and_raw_argv(self):
        for key, value in [('billing_mode', 'api'), ('billing_mode', 'subscription'), ('sandbox', 'read-only'),
                ('allowed_tools', []), ('argv', []), ('provider', 'google'), ('base_url', 'test'),
                ('unset_env', ['GEMINI_API_KEY']), ('allow_network', True)]:
            with self.subTest(key=key), self.assertRaises(UsageError):
                engines.validate_options('agy', {**self.payload()['options'], key: value})
        self.assertEqual(self.submit(extra=('--billing-mode', 'api'), expect=2)['error'], 'usage')
        self.assertEqual(self.submit(extra=('--test-only-reason', 'test'), expect=2)['error'], 'usage')
        self.assertEqual(self.db.open_attempts(), [])

    def test_cli_rejects_missing_fields_and_help_exposes_carbon(self):
        valid = ['submit', '--key', 'bad', '--engine', 'agy', '--task-class', 'carbon',
                 '--resource-class', 'standard', '--cwd', str(self.work), '--prompt', 'p',
                 '--option', 'model="gemini-4-carbon-high"', '--option', 'effort="high"']
        for flag, value in [('--task-class', 'carbon'), ('--resource-class', 'standard'),
                            ('--option', 'model="gemini-4-carbon-high"'), ('--option', 'effort="high"')]:
            args = list(valid)
            index = args.index(value)
            del args[index - 1:index + 1]
            self.command(*args, expect=2)
        text = io.StringIO()
        with contextlib.redirect_stdout(text), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(['submit', '--help'])
        self.assertIn('agy', text.getvalue())
        self.assertIn('carbon', text.getvalue())
        self.assertFalse(self.calls())

    def test_cross_class_requests_preserve_model_in_refusal_and_never_spawn(self):
        for task_class, model, effort in [('carbon', NEON, 'xhigh'),
                                          ('highest_quality', MODELS[2], 'high')]:
            with self.subTest(task_class=task_class):
                refused = self.submit(task_class=task_class, options={'model': model, 'effort': effort}, expect=2)
                self.assertEqual(refused['detail']['requested'], {'engine': 'agy', 'model': model, 'effort': effort})
                self.assertEqual(refused['detail']['task_class'], task_class)
        self.assertFalse(self.db.open_attempts())
        self.assertFalse(self.calls())

    def test_highest_quality_requires_explicit_fields_and_help_gives_neon_command(self):
        valid = ['submit', '--key', 'neon', '--engine', 'agy', '--task-class', 'highest_quality',
                 '--resource-class', 'standard', '--cwd', str(self.work), '--prompt', 'p',
                 '--option', 'model="gemini-4-neon-xhigh"', '--option', 'effort="xhigh"']
        for flag, value in [('--engine', 'agy'), ('--option', 'model="gemini-4-neon-xhigh"'),
                            ('--option', 'effort="xhigh"')]:
            args = list(valid)
            index = args.index(value)
            del args[index - 1:index + 1]
            self.command(*args, expect=2)
        text = io.StringIO()
        with contextlib.redirect_stdout(text), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(['submit', '--help'])
        self.assertIn('gemini-4-neon-xhigh', text.getvalue())
        self.assertIn('effort=xhigh', text.getvalue())
        self.assertFalse(self.calls())

    def test_cli_rejects_old_engines_neon_efforts_and_agy_simple_complex(self):
        for engine, model, effort, task_class in [
                ('claude', 'claude-opus-5-5', 'max', 'highest_quality'),
                ('claude', 'claude-fable-5-2', 'xhigh', 'highest_quality'),
                ('claude', 'claude-fable-5-2', 'max', 'highest_quality'),
                ('codex', 'gpt-6.1-sol', 'max', 'highest_quality'),
                *[('agy', NEON, effort, 'highest_quality') for effort in ('low', 'medium', 'high', 'max')],
                *[('agy', NEON, 'xhigh', task_class) for task_class in ('simple', 'complex')]]:
            with self.subTest(engine=engine, model=model, effort=effort, task_class=task_class):
                args = ['submit', '--key', 'invalid', '--engine', engine, '--task-class', task_class,
                        '--resource-class', 'standard', '--cwd', str(self.work), '--prompt', 'p',
                        '--option', 'model=' + json.dumps(model), '--option', 'effort=' + json.dumps(effort)]
                if engine == 'codex':
                    args += ['--option', 'sandbox="read-only"']
                self.assertEqual(self.command(*args, expect=2)['error'], 'usage')
        self.assertFalse(self.db.open_attempts())
        self.assertFalse(self.calls())


class AgyPreflight(AgyCase):
    def test_discovery_order_absolute_paths_and_bad_override_no_fallback(self):
        env = isolated_env(self.root)
        local = Path(env['HOME']) / '.local/bin/agy'
        local.parent.mkdir(parents=True)
        local.write_text('#!/bin/sh\nexit 97\n')
        local.chmod(0o755)
        path_dir = self.root / 'path-bin'
        path_dir.mkdir()
        other = path_dir / 'agy'
        other.write_text('#!/bin/sh\nexit 97\n')
        other.chmod(0o755)
        env['PATH'] = str(path_dir)
        self.assertEqual(engines.resolve_bin('agy', dict(env, CLI_RELAY_AGY_BIN=str(self.binary))), str(self.binary))
        self.assertEqual(engines.resolve_bin('agy', env), str(local))
        local.chmod(0o644)
        self.assertEqual(engines.resolve_bin('agy', env), str(other))
        for value in ('', './agy', 'agy --print', str(self.root / 'missing'), str(self.root), str(local)):
            with self.subTest(value=value):
                self.assertIsNone(engines.resolve_bin('agy', dict(env, CLI_RELAY_AGY_BIN=value)))

    def test_missing_and_unverified_never_probe_or_read_other_engine_config(self):
        with contextlib.ExitStack() as stack:
            for name in ('codex_config_problems', 'claude_settings_problems', 'codex_home',
                         'claude_config_dir', 'codex_rollout', 'session_record'):
                stack.enter_context(patch.object(engines, name, side_effect=AssertionError(name)))
            probe = stack.enter_context(patch.object(engines.subprocess, 'Popen', side_effect=AssertionError('probe')))
            pf = self.preflight(CLI_RELAY_AGY_BIN=str(self.root / 'absent'))
            self.assertEqual((pf.ok, pf.reason), (False, 'cli_not_found'))
            self.assertIn('不安装', pf.detail['message'])
            with patch.object(engines, 'AGY_CONTRACT', None):
                self.assertEqual(self.preflight().reason, 'agy_capability_unverified')
                with self.assertRaises(RuntimeError):
                    self.build()
            probe.assert_not_called()

    def test_route_conflicts_check_names_even_empty_and_never_read_values(self):
        class NamesOnly(dict):
            def __getitem__(self, name):
                if name in engines.BILLING_ENV['agy']:
                    raise AssertionError('must not read billing values')
                return super().__getitem__(name)
        for name in engines.BILLING_ENV['agy']:
            env = NamesOnly({**self.env, name: ''})
            with patch.object(engines, '_agy_probe') as probe:
                pf = engines.preflight('agy', env, str(self.work))
            self.assertEqual((pf.reason, pf.detail['names']), ('agy_non_subscription_route', [name]))
            probe.assert_not_called()
        pf = self.preflight(GOOGLE_APPLICATION_CREDENTIALS='/must/not/open/secret')
        self.assertNotIn('/must/not/open', json.dumps(pf.detail))

    @patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT)
    def test_verified_fake_probes_models_and_doctor(self):
        with patch.object(engines, 'codex_config_problems', side_effect=AssertionError('codex')), \
                patch.object(engines, 'claude_settings_problems', side_effect=AssertionError('claude')):
            pf = self.preflight()
            doctor = self.command('doctor', '--engine', 'agy', '--cwd', str(self.work))
        self.assertTrue(pf.ok, pf)
        self.assertEqual(pf.detail['models'], [NEON, *MODELS])
        self.assertEqual(doctor['engine']['agy_version'], '1.5.11')
        self.assertTrue(all(c['argv'] in (['--version'], ['models']) and not c['tty'] for c in self.calls()))
        self.assertFalse(self.print_calls())

    @patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT)
    def test_unknown_version_model_missing_and_unverified_subscription(self):
        self.assertEqual(self.preflight(AGY_FAKE_VERSION='TEST-AGY-VERSION 9.0').reason, 'agy_capability_unverified')
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.preflight(AGY_FAKE_MODELS=json.dumps({'test_models': [MODELS[0]]})).reason,
                         'agy_model_unavailable')
        self.assertEqual(self.preflight(AGY_FAKE_MODELS='unknown output').reason, 'agy_preflight_unverified')
        with patch.object(engines, 'AGY_CONTRACT', replace(FAKE_CONTRACT, subscription_verified=False)):
            self.assertEqual(self.preflight().reason, 'agy_subscription_unverified')
        self.assertFalse(self.print_calls())

    @patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT)
    def test_auth_subscription_quota_and_unknown_failures_are_sanitized(self):
        for name in ('auth_required', 'subscription_unavailable', 'quota_exhausted', 'non_subscription_route'):
            pf = self.preflight(AGY_FAKE_PROBE_EXIT='1',
                AGY_FAKE_PROBE_STDERR=f'TEST-AGY-ERROR {name}\nSYNTHETIC-IDENTITY https://example.invalid/login')
            self.assertEqual(pf.reason, 'agy_' + name)
            self.assertNotIn('SYNTHETIC', json.dumps(pf.detail))
            self.assertNotIn('https:', json.dumps(pf.detail))
        pf = self.preflight(AGY_FAKE_PROBE_EXIT='1', AGY_FAKE_PROBE_STDERR='unknown SYNTHETIC-IDENTITY')
        self.assertEqual(pf.reason, 'agy_preflight_unverified')

    @patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT)
    def test_probes_are_bounded_and_errors_do_not_leak(self):
        with patch.object(engines, 'AGY_PROBE_TIMEOUT_S', 0.1):
            self.assertEqual(self.preflight(AGY_FAKE_PROBE_DELAY='2').reason, 'agy_probe_timeout')
        self.assertEqual(self.preflight(AGY_FAKE_NOISE='1').reason, 'agy_preflight_unverified')
        with patch.object(engines.subprocess, 'Popen', side_effect=OSError('SECRET raw failure')):
            pf = self.preflight()
        self.assertEqual(pf.reason, 'agy_preflight_unverified')
        self.assertNotIn('SECRET', json.dumps(pf.detail))


class AgyPrint(AgyCase):
    def parsed(self, text='answer', *, code=0, sig=None, known=True, stderr=''):
        stdout = self.root / 'stdout.jsonl'
        stdout.write_text(text)
        err = self.root / 'stderr.log'
        err.write_text(stderr)
        return engines.parse_output('agy', stdout, stderr_path=err, exit_known=known,
                                    exit_code=code, exit_signal=sig)

    def decide(self, parsed, *, code=0, sig=None, known=True, cancel=False, marker=False):
        return engines.decide('agy', parsed, env=self.env, session_id=None, exit_code=code, exit_signal=sig,
                              exit_known=known, cancel_requested=cancel, require_completion_marker=marker)

    @patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT)
    def test_exact_argv_rechecks_binary_and_rejects_unknown_engine(self):
        spec = self.build()
        self.assertEqual(spec.argv, [str(self.binary), '--print', '--model', MODELS[2]])
        self.assertEqual(spec.cwd, str(self.work))
        self.assertEqual(spec.env['HOME'], self.env['HOME'])
        self.assertIsNone(spec.last_message_path)
        self.assertEqual(spec.evidence['agy']['input_mode'], 'stdin')
        reporting = self.payload(options={**self.payload()['options'], 'result_file': 'out/result.json',
                                          'required_artifacts': ['report.md']})
        after = engines.build('agy', payload=reporting, attempt={}, run_dir=self.root,
                              fixture_state_dir=str(self.root), env=self.env)
        self.assertEqual((after.argv, after.evidence), (spec.argv, spec.evidence))
        for extra in ('--effort', '--resume', '--login', '--no-login', '--yolo'):
            with self.assertRaises(RuntimeError):
                engines.assert_safe('agy', spec.argv + [extra], options=self.payload()['options'])
        with self.assertRaises(RuntimeError):
            self.build(resume_session_id='session')
        self.binary.unlink()
        with self.assertRaises(RuntimeError):
            self.build()
        with self.assertRaises(UsageError):
            engines.build('unknown', payload=self.payload(), attempt={}, run_dir=self.root,
                          fixture_state_dir=str(self.root), env=self.env)

    @patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT)
    def test_stdout_is_utf8_text_not_session_billing_or_init_events(self):
        text = ('{"type":"system","subtype":"init","session_id":"forged","apiKeySource":"api"}\n'
                '{"type":"turn.completed","thread_id":"forged"}\n中文\nCLI-RELAY-STATUS: complete\n')
        parsed = self.parsed(text)
        self.assertEqual(parsed['native']['source'], 'agy_print')
        self.assertEqual(parsed['native']['result'], text)
        self.assertIsNone(parsed['init'])
        self.assertIsNone(parsed['session_id'])
        self.assertEqual(self.decide(parsed, marker=True)[0], 'succeeded')
        with patch.object(engines, 'codex_home', side_effect=AssertionError('trace')):
            self.assertIsNone(engines.native_trace_exists('agy', 'forged'))
            self.assertIsNone(engines.session_record('agy', self.env, 'forged', fixture_state_dir=str(self.root)))

    @patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT)
    def test_empty_nonzero_signal_unknown_exit_and_marker_cannot_promote(self):
        for text, code, sig, want in [('', 0, None, 'agy_empty_output'),
                (' \n', 0, None, 'agy_empty_output'), ('CLI-RELAY-STATUS: complete', 1, None, 'agy_cli_failed'),
                ('CLI-RELAY-STATUS: complete', None, 9, 'agy_cli_failed')]:
            parsed = self.parsed(text, code=code, sig=sig)
            outcome, evidence = self.decide(parsed, code=code, sig=sig, marker=True)
            self.assertEqual((outcome, evidence['reason_code']), ('failed', want))
        parsed = self.parsed('CLI-RELAY-STATUS: complete', known=False)
        self.assertIsNone(parsed['native'])
        self.assertEqual(self.decide(parsed, known=False)[0], 'unknown')
        self.assertEqual(self.decide(parsed, known=False, cancel=True)[0], 'cancelled')
        for text, want in [('answer', 'needs_input'), ('CLI-RELAY-STATUS: incomplete', 'needs_input'),
                            ('CLI-RELAY-STATUS: complete', 'succeeded')]:
            self.assertEqual(self.decide(self.parsed(text), marker=True)[0], want)

    @patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT)
    def test_stderr_only_errors_never_model_prose_and_no_raw_export(self):
        for name in ('auth_required', 'subscription_unavailable', 'quota_exhausted', 'non_subscription_route'):
            parsed = self.parsed('CLI-RELAY-STATUS: complete', code=1,
                stderr=f'TEST-AGY-ERROR {name}\nSECRET URL https://example.invalid/login')
            outcome, evidence = self.decide(parsed, code=1, marker=True)
            self.assertEqual((outcome, evidence['reason_code']), ('blocked', 'agy_' + name))
            self.assertNotIn('SECRET', json.dumps(parsed))
        self.assertEqual(self.decide(self.parsed('TEST-AGY-ERROR auth_required'))[0], 'succeeded')
        self.assertEqual(self.decide(self.parsed('answer', code=1, stderr='unknown'), code=1)[0], 'failed')
        self.assertEqual(self.decide(self.parsed('', code=1, stderr='TEST-AGY-ERROR needs_input'), code=1)[0],
                         'needs_input')

    def test_unverified_output_contract_and_invalid_utf8_fail(self):
        with patch.object(engines, 'AGY_CONTRACT', None):
            self.assertEqual(self.parsed()['native']['reason'], 'agy_output_unverified')
        stdout = self.root / 'invalid'
        stdout.write_bytes(b'\xff')
        parsed = engines.parse_output('agy', stdout, exit_known=True, exit_code=0)
        self.assertEqual(parsed['native']['reason'], 'agy_output_unverified')


class AgyIntegration(AgyCase):
    def test_neon_exact_settings_argv_receipt_event_and_duplicate(self):
        sub = self.submit('neon', task_class='highest_quality', options={'require_completion_marker': True})
        done = self.settle(sub['task_id'], 'succeeded')
        settings = sub['effective_settings']
        self.assertEqual((settings['engine'], settings['model'], settings['effort'], settings['task_class']),
                         ('agy', NEON, 'xhigh', 'highest_quality'))
        self.assertEqual(done['task']['effective_settings'], settings)
        self.assertEqual(done['events'][0]['body']['effective_settings'], settings)
        self.assertEqual(self.print_calls()[0]['argv'], ['--print', '--model', NEON])
        calls = self.calls()
        self.assertEqual(self.submit('neon', task_class='highest_quality',
                                    options={'require_completion_marker': True})['status'], 'duplicate')
        self.assertEqual(self.calls(), calls)

    def test_neon_missing_from_carbon_only_catalog_blocks_without_replacement(self):
        self.env['AGY_FAKE_MODELS'] = json.dumps({'test_models': MODELS})
        sub = self.submit('neon-missing', task_class='highest_quality')
        done = self.settle(sub['task_id'], 'blocked')
        self.assertEqual(done['attempts'][0]['evidence']['preflight']['reason'], 'agy_model_unavailable')
        self.assertEqual(done['task']['options']['model'], NEON)
        self.assertEqual(done['task']['effective_settings']['effort'], 'xhigh')
        self.assertEqual(done['events'][0]['body']['effective_settings']['model'], NEON)
        self.assertFalse(self.print_calls())

    def test_neon_auth_quota_subscription_and_runtime_failure_never_fall_back(self):
        for reason in ('auth_required', 'quota_exhausted', 'subscription_unavailable'):
            with self.subTest(reason=reason):
                self.env.update(AGY_FAKE_STDERR='TEST-AGY-ERROR ' + reason, AGY_FAKE_EXIT='1')
                sub = self.submit('neon-' + reason, task_class='highest_quality')
                done = self.settle(sub['task_id'], 'blocked')
                self.assertEqual(done['events'][0]['body']['reason_code'], 'agy_' + reason)
                self.assertEqual(done['task']['effective_settings']['model'], NEON)
        self.env.update(AGY_FAKE_STDERR='unknown failure', AGY_FAKE_EXIT='7')
        sub = self.submit('neon-failed', task_class='highest_quality')
        self.assertEqual(self.settle(sub['task_id'], 'failed')['task']['options']['model'], NEON)
        self.assertEqual([call['argv'] for call in self.print_calls()], [['--print', '--model', NEON]] * 4)

    def test_no_contract_doctor_never_executes_even_fake_installed_binary(self):
        self.assertIsNone(engines.AGY_CONTRACT)
        proc = subprocess.run([sys.executable, '-m', 'cli_relay', '--home', str(self.home),
                               'doctor', '--engine', 'agy', '--cwd', str(self.work)],
                              env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 6)
        self.assertEqual(json.loads(proc.stdout)['engine']['reason'], 'agy_capability_unverified')
        self.assertFalse(self.calls())

    def test_shared_capacity_with_fixture_and_queued_carbon_releases_same_attempt(self):
        self.env['CLI_RELAY_LOCAL_HARD_MAX'] = '1'
        _, fixture, _ = self.db.submit(scope='other', key='capacity-holder', native_session_id=None,
            payload={'engine': 'fixture', 'cwd': str(self.work), 'options': {}, 'prompt': 'p'}, prompt='p')
        att = self.db.current_attempt(fixture['task_id'])
        with patch.dict(os.environ, self.env, clear=True):
            self.assertTrue(self.db.claim(att['attempt_id'], keeper_pid=os.getpid(),
                keeper_token=procs.start_token(os.getpid()), keeper_pgid=os.getpgid(0), host='test'))
        sub = self.submit()
        self.settle(sub['task_id'], 'waiting')
        self.assertFalse(self.print_calls())
        self.db.finish(att['attempt_id'], from_states=('claimed',), outcome='succeeded')
        done = self.settle(sub['task_id'], 'succeeded')
        self.assertEqual(len(done['attempts']), 1)
        self.assertEqual(len(self.print_calls()), 1)
        self.assertEqual([e['kind'] for e in done['events']], ['waiting', 'terminal'])

    def test_queue_waiting_cancel_does_not_start_second_carbon(self):
        self.env['AGY_FAKE_DURATION'] = '1'
        first = self.submit('first', extra=('--queue', 'carbon-text'))
        wait_until(lambda: self.print_calls())
        second = self.submit('second', extra=('--queue', 'carbon-text'))
        self.settle(second['task_id'], 'waiting')
        self.command('cancel', second['task_id'])
        self.settle(second['task_id'], 'cancelled')
        self.settle(first['task_id'], 'succeeded')
        self.assertEqual(len(self.print_calls()), 1)

    def test_stdin_prompt_consumed_once_unicode_quotes_shell_text_and_three_models(self):
        prompt = '-中文\n spaces \' " $(touch MUST_NOT_EXIST) `touch MUST_NOT_EXIST`\n' + '长' * 50000
        for effort in ('low', 'medium', 'high'):
            sub = self.submit(effort, effort=effort, prompt=prompt, options={'require_completion_marker': True})
            done = self.settle(sub['task_id'])
            self.assertEqual(done['task']['state'], 'succeeded', done)
            self.assertIsNone(done['attempts'][0]['native_session_id'])
            settings = done['task']['effective_settings']
            self.assertEqual(settings['effort'], effort)
        calls = self.print_calls()
        self.assertEqual(len(calls), 3)
        for record in calls:
            self.assertEqual(record['prompt'], prompt + '\n\n' + business_status.COMPLETION_PROMPT)
            self.assertEqual(record['cwd'], str(self.work))
            self.assertFalse(record['tty'])
            self.assertEqual(record['argv'][1], '--model')
        self.assertFalse((self.work / 'MUST_NOT_EXIST').exists())

    def test_duplicate_key_and_model_conflict_never_repeat_probes_or_print(self):
        sub = self.submit()
        self.settle(sub['task_id'])
        first = self.calls()
        again = self.submit()
        self.assertEqual((again['status'], again['task_id']), ('duplicate', sub['task_id']))
        self.submit(effort='low', expect=3)
        self.assertEqual(self.calls(), first)
        self.assertEqual(len(self.print_calls()), 1)
        self.assertEqual(len(self.db.events_of_task(sub['task_id'])), 1)

    def test_no_agy_accepted_then_blocked_status_wait_event_and_doctor(self):
        self.env['CLI_RELAY_AGY_BIN'] = str(self.root / 'absent')
        sub = self.submit()
        self.assertTrue(sub['accepted'])
        done = self.settle(sub['task_id'])
        self.assertEqual(done['task']['state'], 'blocked')
        self.assertEqual(done['attempts'][0]['evidence']['preflight']['reason'], 'cli_not_found')
        wait = self.command('wait', sub['task_id'], '--timeout', '1')
        self.assertEqual(wait['task']['state'], 'blocked')
        events = self.command('events', '--consumer', 'test')['events']
        self.assertEqual(events[0]['body']['reason_code'], 'cli_not_found')
        self.assertEqual(self.command('doctor', '--engine', 'agy', expect=6)['engine']['reason'], 'cli_not_found')
        self.assertFalse(self.calls())

    def test_session_refused_resume_no_native_and_explicit_fresh(self):
        out = self.submit(extra=('--session', 'anything'), expect=6)
        self.assertEqual(out['error'], 'agy_session_unsupported')
        self.assertFalse(self.db.open_attempts())
        sub = self.submit()
        self.settle(sub['task_id'])
        for child in self.children:
            child.wait(timeout=10)
        self.assertEqual(self.command('resume', sub['task_id'], '--prompt', 'continue', expect=6)['error'],
                         'no_native_session')
        self.assertEqual(self.command('resume', sub['task_id'], '--fresh', expect=6)['error'], 'rerun_not_confirmed')
        with patch.object(cli, 'old_run_stopped', return_value={'stopped': False}), patch.object(cli.time, 'sleep'), \
                patch.object(cli.time, 'monotonic', side_effect=[0, 20]):
            self.assertEqual(self.command('resume', sub['task_id'], '--fresh', '--confirm-rerun', expect=6)['error'],
                             'old_run_may_be_alive')
        self.command('resume', sub['task_id'], '--fresh', '--confirm-rerun')
        self.settle(sub['task_id'], 'succeeded')
        self.assertEqual(len(self.print_calls()), 2)

    def test_cancel_cleans_process_group_and_does_not_retry(self):
        descendant = self.root / 'descendant.pid'
        self.env.update(AGY_FAKE_DURATION='90', AGY_FAKE_DESCENDANT=str(descendant), CLI_RELAY_CANCEL_GRACE_S='0.1')
        sub = self.submit()
        wait_until(descendant.exists)
        pid = int(descendant.read_text())
        self.command('cancel', sub['task_id'])
        done = self.settle(sub['task_id'])
        self.assertEqual(done['task']['state'], 'cancelled', done)
        self.assertIs(procs.same_process_alive(pid, None), False)
        self.assertEqual(len(self.print_calls()), 1)
        self.assertEqual(len(self.db.events_of_task(sub['task_id'])), 1)

    def test_marker_business_result_file_and_missing_artifacts(self):
        self.env['AGY_FAKE_RESULT'] = 'answer'
        sub = self.submit('no-marker', options={'require_completion_marker': True})
        self.assertEqual(self.settle(sub['task_id'])['task']['state'], 'needs_input')
        (self.work / 'out').mkdir()
        (self.work / 'out/result.json').write_text('{"status":"ok","required_artifacts":["missing.txt"]}')
        self.env['AGY_FAKE_RESULT'] = '做完没：做完\n还差：无\n下一步谁动：没有\nstatus=ok\nCLI-RELAY-STATUS: complete'
        sub = self.submit('missing-artifact', options={'result_file': 'out/result.json',
                          'required_artifacts': ['missing.txt'], 'require_completion_marker': True})
        done = self.settle(sub['task_id'])
        self.assertEqual(done['task']['state'], 'succeeded')
        self.assertNotEqual(done['task']['business_status'], 'ok')

    def test_runtime_auth_error_blocks_and_nonzero_complete_fails(self):
        for name, stderr, code, want in [
                ('auth', 'TEST-AGY-ERROR auth_required\nDO_NOT_EXPORT', '1', 'blocked'),
                ('nonzero', 'unknown', '7', 'failed'), ('empty', '', '0', 'failed')]:
            self.env.update(AGY_FAKE_STDERR=stderr, AGY_FAKE_EXIT=code,
                            AGY_FAKE_RESULT='' if name == 'empty' else 'CLI-RELAY-STATUS: complete')
            sub = self.submit(name, options={'require_completion_marker': True})
            done = self.settle(sub['task_id'])
            self.assertEqual(done['task']['state'], want, done)
            self.assertNotIn('DO_NOT_EXPORT', json.dumps(done))

    def test_crash_windows_no_exit_text_unknown_and_exit_record_recovers_once(self):
        for point, want, starts in [('before_spawn', 'unknown', 0), ('after_spawn_before_record', 'unknown', 1),
                                    ('after_exit_before_commit', 'succeeded', 1)]:
            with self.subTest(point=point):
                before = len(self.print_calls())
                self.env['CLI_RELAY_TEST_CRASH'] = point
                sub = self.submit(point)
                child = self.children[-1]
                self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)
                done = self.settle(sub['task_id'], want)
                self.assertEqual(len(self.print_calls()) - before, starts)
                if want == 'unknown':
                    self.assertIsNone(done['attempts'][0]['native'])
                for _ in range(2):
                    self.command('reconcile', sub['task_id'])
                self.assertEqual(len(self.db.events_of_task(sub['task_id'])), 1)
                self.assertEqual(len(self.print_calls()) - before, starts)

    def test_keeper_lost_mid_output_observes_then_unknown_without_rerun(self):
        self.env['AGY_FAKE_DURATION'] = '1'
        sub = self.submit()
        wait_until(lambda: self.print_calls())
        self.children[-1].kill()
        self.children[-1].wait(timeout=10)
        live = self.command('status', sub['task_id'])
        self.assertEqual(live['reconcile'][0]['action'], 'observing_cli_without_keeper')
        done = self.settle(sub['task_id'], 'unknown')
        self.assertIsNone(done['attempts'][0]['native'])
        self.assertEqual(len(self.print_calls()), 1)

    def test_after_claim_crash_releases_same_attempt_then_starts_once(self):
        self.env['CLI_RELAY_TEST_CRASH'] = 'after_claim'
        sub = self.submit()
        self.children[-1].wait(timeout=10)
        self.env.pop('CLI_RELAY_TEST_CRASH')
        self.command('reconcile', sub['task_id'])
        done = self.settle(sub['task_id'], 'succeeded')
        self.assertEqual(len(done['attempts']), 1)
        self.assertEqual(len(self.print_calls()), 1)

    def test_malformed_exit_record_is_not_completion_evidence(self):
        self.env['CLI_RELAY_TEST_CRASH'] = 'before_spawn'
        sub = self.submit()
        self.children[-1].wait(timeout=10)
        run_dir = self.db.run_dir(sub['task_id'], 1)
        (run_dir / 'stdout.jsonl').write_text('CLI-RELAY-STATUS: complete')
        for data in ({}, {'exit_code': False, 'signal': None}, {'exit_code': 0, 'signal': 9}):
            (run_dir / 'exit.json').write_text(json.dumps(data))
            self.assertEqual(self.command('status', sub['task_id'])['task']['state'], 'unknown')

    def test_acceptance_script_no_models_by_default_and_refuses_existing_directory(self):
        directory = self.root / 'acceptance'
        args = [sys.executable, str(REPO / 'scripts/agy_acceptance.py'), '--dir', str(directory)]
        first = subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(first.returncode, 3, first.stderr)
        report = json.loads((directory / 'report.json').read_text())
        self.assertEqual(report['preflight']['reason'], 'agy_capability_unverified')
        self.assertTrue(all(step['verdict'] == 'NOT RUN' for step in report['steps']))
        self.assertFalse(self.calls())
        second = subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(second.returncode, 2)

    def test_acceptance_neon_presence_only_and_no_inference_by_default(self):
        from scripts import agy_acceptance
        directory = self.root / 'neon-acceptance'
        self.env['AGY_FAKE_MODELS'] = json.dumps({'test_models': [NEON, 'SYNTHETIC-IDENTITY']})
        with patch.dict(os.environ, self.env, clear=True), patch.object(engines, 'AGY_CONTRACT', FAKE_CONTRACT), \
                patch.object(agy_acceptance, 'relay', side_effect=AssertionError('must not submit')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agy_acceptance.main(['--dir', str(directory),
                                                  '--task-class', 'highest_quality']), 3)
        text = (directory / 'report.json').read_text()
        report = json.loads(text)
        self.assertEqual(report['model_presence'], {NEON: True, **dict.fromkeys(MODELS, False)})
        self.assertEqual(report['steps'], [{'model': NEON, 'effort': 'xhigh', 'verdict': 'NOT RUN',
                                           'reason': 'explicit --run-models required'}])
        self.assertNotIn('SYNTHETIC-IDENTITY', text)
        self.assertNotIn('models', report['preflight'])
        self.assertFalse(self.print_calls())



if __name__ == '__main__':
    unittest.main()

"""Config precedence, real platform probe boundaries and existing CLI diagnostics."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers
from cli_relay import cli, concurrency, notify, routes
from cli_relay.errors import UsageError
from cli_relay.ledger import Ledger, atomic_write_text, init_ledger


class MemoryProbe(unittest.TestCase):
    def vm_stat(self, page_size):
        return (f'Mach Virtual Memory Statistics: (page size of {page_size} bytes)\n'
                'Pages free:                               100.\n'
                'Pages active:                           90000.\n'
                'Pages inactive:                           200.\n'
                'Pages purgeable:                           50.\n'
                'Pages occupied by compressor:           80000.\n')

    def test_macos_uses_reported_page_size_and_only_free_inactive_purgeable(self):
        for size in (4096, 16384):
            with self.subTest(page_size=size), patch.object(concurrency.sys, 'platform', 'darwin'), \
                    patch.object(concurrency.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        ['/usr/bin/vm_stat'], 0, self.vm_stat(size))) as run:
                observation = concurrency.read_available_memory()
                self.assertEqual(observation.available_bytes, 350 * size)
                self.assertIsNone(observation.error)
                self.assertEqual(run.call_args.args[0], ['/usr/bin/vm_stat'])
                self.assertEqual(run.call_args.kwargs['timeout'], 1)
                self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_linux_uses_memavailable_in_kib_instead_of_memfree(self):
        text = 'MemTotal: 16000000 kB\nMemFree: 200 kB\nMemAvailable: 1700000 kB\nCached: 999 kB\n'
        with patch.object(concurrency.sys, 'platform', 'linux'), \
                patch.object(Path, 'read_text', return_value=text) as read, \
                patch.object(concurrency.subprocess, 'run') as run:
            memory = concurrency.read_available_memory()
            self.assertEqual(memory.available_bytes, 1700000 * 1024)
            self.assertEqual(memory.source, '/proc/meminfo:MemAvailable')
            read.assert_called_once_with(encoding='ascii')
            run.assert_not_called()

    def test_macos_missing_command_timeout_and_bad_output_degrade_to_unknown(self):
        for error in (FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired('vm_stat', 1),
                      subprocess.CalledProcessError(1, 'vm_stat')):
            with self.subTest(error=type(error).__name__), patch.object(concurrency.sys, 'platform', 'darwin'), \
                    patch.object(concurrency.subprocess, 'run', side_effect=error):
                memory = concurrency.read_available_memory()
                self.assertIsNone(memory.available_bytes)
                self.assertEqual(memory.error, type(error).__name__)
        for text in ('', self.vm_stat(0), self.vm_stat(4096).replace('Pages inactive:', 'unknown:'),
                     self.vm_stat(4096).replace('100.', '-100.')):
            with self.subTest(output=text), patch.object(concurrency.sys, 'platform', 'darwin'), \
                    patch.object(concurrency.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text)):
                self.assertIsNone(concurrency.read_available_memory().available_bytes)

    def test_linux_unreadable_missing_or_invalid_memavailable_is_not_zero(self):
        with patch.object(concurrency.sys, 'platform', 'linux'):
            for text in ('', 'MemFree: 1000000 kB\n', 'MemAvailable: -1 kB\n', 'MemAvailable: 1 MB\n'):
                with self.subTest(output=text), patch.object(Path, 'read_text', return_value=text):
                    self.assertIsNone(concurrency.read_available_memory().available_bytes)
            with patch.object(Path, 'read_text', side_effect=OSError):
                self.assertEqual(concurrency.read_available_memory().error, 'OSError')
            with patch.object(Path, 'read_text', return_value='MemAvailable: 0 kB\n'):
                self.assertEqual(concurrency.read_available_memory().available_bytes, 0)

    def test_unsupported_platform_is_an_explicit_probe_failure(self):
        with patch.object(concurrency.sys, 'platform', 'other'):
            self.assertEqual(concurrency.read_available_memory(),
                             concurrency.Memory(None, 'unsupported', 'unsupported_platform'))


class LocalConcurrencyConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.tmp) / 'ledger'
        init_ledger(self.home)
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.memory = self.enterContext(patch.object(concurrency, 'read_available_memory',
            return_value=concurrency.Memory(8 * 1024**3, 'mock')))

    def save(self, settings, source_id='mini'):
        raw = {'source_id': source_id, 'routes': {}, 'local_concurrency': settings}
        (self.home / 'notifier.json').write_text(json.dumps(raw))
        return raw

    def test_missing_file_and_existing_config_without_section_use_defaults(self):
        for configured in (False, True):
            if configured:
                (self.home / 'notifier.json').write_text('{"source_id":"mini","routes":{}}')
            snapshot = concurrency.snapshot(self.home)
            self.assertEqual(snapshot['hard_max'], 4)
            self.assertEqual(snapshot['fallback_max'], 2)
            self.assertEqual(snapshot['min_available_bytes'], 1536 * concurrency.MIB)
            self.assertEqual(snapshot['max_active'], 4)
            self.assertEqual(set(snapshot['setting_sources'].values()), {'default'})
            self.memory.return_value = concurrency.Memory(None, 'mock', 'OSError')
            self.assertEqual(concurrency.snapshot(self.home)['max_active'], 2)
            self.memory.return_value = concurrency.Memory(8 * 1024**3, 'mock')

    def test_environment_over_machine_over_home_over_default_and_no_rewrite(self):
        self.save({'hard_max': 6, 'min_available_mib': 2048, 'sources': {
            'mini': {'hard_max': 3, 'fallback_max': 1}, 'mbp': {'hard_max': 8}}})
        before = (self.home / 'notifier.json').read_bytes()
        cfg = concurrency.load_config(self.home, {'CLI_RELAY_LOCAL_HARD_MAX': '5'})
        self.assertEqual((cfg['hard_max'], cfg['fallback_max'], cfg['min_available_mib']), (5, 1, 2048))
        self.assertEqual(cfg['source_id'], 'mini')
        self.assertEqual(cfg['setting_sources'], {'hard_max': 'CLI_RELAY_LOCAL_HARD_MAX',
            'fallback_max': 'source_id', 'min_available_mib': 'notifier.json'})
        self.assertEqual(concurrency.load_config(self.home, {})['hard_max'], 3)
        self.assertEqual((self.home / 'notifier.json').read_bytes(), before)

    def test_source_id_selects_only_that_machine_and_inherits_other_settings(self):
        settings = {'hard_max': 6, 'sources': {'mini': {'hard_max': 3}, 'mbp': {'hard_max': 8}}}
        for source_id, limit in (('mini', 3), ('mbp', 8), ('other', 6)):
            with self.subTest(source=source_id):
                self.save(settings, source_id)
                cfg = concurrency.load_config(self.home, {})
                self.assertEqual(cfg['hard_max'], limit)
                self.assertEqual(cfg['fallback_max'], 2)

    def test_every_environment_setting_is_supported(self):
        cfg = concurrency.load_config(self.home, {'CLI_RELAY_LOCAL_HARD_MAX': '7',
            'CLI_RELAY_LOCAL_FALLBACK_MAX': '3', 'CLI_RELAY_LOCAL_MIN_AVAILABLE_MIB': '2048'})
        self.assertEqual((cfg['hard_max'], cfg['fallback_max'], cfg['min_available_mib']), (7, 3, 2048))

    def test_configured_hard_cap_and_fallback_always_allow_at_least_one(self):
        self.memory.return_value = concurrency.Memory(None, 'mock', 'OSError')
        for hard_max, fallback_max, expected in ((1, 8, 1), (3, 2, 2), (4, 6, 4)):
            with self.subTest(hard_max=hard_max, fallback_max=fallback_max):
                self.save({'hard_max': hard_max, 'fallback_max': fallback_max})
                self.assertEqual(concurrency.snapshot(self.home)['max_active'], expected)

    def test_invalid_values_report_names_without_values(self):
        for spec in (None, [], {'hard_max': 0}, {'fallback_max': True}, {'min_available_mib': -1},
                     {'hard_max': 1.5}, {'unknown': 3}, {'sources': []}, {'sources': {'bad id': {}}},
                     {'sources': {'mini': {'hard_max': 0}}}, {'sources': {'mini': {'sources': {}}}}):
            with self.subTest(spec=spec), self.assertRaises(UsageError):
                concurrency.validate_config(spec)
        for value in ('', '0', '-2', '1.5', 'secret-should-not-be-printed'):
            snapshot = concurrency.snapshot(self.home, {'CLI_RELAY_LOCAL_HARD_MAX': value})
            self.assertFalse(snapshot['ok'])
            self.assertEqual(snapshot['max_active'], 0)
            self.assertNotIn('secret-should-not-be-printed', json.dumps(snapshot))

    def test_bad_file_queues_instead_of_crashing_and_does_not_probe_memory(self):
        for text in ('{', '[]', '{"source_id": []}', '{"local_concurrency": null}'):
            with self.subTest(text=text):
                (self.home / 'notifier.json').write_text(text)
                snapshot = concurrency.snapshot(self.home)
                self.assertEqual(snapshot['reason'], 'invalid_concurrency_config')
        self.memory.assert_not_called()

    def test_route_save_keeps_partial_machine_overrides_and_old_shape(self):
        raw = self.save({'hard_max': 5, 'sources': {'mini': {'min_available_mib': 2000}}})
        routes._save(self.home, notify.validate_config(raw))
        self.assertEqual(json.loads((self.home / 'notifier.json').read_text())['local_concurrency'],
                         raw['local_concurrency'])
        old = notify.validate_config({'source_id': 'mini', 'routes': {}})
        self.assertNotIn('local_concurrency', old)

    def test_status_and_doctor_report_current_settings_memory_and_queue_reason(self):
        self.save({'hard_max': 4})
        self.memory.return_value = concurrency.Memory(200 * concurrency.MIB, 'mock')
        with contextlib.closing(Ledger(self.home)) as ledger:
            tasks = []
            for key in ('one', 'two'):
                payload = dict(engine='fixture', cwd=self.tmp, prompt='fixture', options={})
                _, task, _ = ledger.submit(scope=key, key=key, payload=payload,
                                           prompt='fixture', native_session_id=None)
                att = ledger.current_attempt(task['task_id'])
                ledger.claim(att['attempt_id'], keeper_pid=123, keeper_token='mock', keeper_pgid=123, host='mock')
                tasks.append(task['task_id'])
        for command in (['status', tasks[1], '--no-reconcile'], ['doctor']):
            output = io.StringIO()
            with contextlib.closing(Ledger(self.home)) as ledger, \
                    patch.object(cli, 'Ledger', return_value=ledger), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(['--home', str(self.home), *command]), 0)
            result = json.loads(output.getvalue())
            config = result['local_concurrency']
            self.assertEqual(config['source_id'], 'mini')
            self.assertEqual((config['hard_max'], config['max_active']), (4, 1))
            self.assertEqual(config['available_bytes'], 200 * concurrency.MIB)
            self.assertEqual(config['reason'], 'low_available_memory')
            if command[0] == 'status':
                capacity = result['task']['waiting_on']['capacity']
                self.assertEqual(capacity['reasons'], ['low_available_memory'])
                self.assertEqual(capacity['active_task_ids'], [tasks[0]])
                self.assertEqual(result['attempts'][0]['waiting_on']['capacity'], capacity)


class LocalConcurrencyKeepers(helpers.RelayTestCase):
    def test_file_change_releases_a_real_waiting_keeper_without_restarting_or_rerunning(self):
        config = {'source_id': 'test-mini', 'routes': {}, 'local_concurrency': {
            'hard_max': 3, 'fallback_max': 1, 'min_available_mib': 1 << 40}}
        path = self.home / 'notifier.json'
        atomic_write_text(path, json.dumps(config))
        one = self.submit('one', duration=60)
        helpers.wait_until(lambda: self.attempt_row(one['task_id'])['state'] == 'running')
        two = self.submit('two')
        helpers.wait_until(lambda: self.attempt_row(two['task_id'])['state'] == 'waiting')
        before = self.status(two['task_id'], reconcile=False)
        self.assertEqual(before['task']['waiting_on']['capacity']['max_active'], 1)
        keeper_pid = before['attempts'][0]['keeper']['pid']
        self.assertIsNone(self.attempt_row(two['task_id'])['spawn_started_at'])

        # Make room with a home setting, regardless of the test host's probe availability.
        config['local_concurrency'].update(min_available_mib=0, fallback_max=3)
        atomic_write_text(path, json.dumps(config))
        done = self.cli('wait', two['task_id'], '--timeout', '20', '--interval', '0.1')
        self.assertFalse(done['timed_out'])
        self.assertEqual(done['task']['state'], 'succeeded')
        self.assertEqual(len(done['attempts']), 1)
        self.assertEqual(done['attempts'][0]['keeper']['pid'], keeper_pid)
        self.assertEqual(self.attempt_row(one['task_id'])['state'], 'running')
        self.cli('cancel', one['task_id'])

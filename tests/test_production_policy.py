"""Routing and shared admission: no CLIs, network, auth or production ledgers."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import helpers
from cli_relay import cli, policy
from cli_relay.errors import UsageError
from cli_relay.ledger import Ledger, init_ledger, payload_hash


class ProductionTiers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'ledger'
        init_ledger(self.home)
        self.ledger = Ledger(self.home)
        self.addCleanup(self.ledger.close)

    def payload(self, task_class='complex', resource_class='standard'):
        engine, model, effort = policy.TIERS[task_class]
        return dict(engine=engine, cwd=self.tmp.name, prompt='fixture', task_class=task_class,
                    resource_class=resource_class, options=dict(model=model, effort=effort,
                    **({'sandbox': 'workspace-write'} if engine == 'codex' else {})))

    def test_exact_three_tiers_and_cu(self):
        for tier in policy.TIERS:
            p = self.payload(tier)
            policy.validate_new(p)
            settings = policy.settings(p['engine'], p)['effective_settings']
            self.assertEqual((settings['engine'], settings['model'], settings['effort']), policy.TIERS[tier])
            self.assertEqual(settings['verification'], 'invocation_only')
        policy.validate_new(self.payload(resource_class='computer_use'))
        for tier in ('simple', 'highest_quality'):
            with self.assertRaises(UsageError):
                policy.validate_new(self.payload(tier, 'computer_use'))

    def test_missing_unknown_deprecated_and_effort_mismatch(self):
        for field, value in (('model', None), ('model', ''), ('model', 'gpt-6-astra'),
                             ('model', 'unknown'), ('effort', None), ('effort', 'high')):
            p = self.payload()
            if value is None:
                p['options'].pop(field)
            else:
                p['options'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(UsageError):
                policy.validate_new(p)
        for field in ('task_class', 'resource_class'):
            p = self.payload()
            p.pop(field)
            with self.assertRaises(UsageError):
                policy.validate_new(p)
        p = self.payload()
        p['options'].pop('sandbox')
        with self.assertRaises(UsageError):
            policy.validate_new(p)

    def test_no_prose_classification_or_production_test_exemption(self):
        p = self.payload()
        p.pop('task_class')
        p['prompt'] = 'simple computer use task; please use the right model'
        with self.assertRaises(UsageError):
            policy.validate_new(p)
        p = self.payload()
        p['test_only_reason'] = 'smoke'
        with self.assertRaises(UsageError):
            policy.validate_new(p)
        p = dict(engine='fixture', options={}, cwd=self.tmp.name, prompt='isolated', test_only_reason='unit fixture')
        policy.validate_new(p)
        self.assertEqual(policy.settings('fixture', p)['effective_settings']['test_only_reason'], 'unit fixture')

    def test_validation_is_new_work_only_and_does_not_rewrite_legacy(self):
        p = self.payload()
        with patch.object(policy, 'validate_new'):
            _, task, sha = self.ledger.submit(scope='old', key='legacy', payload={k:v for k,v in p.items()
                if k not in ('task_class','resource_class')}, prompt='fixture', native_session_id=None)
        old = json.loads(task['payload_json'])
        status, duplicate, other_sha = self.ledger.submit(scope='old', key='legacy', payload=old,
                                                        prompt='fixture', native_session_id=None)
        self.assertEqual((status, duplicate['task_id'], other_sha), ('duplicate', task['task_id'], sha))
        self.assertEqual(json.loads(duplicate['payload_json']), old)
        self.assertEqual(policy.warnings(old['engine'], old)[0]['code'], 'production_settings_unverified')
        with self.assertRaises(UsageError):
            self.ledger.submit(scope='old', key='new', payload=old, prompt='fixture', native_session_id=None)

    def test_cli_receipt_status_event_and_help_include_effective_settings(self):
        args = ['--home', str(self.home), 'submit', '--key', 'new', '--engine', 'codex', '--cwd', self.tmp.name,
                '--prompt', 'synthetic', '--task-class', 'complex', '--resource-class', 'computer_use',
                '--option', 'model="gpt-6.1-sol"', '--option', 'effort="max"', '--option', 'sandbox="workspace-write"']
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(cli, 'spawn_keeper'), patch.object(cli, 'Ledger', return_value=self.ledger):
            self.assertEqual(cli.main(args), 0)
        receipt = json.loads(out.getvalue())
        effective = receipt['effective_settings']
        self.assertEqual(effective['resource_class'], 'computer_use')
        self.ledger.finish(receipt['attempt_id'], from_states=('pending',), outcome='succeeded', native={'result':'status=ok'})
        view = cli.task_view(self.ledger, receipt['task_id'], probe=False)
        self.assertEqual(view['task']['effective_settings'], effective)
        self.assertEqual(view['events'][0]['body']['effective_settings'], effective)
        self.assertEqual(json.loads(self.ledger.get_task(receipt['task_id'])['payload_json'])['options']['sandbox'], 'workspace-write')


class SharedCapacity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'ledger'
        init_ledger(self.home)
        self.ledger = Ledger(self.home)
        self.addCleanup(self.ledger.close)

    def submit(self, key, scope=None, engine='fixture', resource='standard', **extra):
        p = dict(engine=engine, cwd=self.tmp.name, options={}, prompt='fixture', resource_class=resource, **extra)
        if engine != 'fixture':
            task_class = 'complex' if engine == 'codex' else 'highest_quality'
            _, model, effort = policy.TIERS[task_class]
            p.update(task_class=task_class, options=dict(model=model, effort=effort,
                     **({'sandbox':'workspace-write'} if engine == 'codex' else {})))
        _, task, _ = self.ledger.submit(scope=scope or key, key=key, payload=p, prompt='fixture', native_session_id=None)
        return self.ledger.current_attempt(task['task_id'])

    def claim(self, att, ledger=None):
        db = ledger or self.ledger
        self.assertTrue(db.claim(att['attempt_id'], keeper_pid=123, keeper_token='fixture', keeper_pgid=123, host='fixture'))
        return db.get_attempt(att['attempt_id'])['state']

    def finish(self, att, **extra):
        self.ledger.finish(att['attempt_id'], from_states=('claimed','running','pending','waiting','unknown'),
                           outcome='succeeded', native={'result':'status=ok'}, **extra)

    def test_cross_scope_simultaneous_starts_and_mixed_engines_total_two(self):
        attempts = [self.submit(str(i), engine='codex' if i % 2 else 'claude') for i in range(8)]
        barrier = threading.Barrier(len(attempts))
        def claim(att):
            db = Ledger(self.home)
            try:
                barrier.wait(timeout=10)
                return self.claim(att, db)
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=len(attempts)) as pool:
            states = list(pool.map(claim, attempts))
        self.assertEqual(states.count('claimed'), 2)
        self.assertEqual(states.count('waiting'), 6)
        self.finish(attempts[0])
        self.assertEqual(self.ledger.admit_waiting(attempts[3]['attempt_id'], keeper_pid=123), 'waiting')
        self.assertEqual(self.ledger.admit_waiting(attempts[2]['attempt_id'], keeper_pid=123), 'ready')
        self.finish(attempts[1])
        self.assertEqual(self.ledger.admit_waiting(attempts[3]['attempt_id'], keeper_pid=123), 'ready')
        self.assertFalse(self.ledger.mark_spawning(attempts[3]['attempt_id'], keeper_pid=999))
        self.assertTrue(self.ledger.mark_spawning(attempts[3]['attempt_id'], keeper_pid=123))
        self.assertFalse(self.ledger.mark_spawning(attempts[3]['attempt_id'], keeper_pid=123))

    def test_cu_singleton_does_not_block_standard_slot_and_releases(self):
        cu1, cu2, standard = self.submit('cu1', resource='computer_use'), self.submit('cu2', resource='computer_use'), self.submit('s')
        self.assertEqual(self.claim(cu1), 'claimed')
        self.assertEqual(self.claim(cu2), 'waiting')
        self.assertEqual(self.claim(standard), 'claimed')
        self.finish(cu1)
        self.assertEqual(self.ledger.admit_waiting(cu2['attempt_id'], keeper_pid=123), 'ready')

    def test_dependency_wait_takes_no_slot_and_queue_remains_scoped_fifo(self):
        dep = self.submit('dep', scope='s')
        waiter = self.submit('wait', scope='s', after=dep['task_id'], queue='q')
        ordinary = self.submit('ordinary')
        self.assertEqual(self.claim(waiter), 'waiting')
        self.assertEqual(self.claim(dep), 'claimed')
        self.assertEqual(self.claim(ordinary), 'claimed')
        self.finish(dep)
        self.assertEqual(self.ledger.admit_waiting(waiter['attempt_id'], keeper_pid=123), 'ready')
        second = self.submit('second', scope='s', queue='q')
        self.assertEqual(self.claim(second), 'waiting')
        self.finish(waiter)
        self.assertEqual(self.ledger.admit_waiting(second['attempt_id'], keeper_pid=123), 'ready')

    def test_restart_unknown_and_released_claim_are_conservative_and_idempotent(self):
        one, two, three = [self.submit(str(i)) for i in range(3)]
        self.claim(one); self.claim(two); self.claim(three)
        self.ledger.mark_unknown(one['attempt_id'], from_states=('claimed',), evidence={})
        restarted = Ledger(self.home)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.admit_waiting(three['attempt_id'], keeper_pid=123), 'waiting')
        self.assertTrue(restarted.release_claim(two['attempt_id'], keeper_pid=123, evidence={'fixture':'crash before spawn'}))
        # Older ready work retains FIFO position after restart.
        self.assertEqual(restarted.admit_waiting(three['attempt_id'], keeper_pid=123), 'waiting')
        self.assertEqual(self.claim(two, restarted), 'claimed')
        self.assertFalse(restarted.claim(two['attempt_id'], keeper_pid=123, keeper_token='fixture', keeper_pgid=123, host='fixture'))
        self.finish(one, evidence={'processes_stopped': True})
        self.assertEqual(restarted.admit_waiting(three['attempt_id'], keeper_pid=123), 'ready')

    def test_stopped_unknown_releases_capacity_across_restart_and_explicit_rerun(self):
        one, two, three = [self.submit(str(i)) for i in range(3)]
        self.assertEqual(self.claim(one), 'claimed')
        self.assertEqual(self.claim(two), 'claimed')
        self.assertEqual(self.claim(three), 'waiting')
        self.ledger.mark_unknown(one['attempt_id'], from_states=('claimed',),
                                 evidence={'process_group_alive': False}, processes_stopped=True)
        restarted = Ledger(self.home)
        self.addCleanup(restarted.close)
        proof = json.loads(restarted.get_attempt(one['attempt_id'])['evidence_json'])
        self.assertIs(proof.get('processes_stopped'), True)
        self.assertEqual(restarted.admit_waiting(three['attempt_id'], keeper_pid=123), 'ready')
        self.assertEqual(len(restarted.attempts(one['task_id'])), 1, 'capacity release never reruns work')
        rerun = restarted.new_attempt(one['task_id'], expect_attempt_no=1, reason='rerun',
                                      prompt='explicit rerun', resume_session_id=None, native_session_id=None)
        self.assertEqual(self.claim(rerun, restarted), 'waiting', 'the two active tasks still own both slots')
        self.finish(two)
        self.assertEqual(restarted.admit_waiting(rerun['attempt_id'], keeper_pid=123), 'ready')
        self.assertEqual(restarted.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE state IN ('claimed','running')").fetchone()[0], 2)

    def test_stops_never_release_ok_or_partial_dependency(self):
        dep = self.submit('dep', scope='s')
        self.ledger.finish(dep['attempt_id'], from_states=('pending',), outcome='succeeded', native={'result':'status=blocked'})
        waiter = self.submit('wait', scope='s', after=dep['task_id'], after_require='ok_or_partial')
        self.assertEqual(self.claim(waiter), 'waiting')
        self.assertEqual(self.ledger.admit_waiting(waiter['attempt_id'], keeper_pid=123), 'blocked')

    def test_unclassified_legacy_work_reserves_cu_budget_without_prose_inference(self):
        legacy = self.submit('legacy', engine='codex')
        task = self.ledger.get_task(legacy['task_id'])
        payload = json.loads(task['payload_json'])
        payload.pop('resource_class')
        payload.pop('task_class')
        payload['prompt'] = 'just text; cannot establish resource usage'
        with self.ledger.tx() as c:
            c.execute('UPDATE tasks SET payload_json=? WHERE task_id=?', (json.dumps(payload), legacy['task_id']))
        self.assertEqual(self.claim(legacy), 'claimed')
        cu = self.submit('cu', resource='computer_use')
        self.assertEqual(self.claim(cu), 'waiting')
        self.assertEqual(self.claim(self.submit('standard')), 'claimed')
        self.assertIsNone(policy.settings('codex', payload)['effective_settings']['resource_class'])

    def test_duplicate_submission_creates_no_extra_attempt_or_capacity_slot(self):
        one = self.submit('same', scope='s')
        duplicate = self.submit('same', scope='s')
        self.assertEqual(one['attempt_id'], duplicate['attempt_id'])
        self.assertEqual(self.claim(one), 'claimed')
        self.assertEqual(self.claim(self.submit('two')), 'claimed')
        self.assertEqual(self.claim(self.submit('three')), 'waiting')
        self.assertEqual(self.ledger.conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 3)

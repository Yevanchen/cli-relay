#!/usr/bin/env python3
"""Separate agy acceptance, with a new local directory and no native session/notifier tests.

Default: gated preflight only. Real model calls require --run-models AND an independently
verified native AgyContract in source. Never installs, logs in, scans credentials or configures
accounts. Run locally on the already authenticated machine; not via a remote dispatcher.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from cli_relay import engines, policy  # noqa: E402


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def source_sha():
    out = subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], capture_output=True,
                         text=True, timeout=10)
    return out.stdout.strip() if out.returncode == 0 else None


def relay(home, *args):
    proc = subprocess.run([sys.executable, '-m', 'cli_relay', '--home', str(home), *args],
        env={**os.environ, 'PYTHONPATH': str(REPO)}, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=180)
    return proc.returncode, json.loads(proc.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', required=True, type=Path, help='new acceptance directory; existing paths refused')
    parser.add_argument('--task-class', choices=('carbon', 'highest_quality'), default='carbon',
                        help='select Carbon low/medium/high or the single exact Neon xhigh request; never substitutes models')
    parser.add_argument('--run-models', action='store_true',
                        help='explicitly spend subscription quota on the selected class, only after native gates pass')
    args = parser.parse_args(argv)
    directory = args.dir.expanduser().resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    except OSError:
        parser.error('--dir must be a new writable directory, never a production relay home')
    home, work = directory / 'relay-home', directory / 'work'
    work.mkdir()
    combinations = policy.CARBON_TIERS['carbon'] if args.task_class == 'carbon' else policy.TIERS['highest_quality']
    report = {'engine': 'agy', 'task_class': args.task_class, 'started_at': timestamp(), 'source_sha': source_sha(),
              'verification': 'invocation_only', 'steps': []}

    def finish(code):
        report.update(finished_at=timestamp(), verdict='PASS' if code == 0 else 'NOT RUN' if code == 3 else 'FAIL')
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'verdict': report['verdict'], 'report': str(directory / 'report.json')}, ensure_ascii=False))
        return code

    try:
        pf = engines.preflight('agy', dict(os.environ), cwd=str(work))
        # Only presence of these exact IDs is retained, never an identity or full directory dump.
        visible = pf.detail.get('models')
        report['model_presence'] = {model: (model in visible if visible is not None else None)
                                    for _, model, _ in policy.AGY_COMBINATIONS}
        report['preflight'] = {'ok': pf.ok, 'reason': pf.reason,
                               **{key: value for key, value in pf.detail.items() if key != 'models'}}
        report['command_structure'] = 'doctor --engine agy --cwd <acceptance-work>'
        if not pf.ok or not args.run_models:
            reason = pf.reason or 'explicit --run-models required'
            for _, model, effort in combinations:
                report['steps'].append({'model': model, 'effort': effort, 'verdict': 'NOT RUN', 'reason': reason})
            return finish(3)

        if any(report['model_presence'][model] is not True for _, model, _ in combinations):
            report['reason'] = 'agy_model_unavailable'
            for _, model, effort in combinations:
                report['steps'].append({'model': model, 'effort': effort, 'verdict': 'NOT RUN',
                                        'reason': 'agy_model_unavailable'})
            return finish(3)

        rc, _ = relay(home, 'init')
        if rc != 0:
            return finish(1)
        for _, model, effort in combinations:
            command = ['submit', '--key', 'acceptance-' + args.task_class + '-' + effort,
                '--engine', 'agy', '--task-class', args.task_class,
                '--resource-class', 'standard', '--cwd', str(work), '--option', 'model=' + json.dumps(model),
                '--option', 'effort=' + json.dumps(effort), '--option', 'require_completion_marker=true',
                '--prompt', '只回复短文本，不使用工具、不编辑文件。最后一行写 CLI-RELAY-STATUS: complete。']
            rc, receipt = relay(home, *command)
            step = {'model': model, 'effort': effort, 'verdict': 'FAIL', 'submit_returncode': rc}
            report['steps'].append(step)
            if rc != 0:
                return finish(1)
            rc, done = relay(home, 'wait', receipt['task_id'], '--timeout', '120', '--interval', '0.2')
            if done.get('timed_out'):
                # wait is not cancellation. Explicitly stop this acceptance attempt; never resend it.
                relay(home, 'cancel', receipt['task_id'])
                relay(home, 'wait', receipt['task_id'], '--timeout', '30')
                step['reason'] = 'acceptance_wait_timeout_cancel_requested'
                return finish(1)
            att = done['attempts'][-1]
            step.update(task_id=receipt['task_id'], outcome=done['task']['state'], exit=att['exit'],
                        effective_settings=done['task']['effective_settings'],
                        source=(att.get('native') or {}).get('source'))
            rc, duplicate = relay(home, *command)
            step['duplicate_same_task'] = (rc == 0 and duplicate.get('status') == 'duplicate' and
                                          duplicate.get('task_id') == receipt['task_id'])
            if (done['task']['state'] != 'succeeded' or att['exit'] != {'code': 0, 'signal': None, 'known': True}
                    or not step['duplicate_same_task']):
                return finish(1)
            step['verdict'] = 'PASS'
        report['manual_followups'] = ['native unauthenticated no-login proof', 'no implicit API/proxy fallback',
                                      'native tool authorization', 'real cancellation if duration permits']
        return finish(0)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        report['reason'] = 'acceptance_unverified'  # never copy native errors or URLs into the report
        return finish(1)


if __name__ == '__main__':
    raise SystemExit(main())

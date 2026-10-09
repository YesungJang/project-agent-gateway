#!/usr/bin/env python3
"""Generate synthetic history only, in a new external directory. No services."""

import argparse
import datetime as dt
import fcntl
import os
from pathlib import Path

import events


SCENARIOS = ('completed', 'repair', 'repair-limit', 'risk', 'verifier-error',
             'bootstrap-failed', 'ci-pending', 'ci-failed', 'crash', 'cut-tail', 'FAST', 'DEBATE')


def generate(output):
    output = Path(output).expanduser().resolve()
    checkout = Path(__file__).parent.resolve()
    if output == checkout or checkout in output.parents:
        raise ValueError('Demo output must be outside the checkout')
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    lock = (output / 'worker.lock').open('x')
    os.chmod(output / 'worker.lock', 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    result = {}
    original_clock = events.utc_now
    try:
        for number, scenario in enumerate(SCENARIOS, 1):
            # Older examples are intentionally stale; the last is recent.
            timestamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=(len(SCENARIOS)-number)*3)
            def clock():
                nonlocal timestamp
                timestamp += dt.timedelta(seconds=1)
                return timestamp.isoformat().replace('+00:00', 'Z')
            events.utc_now = clock
            writer = events.EventWriter(output, 'example/gateway', lock.fileno())
            result[scenario] = writer.run_id
            def emit(stage, outcome, task=False, attempt=0, **details):
                return writer.append(stage, outcome, issue=number if stage not in ('queue', 'worker') else None,
                    task_id='task-1' if task else None, attempt=attempt, **details)
            mode = scenario if scenario in ('FAST', 'DEBATE') else 'NORMAL'
            try:
                emit('worker', 'started', mode=mode, publish=True, pid=12345)
                emit('queue', 'PASS', queue_issues=[number, number+1], observed_count=2, queue_limit=100)
                emit('run', 'started', mode=mode, publish=True)
                emit('claim', 'started')
                emit('worktree', 'started')
                emit('worktree', 'PASS')
                emit('bootstrap', 'started')
                if scenario == 'bootstrap-failed':
                    emit('bootstrap', 'ERROR', diagnostic_code='BOOTSTRAP_FAILED')
                    emit('run', 'approval-required')
                    emit('worker', 'ERROR', diagnostic_code='BOOTSTRAP_FAILED')
                    emit('worker', 'exited')
                    continue
                emit('bootstrap', 'PASS')
                emit('context', 'PASS')
                if mode == 'DEBATE':
                    emit('debate', 'started')
                    emit('debate', 'PASS')
                emit('plan', 'started', attempt=1)
                emit('plan', 'PASS', attempt=1, task_count=1)
                emit('plan_gate', 'skipped' if mode == 'FAST' else 'PASS', attempt=1, phase='PLAN')
                emit('task', 'started', task=True, criteria_count=2)
                emit('task', 'PASS', task=True, attempt=1)
                emit('task_validation', 'PASS', task=True, attempt=1)
                if scenario in ('crash', 'cut-tail'):
                    emit('task_gate', 'started', task=True, attempt=1)
                    continue
                if scenario in ('risk', 'verifier-error'):
                    emit('task_gate', 'RISK' if scenario == 'risk' else 'ERROR', task=True, attempt=1,
                         diagnostic_code='VERIFIER_RISK' if scenario == 'risk' else 'VERIFIER_ERROR')
                    emit('run', 'approval-required')
                    emit('worker', 'exited')
                    continue
                if scenario in ('repair', 'repair-limit'):
                    emit('task_gate', 'FAIL', task=True, attempt=1, diagnostic_code='GATE_FAILED')
                    emit('repair', 'started', task=True, attempt=2, phase='TASK', repair_count=1)
                    if scenario == 'repair-limit':
                        emit('task_gate', 'FAIL', task=True, attempt=2, diagnostic_code='GATE_FAILED')
                        emit('repair', 'FAIL', task=True, attempt=2, phase='TASK', repair_count=1)
                        emit('run', 'approval-required', diagnostic_code='APPROVAL_REQUIRED')
                        emit('worker', 'exited')
                        continue
                    emit('task_gate', 'PASS', task=True, attempt=2)
                    emit('repair', 'PASS', task=True, attempt=2, phase='TASK', repair_count=1)
                else:
                    emit('task_gate', 'skipped' if mode == 'FAST' else 'PASS', task=True, attempt=1)
                emit('task_commit', 'PASS', task=True, attempt=2 if scenario == 'repair' else 1)
                emit('task', 'done', task=True, attempt=2 if scenario == 'repair' else 1)
                emit('full_validation', 'PASS')
                emit('pr_prepare', 'PASS', head='a'*40, base='b'*40)
                emit('pr_publish', 'PASS', pr_number=100+number, head='a'*40, base='b'*40)
                if mode == 'FAST':
                    for stage in ('ci', 'merge_gate', 'merge', 'issue_close'):
                        emit(stage, 'skipped')
                    emit('run', 'pr-ready')
                else:
                    ci = 'pending' if scenario == 'ci-pending' else 'FAIL' if scenario == 'ci-failed' else 'PASS'
                    emit('ci', ci, checks=[{'identifier': 'check-run:CI/unit-tests', 'outcome': ci},
                        {'identifier': 'status-context:lint', 'outcome': 'PASS'}])
                    emit('merge_gate', ci)
                    if ci != 'PASS':
                        emit('run', 'approval-required', diagnostic_code='GATE_FAILED')
                    else:
                        emit('merge', 'PASS', merged=True)
                        emit('issue_close', 'PASS')
                        emit('run', 'done')
                emit('claim', 'PASS')
                emit('worker', 'exited')
            finally:
                writer.close()
                if scenario == 'cut-tail':
                    with writer.path.open('ab') as stream:
                        stream.write(b'{"version":1,')
        return result
    finally:
        events.utc_now = original_clock
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='NEW directory outside checkout; existing directories are refused')
    args = parser.parse_args()
    try:
        result = generate(args.output)
    except (OSError, ValueError):
        parser.error('Use a new, writable directory outside the checkout')
    print('Created synthetic scenarios only: ' + ', '.join(result))
    print(f'python3 dashboard.py --state {args.output} --port 8765')


if __name__ == '__main__':
    main()

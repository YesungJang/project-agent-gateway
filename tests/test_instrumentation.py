import contextlib
import io
import json
import unittest
from unittest.mock import patch

import gateway
from events import read_lines
import test_gateway


class InstrumentationTests(unittest.TestCase):
    setUp = test_gateway.GatewayTests.setUp
    tearDown = test_gateway.GatewayTests.tearDown
    worker = test_gateway.GatewayTests.worker

    def history(self):
        files = sorted((self.root / 'state/events').glob('*.jsonl'))
        records = []
        for file in files:
            values, errors = read_lines(file)
            self.assertEqual(errors, [])
            records.extend(values)
        return records

    def test_normal_repair_dry_run_restart_and_no_prompt(self):
        w = self.worker(fail_task=True)
        self.assertTrue(w.once())
        records = self.history()
        stages = {e['stage'] for e in records}
        self.assertTrue({'worker', 'queue', 'claim', 'worktree', 'bootstrap', 'plan',
                         'plan_gate', 'task', 'task_validation', 'task_gate', 'repair',
                         'task_commit', 'full_validation', 'pr_prepare', 'run'} <= stages)
        self.assertTrue(any(e['stage'] == 'repair' and e['attempt'] == 2 for e in records))
        self.assertTrue(any(e['stage'] == 'task' and e['task_id'] == 'task-1' and e['outcome'] == 'done' for e in records))
        self.assertTrue(any(e['stage'] == 'pr_publish' and e['outcome'] == 'skipped' for e in records))
        self.assertNotIn('touch should-never-exist', json.dumps(records))
        self.assertFalse(w.once())
        self.assertEqual(len(list((self.root / 'state/events').glob('*.jsonl'))), 2)
        self.assertTrue(any(e['outcome'] == 'idle' for e in self.history()))

    def test_event_disk_failure_preserves_calls_state_and_gate_decisions(self):
        for failure in ('init', 'append'):
            with self.subTest(failure=failure):
                state = self.root / 'state'
                # Compare complete operation order in separate state/worktree sets.
                baseline = self.worker()
                baseline.state = self.root / ('baseline-' + failure)
                baseline.state.mkdir()
                with patch.object(gateway, 'EventWriter', side_effect=OSError('secret')):
                    with contextlib.redirect_stderr(io.StringIO()):
                        baseline.once()
                w = self.worker()
                w.state = self.root / ('observed-' + failure)
                w.state.mkdir()
                if failure == 'init':
                    target = patch.object(gateway, 'EventWriter', side_effect=OSError('secret'))
                else:
                    target = patch('events.EventWriter.append', side_effect=OSError('secret'))
                stderr = io.StringIO()
                with target, contextlib.redirect_stderr(stderr):
                    w.once()
                self.assertEqual(w.calls, baseline.calls)
                self.assertEqual(json.loads((w.state / 'issue-1.json').read_text())['state'], 'pr-ready')
                self.assertIn('OBSERVATION_FAILED', stderr.getvalue())
                self.assertNotIn('secret', stderr.getvalue())
                observed = self.worker()
                observed.state = self.root / ('normal-' + failure)
                observed.state.mkdir()
                observed.once()
                self.assertEqual(observed.calls, baseline.calls)
                self.assertEqual(json.loads((observed.state / 'issue-1.json').read_text())['state'], 'pr-ready')

    def test_bootstrap_error_and_risk_stop_without_repair(self):
        for error in (gateway.BootstrapFailure('SECRET'), gateway.VerifierRisk(['SECRET']),
                      gateway.VerificationFailure('SECRET')):
            with self.subTest(error=type(error).__name__):
                w = self.worker()
                w.state = self.root / type(error).__name__
                w.state.mkdir()
                if isinstance(error, gateway.BootstrapFailure):
                    w.bootstrap = lambda: (_ for _ in ()).throw(error)
                else:
                    w.verify = lambda _: (_ for _ in ()).throw(error)
                with self.assertRaises(type(error)):
                    w.once()
                records = read_lines(next((w.state / 'events').glob('*.jsonl')))[0]
                self.assertFalse(any(e['stage'] == 'repair' for e in records))
                self.assertTrue(any(e['outcome'] in ('ERROR', 'RISK') for e in records))
                self.assertNotIn('SECRET', json.dumps(records))

    def test_fast_and_debate_are_explicit(self):
        for mode in ('FAST', 'DEBATE'):
            w = self.worker()
            w.cfg['mode'] = mode
            w.state = self.root / mode
            w.state.mkdir()
            w.once()
            records = read_lines(next((w.state / 'events').glob('*.jsonl')))[0]
            if mode == 'FAST':
                self.assertEqual(w.verifications, 0)
                self.assertTrue(any(e['stage'] == 'task_gate' and e['outcome'] == 'skipped' for e in records))
            else:
                self.assertTrue(any(e['stage'] == 'debate' and e['outcome'] == 'PASS' for e in records))

    def test_crash_is_not_success(self):
        w = self.worker()
        w.claude = lambda *a: (_ for _ in ()).throw(RuntimeError('SECRET'))
        with self.assertRaises(RuntimeError):
            w.once()
        records = self.history()
        self.assertTrue(any(e['details'].get('diagnostic_code') == 'CRASH' for e in records))
        self.assertEqual([e for e in records if e['stage'] == 'run'][-1]['outcome'], 'approval-required')
        self.assertNotIn('SECRET', json.dumps(records))

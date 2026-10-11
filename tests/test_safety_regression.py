import contextlib
import copy
import fcntl
import http.client
import io
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import dashboard
import events
import gateway
import test_gateway

# Newest pre-instrumentation behaviour: proves instrumentation stayed inert across
# the measured-validation change. Advance this only for an intended behaviour change.
BASELINE = '002ac0b'


class SafetyRegressionTests(unittest.TestCase):
    setUp = test_gateway.GatewayTests.setUp
    tearDown = test_gateway.GatewayTests.tearDown
    worker = test_gateway.GatewayTests.worker
    published_worker = test_gateway.GatewayTests.published_worker

    def test_original_and_observed_external_operation_order_and_gate_outcomes(self):
        checkout = Path(gateway.__file__).parent
        try:
            source = subprocess.check_output(['git', 'show', BASELINE + ':gateway.py'], cwd=checkout, text=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            self.skipTest('Original Git object unavailable; run from a full Git checkout')
        baseline = types.ModuleType('gateway_baseline')
        exec(compile(source, 'gateway_baseline.py', 'exec'), baseline.__dict__)

        def execute(module, scenario, label):
            trace = []
            cfg = copy.deepcopy(self.cfg)
            cfg['require_branch_protection'] = False
            cfg['ci_wait_seconds'] = 1
            cfg['mode'] = scenario if scenario in ('FAST', 'DEBATE') else 'NORMAL'
            state = self.root / label
            class Worker(module.Gateway):
                def claude(self, payload, schema):
                    trace.append(('claude', 'plan' if schema == module.PLAN_SCHEMA else 'task'))
                    if scenario == 'crash':
                        raise RuntimeError('synthetic crash')
                    return test_gateway.LocalGateway.claude(self, payload, schema)
                def verify(self, payload):
                    trace.append(('verify', payload['verification_phase']))
                    if scenario == 'risk':
                        raise module.VerifierRisk(['synthetic risk'])
                    if scenario == 'error':
                        raise module.VerificationFailure('synthetic error')
                    return test_gateway.LocalGateway.verify(self, payload)
                def tests(self, names):
                    trace.append(('tests', tuple(names)))
                    return super().tests(names)
                def bootstrap(self):
                    trace.append(('bootstrap',))
                    if scenario == 'bootstrap':
                        raise module.BootstrapFailure('synthetic bootstrap')
                    return super().bootstrap()
                def gh(self, *args):
                    trace.append(('gh', args))
                    if args[:2] == ('issue', 'list'):
                        return json.dumps([dict(number=1, title='Add greeting', body='synthetic', labels=[])])
                    if args[:2] == ('pr', 'create'):
                        return 'https://github.com/owner/repo/pull/1'
                    if args[:2] == ('pr', 'view'):
                        if args[-1] == 'state,headRefOid':
                            return json.dumps(dict(state='MERGED', headRefOid=self.job_data['head']))
                        return json.dumps(dict(headRefOid=self.job_data['head'], baseRefOid=self.job_data['base'],
                            baseRefName='main', state='OPEN', isDraft=False, mergeable='MERGEABLE', mergeStateStatus='CLEAN',
                            files=[{'path':'src/greeting.py'}], statusCheckRollup=[dict(__typename='CheckRun',
                                workflowName='CI', name='unit', status='IN_PROGRESS' if scenario == 'pending' else 'COMPLETED',
                                conclusion=None if scenario == 'pending' else 'FAILURE' if scenario == 'ci-fail' else 'SUCCESS')]))
                    return ''
            w = Worker(cfg, state, publish=True)
            w.fail_task = scenario == 'repair'
            w.fail_plan = scenario == 'plan-fail'
            w.verifications = 0
            original_git = w.git
            def git(*args):
                if args[0] in ('fetch', 'push'):
                    trace.append(('git-external', args))
                    return ''
                return original_git(*args)
            w.git = git
            try:
                w.once()
                outcome = 'returned'
            except Exception as exc:
                outcome = type(exc).__name__
            saved = json.loads((state / 'issue-1.json').read_text())
            def normalize(value):
                if isinstance(value, tuple):
                    return tuple(normalize(v) for v in value)
                if not isinstance(value, str):
                    return value
                if value == w.job_data.get('head'):
                    return '<HEAD>'
                if value == w.job_data.get('base'):
                    return '<BASE>'
                value = value.replace(str(state), '<STATE>')
                return re.sub(r'gateway/issue-1-[0-9]+', '<BRANCH>', value)
            return normalize(tuple(trace)), saved['state'], outcome

        for scenario in ('NORMAL', 'FAST', 'DEBATE', 'repair', 'plan-fail', 'risk', 'error',
                         'bootstrap', 'pending', 'ci-fail', 'crash'):
            with self.subTest(scenario=scenario):
                before = execute(baseline, scenario, 'original-' + scenario)
                after = execute(gateway, scenario, 'observed-' + scenario)
                self.assertEqual(before, after)

    def test_full_validation_mutation_not_reported_as_pass(self):
        w = self.worker()
        w.cfg['validation']['mutate'] = ['python3', '-c', "from pathlib import Path; p = Path('src/greeting.py'); p.exists() and p.write_text('changed')"]
        w.cfg['full_validation'] = ['mutate']
        with self.assertRaises(gateway.ApprovalRequired):
            w.once()
        records = events.read_lines(next((w.state / 'events').glob('*.jsonl')))[0]
        results = [e['outcome'] for e in records if e['stage'] == 'full_validation']
        self.assertEqual(results, ['started', 'FAIL'])
        self.assertFalse(any(c[:2] == ('pr', 'merge') for c in w.calls))

    def test_observer_close_failure_cannot_change_done_or_cause_retries(self):
        w = self.published_worker()
        original = events.EventWriter.close
        def close(writer):
            original(writer)
            raise OSError('private secret')
        stderr = io.StringIO()
        with patch.object(events.EventWriter, 'close', close), contextlib.redirect_stderr(stderr):
            self.assertTrue(w.once())
        self.assertEqual(json.loads((w.state / 'issue-1.json').read_text())['state'], 'done')
        self.assertEqual(sum(c[:2] == ('pr', 'merge') for c in w.calls), 1)
        self.assertEqual(sum(c[:2] == ('issue', 'close') for c in w.calls), 1)
        self.assertNotIn('private secret', stderr.getvalue())

    def test_wrapped_timeout_remains_error_without_private_exception_text(self):
        for kind in (gateway.BootstrapFailure, gateway.VerificationFailure):
            exc = kind('private wrapper')
            exc.__cause__ = gateway.ApprovalRequired('Timeout: private command')
            self.assertEqual(gateway.Gateway.observation_error(exc), ('ERROR', 'TIMEOUT'))


class HTTPPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.lock = (self.state / 'worker.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.writer = events.EventWriter(self.state, 'owner/repo', self.lock.fileno())

    def tearDown(self):
        self.writer.close()
        self.lock.close()
        self.temp.cleanup()

    def test_allowlist_at_second_read_boundary_unknown_fields_and_private_files(self):
        event = self.writer.append('run', 'approval-required', issue=1, diagnostic_code='GATE_FAILED')
        event['prompt'] = 'SECRET'
        event['details'].update(summary='SECRET', token='SECRET', stdout='SECRET', path='/private/SECRET',
                                 private_log_id='/private/SECRET', criteria=['SECRET'], checks=[{'identifier':'<img SECRET>', 'outcome':'PASS'}])
        self.writer.close()
        self.writer.path.write_text(json.dumps(event)+'\n')
        self.writer.file = self.writer.path.open('ab')
        (self.state / 'private-command.json').write_text('SECRET')
        (self.state / 'config.json').write_text('SECRET')
        with patch('subprocess.Popen', side_effect=AssertionError('Observer must not spawn processes')):
            value = dashboard.Reader(self.state).snapshot()
        self.assertNotIn('SECRET', json.dumps(value))
        self.assertNotIn('/private/', json.dumps(value))
        self.assertEqual(value['runs'][0]['diagnostics'][0]['summary'], events.DIAGNOSTICS['GATE_FAILED'])

    def test_corrupt_nested_oversize_conflicts_and_limits(self):
        event = self.writer.append('run', 'done', issue=1)
        with self.writer.path.open('ab') as f:
            conflict = copy.deepcopy(event)
            conflict['outcome'] = 'FAIL'
            f.write((json.dumps(conflict)+'\n').encode())
            f.write(('['*1100+']'*1100+'\n').encode())
            f.write(('x'*(events.MAX_LINE+1)+'\n').encode())
        value = dashboard.Reader(self.state).snapshot()
        self.assertEqual(value['runs'][0]['status'], 'unknown')
        self.assertIn('conflicting-event-id', value['problems'])
        self.assertIn('oversize-line', value['problems'])
        self.assertIn('invalid-or-unsupported-event', value['problems'])
        with patch.object(dashboard, 'MAX_EVENTS', 1):
            self.assertIn('event-limit', dashboard.Reader(self.state).snapshot()['problems'])
        (self.state / 'issue-6.json').write_text('['*1100+']'*1100)
        self.assertIn('legacy-state-unreadable', dashboard.Reader(self.state).snapshot()['problems'])

    def test_duplicate_headers_invalid_http_response_and_response_limit(self):
        self.writer.append('worker', 'started')
        with dashboard.Server(self.state, 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for header in ('Host', 'Origin'):
                    c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    c.putrequest('GET', '/api/snapshot', skip_host=True)
                    c.putheader('Host', f'127.0.0.1:{server.server_port}')
                    if header == 'Host':
                        c.putheader('Host', f'127.0.0.1:{server.server_port}')
                    else:
                        c.putheader('Origin', f'http://127.0.0.1:{server.server_port}')
                        c.putheader('Origin', 'https://attacker.example')
                    c.endheaders()
                    r = c.getresponse()
                    self.assertEqual(r.status, 403)
                    self.assertNotIn('Access-Control-Allow-Origin', dict(r.getheaders()))
                    r.read(); c.close()
                with patch.object(dashboard, 'MAX_RESPONSE', 80):
                    c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    c.request('GET', '/api/snapshot')
                    r = c.getresponse()
                    self.assertEqual(r.status, 503)
                    self.assertEqual(json.loads(r.read()), {'error':'response-limit'})
                    c.close()
            finally:
                server.shutdown()
                thread.join(timeout=3)

    def test_invalid_timestamp_and_run_sequence_cannot_create_success(self):
        event = self.writer.append('run', 'done', issue=1)
        self.writer.close()
        event['timestamp'] = '2026-10-09Z'  # ISO parser alone can accept a naive timestamp.
        self.writer.path.write_text(json.dumps(event)+'\n')
        self.writer.file = self.writer.path.open('ab')
        value = dashboard.Reader(self.state).snapshot()
        self.assertEqual(value['runs'][0]['status'], 'unknown')
        self.assertIn('invalid-or-unsupported-event', value['problems'])
        event['timestamp'] = events.utc_now()
        event['sequence'] = 2
        self.writer.close()
        self.writer.path.write_text(json.dumps(event)+'\n')
        self.writer.file = self.writer.path.open('ab')
        value = dashboard.Reader(self.state).snapshot()
        self.assertEqual(value['runs'][0]['status'], 'unknown')
        self.assertIn('sequence-gap', value['problems'])

    def test_cli_startup_and_shutdown_smoke_with_existing_state(self):
        self.writer.append('worker', 'started')
        # The child and client deliberately share this test's network namespace.
        probe = socket.socket()
        try:
            probe.bind(('127.0.0.1', 8765))
        except OSError:
            probe.close()
            self.skipTest('Default localhost port 8765 is already in use')
        port = probe.getsockname()[1]
        probe.close()
        checkout = Path(dashboard.__file__).parent
        child = subprocess.Popen([os.sys.executable, str(checkout/'dashboard.py'), '--state', str(self.state)],
            cwd=checkout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    c = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                    c.request('GET', '/')
                    r = c.getresponse()
                    body = r.read().decode()
                    self.assertEqual(r.status, 200)
                    self.assertIn('Pipeline dashboard', body)
                    c.close()
                    break
                except (ConnectionRefusedError, TimeoutError):
                    if child.poll() is not None or time.monotonic() > deadline:
                        self.fail('Observer CLI did not start')
                    time.sleep(.05)
        finally:
            child.terminate()
            out, err = child.communicate(timeout=3)
        self.assertIn('http://127.0.0.1:'+str(port), out)
        self.assertEqual(err, '')

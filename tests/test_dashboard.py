import datetime as dt
import fcntl
import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import dashboard
from events import EventWriter, read_lines


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.lock = (self.state / 'worker.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.writer = EventWriter(self.state, 'owner/repo', self.lock.fileno())

    def tearDown(self):
        self.writer.close()
        self.lock.close()
        self.temp.cleanup()

    def append(self, stage, outcome, **kwargs):
        return self.writer.append(stage, outcome, **kwargs)

    def test_replay_tasks_repairs_pr_ci_worker_and_stale(self):
        self.append('worker', 'started', pid=123)
        self.append('queue', 'PASS', queue_issues=[1, 2], observed_count=2, queue_limit=100)
        self.append('run', 'started', issue=1, mode='NORMAL', publish=True)
        self.append('task', 'started', issue=1, task_id='task-1', criteria_count=2)
        self.append('task_gate', 'FAIL', issue=1, task_id='task-1', attempt=1)
        self.append('repair', 'started', issue=1, task_id='task-1', attempt=2, phase='TASK', repair_count=1)
        self.append('task_gate', 'PASS', issue=1, task_id='task-1', attempt=2)
        self.append('task', 'done', issue=1, task_id='task-1', attempt=2)
        self.append('pr_publish', 'PASS', issue=1, pr_number=7, head='a'*40, base='b'*40)
        self.append('ci', 'PASS', checks=[{'identifier': 'check-run:CI/unit', 'outcome': 'PASS'}])
        self.append('merge', 'PASS', issue=1, merged=True)
        self.append('run', 'done', issue=1)
        self.append('worker', 'exited')
        value = dashboard.Reader(self.state).snapshot()
        run = value['runs'][0]
        self.assertEqual(run['status'], 'done')
        self.assertEqual(run['tasks'][0]['repairs'], 1)
        self.assertEqual(run['tasks'][0]['attempts'], 2)
        self.assertEqual(run['pr']['url'], 'https://github.com/owner/repo/pull/7')
        self.assertTrue(run['pr']['merge_confirmed'])
        self.assertEqual(value['queue']['observed_count'], 2)
        self.assertFalse(value['queue']['total_known'])
        self.assertEqual(value['worker']['status'], 'exited')
        self.assertFalse(value['worker']['pid_is_liveness'])
        later = dashboard.Reader(self.state).snapshot(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
        self.assertEqual(later['queue']['freshness'], 'stale')
        self.assertEqual(later['runs'][0]['freshness'], 'stale')
        self.assertEqual(later['worker']['status'], 'exited')

    def test_gap_duplicate_tail_and_unknown_are_not_success(self):
        one = self.append('worker', 'started')
        self.append('run', 'done', issue=1)
        with self.writer.path.open('ab') as stream:
            stream.write((json.dumps(one) + '\n').encode())  # exact duplicate is ignored
        history, problems = dashboard.Reader(self.state).history()
        self.assertEqual(len(history[self.writer.run_id][0]), 2)
        self.assertEqual(problems, [])
        with self.writer.path.open('ab') as stream:
            stream.write(b'{"private":"SECRET"')
        value = dashboard.Reader(self.state).snapshot()
        self.assertEqual(value['runs'][0]['status'], 'unknown')
        self.assertIn('incomplete-tail', value['problems'])
        self.assertNotIn('SECRET', json.dumps(value))

    def test_unknown_worker_legacy_state_redaction_and_no_fabricated_history(self):
        self.append('worker', 'started', pid=1)
        (self.state / 'issue-4.json').write_text(json.dumps(dict(issue=4, state='done', reason='SECRET',
            worktree='/private/path', plan={'prompt': 'SECRET'})))
        value = dashboard.Reader(self.state).snapshot()
        self.assertEqual(value['worker']['status'], 'unknown')
        self.assertEqual(value['legacy'][0]['timestamp'], None)
        self.assertEqual(value['legacy'][0]['stage'], 'unknown')
        self.assertNotIn('SECRET', json.dumps(value))
        self.assertNotIn('/private/path', json.dumps(value))

    def test_multiple_runs_and_sequence_order(self):
        first = self.append('run', 'started', issue=1)
        second = self.append('run', 'pr-ready', issue=1)
        # Simulate out-of-order and repeated persisted rows.
        self.writer.close()
        self.writer.path.write_text(json.dumps(second)+'\n'+json.dumps(first)+'\n')
        self.writer.file = self.writer.path.open('ab')
        newer = EventWriter(self.state, 'owner/repo', self.lock.fileno())
        try:
            newer.append('run', 'started', issue=2)
            newer.append('run', 'approval-required', issue=2)
        finally:
            newer.close()
        value = dashboard.Reader(self.state).snapshot()
        self.assertEqual(len(value['runs']), 2)
        self.assertEqual({v['issue'] for v in value['runs']}, {1, 2})
        self.assertEqual(value['quality'], 'observed')

    def test_http_boundaries_routes_cursor_and_state_unchanged(self):
        self.append('worker', 'started')
        self.append('run', 'pr-ready', issue=1)
        before = {p.name: p.read_bytes() for p in (self.state / 'events').iterdir()}
        with dashboard.Server(self.state, 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host = '127.0.0.1:' + str(server.server_port)
            self.assertEqual(server.server_address[0], '127.0.0.1')
            def request(method, path, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                connection.request(method, path, headers=headers or {})
                response = connection.getresponse()
                value = response.read()
                status, result_headers = response.status, dict(response.getheaders())
                connection.close()
                return status, json.loads(value), result_headers
            try:
                status, value, headers = request('GET', '/api/snapshot')
                self.assertEqual(status, 200)
                self.assertNotIn('Access-Control-Allow-Origin', headers)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(request('GET', '/api/snapshot?cursor=' + value['cursor'])[1]['changed'], False)
                events = request('GET', '/api/events?run='+self.writer.run_id+'&limit=1')[1]
                self.assertEqual(events['next_cursor'], 1)
                self.assertEqual(len(events['events']), 1)
                self.assertEqual(request('GET', '/api/events?run='+self.writer.run_id+'&after=1')[1]['next_cursor'], 2)
                self.assertEqual(request('GET', '/api/runs/'+self.writer.run_id)[0], 200)
                self.assertEqual(request('GET', '/api/runs?limit=1')[0], 200)
                for method in ('POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH', 'TRACE'):
                    self.assertEqual(request(method, '/api/snapshot')[0], 405)
                for hdr in ({'Host':'attacker.example'}, {'Origin':'https://attacker.example'},
                            {'Sec-Fetch-Site':'cross-site'}, {'Origin':'null'}, {'Host':'localhost:'+str(server.server_port)}):
                    self.assertEqual(request('GET', '/api/snapshot', hdr)[0], 403)
                self.assertEqual(request('GET', '/api/snapshot', {'Origin':'http://'+host})[0], 200)
                for path in ('/api/events?run=../../secret', '/api/runs?limit=101',
                             '/api/runs?after=-1', '/api/runs?limit=1&limit=2', '/api/snapshot?path=/secret'):
                    self.assertEqual(request('GET', path)[0], 400)
                for path in ('/../../gateway.py', '/events', '/worker.lock', '/api/logs', '/config.example.json'):
                    self.assertEqual(request('GET', path)[0], 404)
            finally:
                server.shutdown()
                thread.join(timeout=3)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.state / 'events').iterdir()})

    def test_reader_rejects_symlinks_and_unknown_version(self):
        event = self.append('run', 'done', issue=1)
        event['version'] = 999
        self.writer.close()
        self.writer.path.write_text(json.dumps(event)+'\n')
        self.writer.file = self.writer.path.open('ab')
        value = dashboard.Reader(self.state).snapshot()
        self.assertEqual(value['quality'], 'unknown')
        self.assertEqual(value['runs'][0]['status'], 'unknown')
        (self.state / 'events' / ('run-'+str(uuid.uuid4())+'.jsonl')).symlink_to(self.state / 'worker.lock')
        (self.state / 'issue-5.json').symlink_to(self.writer.path)
        self.assertIn('unsafe-file', dashboard.Reader(self.state).snapshot()['problems'])

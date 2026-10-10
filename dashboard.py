#!/usr/bin/env python3
"""Independent, loopback-only read-only Gateway observer (standard library)."""

import argparse
import datetime as dt
import hashlib
import http.server
import itertools
import json
import os
import re
import stat
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from events import EventError, read_lines, utc_now

MAX_FILES = 64
MAX_EVENTS = 10000
MAX_RESPONSE = 2 * 1024 * 1024
FRESH_SECONDS = 60
RUN_FILE = re.compile(r'run-([0-9a-f-]{36})\.jsonl\Z')
STATE_FILE = re.compile(r'issue-([1-9][0-9]{0,15})\.json\Z')
PIPELINE = ('claim', 'worktree', 'bootstrap', 'context', 'debate', 'plan', 'plan_gate',
            'task', 'task_validation', 'task_gate', 'repair', 'task_commit',
            'full_validation', 'pr_prepare', 'pr_publish', 'ci', 'merge_gate', 'merge', 'issue_close')
ASSETS = {}  # Added by the Dashboard UI; never serve arbitrary checkout/state files.


def run_identity(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid run ID') from None
    return value


def age(timestamp, now):
    if timestamp is None:
        return None
    return max(0, int((now - dt.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))).total_seconds()))


def entries(directory, pattern):
    """Bound directory scanning, reject links and nonregular files."""
    found, problems = [], []
    if directory.is_symlink():
        return [], ['unsafe-directory']
    try:
        with os.scandir(directory) as listing:
            candidates = list(itertools.islice(listing, 1001))
        if len(candidates) > 1000:
            problems.append('directory-limit')
        for entry in candidates[:1000]:
            if not pattern.fullmatch(entry.name):
                continue
            if not entry.is_file(follow_symlinks=False):
                problems.append('unsafe-file')
                continue
            found.append((entry.stat(follow_symlinks=False).st_mtime_ns, Path(entry.path)))
    except FileNotFoundError:
        problems.append('history-unavailable')
    except OSError:
        problems.append('history-unreadable')
    found.sort(reverse=True)
    if len(found) > MAX_FILES:
        problems.append('file-limit')
    return [p for _, p in found[:MAX_FILES]], problems


class Reader:
    def __init__(self, state):
        self.state = Path(state).expanduser().resolve()

    def history(self):
        files, problems = entries(self.state / 'events', RUN_FILE)
        runs = {}
        remaining = MAX_EVENTS
        for path in files:
            if remaining <= 0:
                problems.append('event-limit')
                break
            identity = RUN_FILE.fullmatch(path.name).group(1)
            try:
                run_identity(identity)
                records, errors = read_lines(path, remaining)
            except (OSError, EventError, ValueError):
                problems.append('history-unreadable')
                continue
            remaining -= len(records)
            sequences, ids = {}, {}
            for event in records:
                if event['run_id'] != identity:
                    errors.append('run-identity-mismatch')
                    continue
                if event['event_id'] in ids:
                    if ids[event['event_id']] != event:
                        errors.append('conflicting-event-id')
                    continue
                ids[event['event_id']] = event
                if event['sequence'] in sequences:
                    errors.append('conflicting-sequence')
                    continue
                sequences[event['sequence']] = event
            ordered = [sequences[n] for n in sorted(sequences)]
            if ordered and [e['sequence'] for e in ordered] != list(range(1, len(ordered) + 1)):
                errors.append('sequence-gap')
            if len({e['repository'] for e in ordered}) > 1:
                errors.append('repository-mismatch')
            if len({e['issue'] for e in ordered if e['issue'] is not None}) > 1:
                errors.append('issue-mismatch')
            runs[identity] = (ordered, sorted(set(errors)))
            problems.extend(errors)
        return runs, sorted(set(problems))

    def legacy(self):
        files, problems = entries(self.state, STATE_FILE)
        items = []
        for path in files:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError()
                    raw = stream.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError()
                value = json.loads(raw)
                number = int(STATE_FILE.fullmatch(path.name).group(1))
                if not isinstance(value, dict) or type(value.get('issue')) is not int or value['issue'] != number:
                    raise ValueError()
                state = value.get('state')
                items.append({'issue': number, 'state': state if state in
                    ('running', 'approval-required', 'failed', 'done', 'pr-ready') else 'unknown',
                    'history': 'unavailable', 'stage': 'unknown', 'timestamp': None})
            except (OSError, ValueError, UnicodeError, RecursionError):
                problems.append('legacy-state-unreadable')
        return items, sorted(set(problems))

    def snapshot(self, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        history, problems = self.history()
        views = []
        all_events = []
        for identity, (records, errors) in history.items():
            all_events.extend(records)
            view = self.project(identity, records, errors, now)
            views.append(view)
        views.sort(key=lambda v: v['updated_at'] or '', reverse=True)
        all_events.sort(key=lambda e: (e['timestamp'], e['run_id'], e['sequence']))
        queue_records = [e for e in all_events if e['stage'] == 'queue' and 'queue_issues' in e['details']]
        latest_queue = queue_records[-1] if queue_records else None
        queue_age = age(latest_queue['timestamp'], now) if latest_queue else None
        queue = dict(issues=latest_queue['details'].get('queue_issues', []) if latest_queue else [],
            observed_count=latest_queue['details'].get('observed_count') if latest_queue else None,
            limit=latest_queue['details'].get('queue_limit') if latest_queue else None,
            total_known=False, timestamp=latest_queue['timestamp'] if latest_queue else None,
            age_seconds=queue_age, freshness='unknown' if queue_age is None else
            'stale' if queue_age > FRESH_SECONDS or problems else 'observed')
        worker_starts = [e for e in all_events if e['stage'] == 'worker' and e['outcome'] == 'started']
        worker = dict(status='unknown', timestamp=None, age_seconds=None, pid_is_liveness=False)
        if worker_starts:
            last_start = worker_starts[-1]
            records, errors = history[last_start['run_id']]
            workers = [e for e in records if e['stage'] == 'worker']
            last = workers[-1]
            worker_age = age(last['timestamp'], now)
            worker.update(run_id=last['run_id'], timestamp=last['timestamp'], age_seconds=worker_age,
                status='exited' if last['outcome'] == 'exited' and not errors else
                'stale' if worker_age > FRESH_SECONDS else 'unknown',
                last_result=next((e['outcome'] for e in reversed(workers) if e['outcome'] != 'exited'), 'unknown'))
        legacy, legacy_errors = self.legacy()
        observed_issues = {v['issue'] for v in views}
        legacy = [i for i in legacy if i['issue'] not in observed_issues]
        problems = sorted(set(problems + legacy_errors))
        digest = hashlib.sha256(json.dumps({'events': all_events, 'problems': problems,
                                          'legacy': legacy}, sort_keys=True).encode()).hexdigest()
        return dict(version=1, generated_at=now.isoformat().replace('+00:00', 'Z'),
            cursor=digest, quality='unknown' if problems else 'observed', problems=problems,
            queue=queue, worker=worker, runs=views, legacy=legacy)

    @staticmethod
    def project(identity, records, errors, now):
        stages = {stage: {'outcome': 'unknown', 'timestamp': None} for stage in PIPELINE}
        tasks, diagnostics, checks = {}, [], []
        view = dict(id=identity, repository=None, issue=None, status='unknown', mode=None,
            publish=None, started_at=None, updated_at=None, ended_at=None, current_stage='unknown',
            stages=stages, tasks=[], diagnostics=diagnostics, ci=checks,
            pr={'published': False, 'number': None, 'url': None, 'head': None,
                'base': None, 'merge_confirmed': False}, quality='unknown' if errors else 'observed',
            problems=errors)
        for event in records:
            stage, outcome, details = event['stage'], event['outcome'], event['details']
            view['repository'] = event['repository']
            if event['issue'] is not None:
                view['issue'] = event['issue']
            view['updated_at'] = event['timestamp']
            if stage == 'run' and outcome == 'started':
                view['started_at'] = event['timestamp']
                view['status'] = 'unknown'  # Recorded start is not proof of continued execution.
            if stage == 'run' and outcome in ('done', 'pr-ready', 'approval-required', 'failed'):
                view['status'] = outcome
                view['ended_at'] = event['timestamp']
            if details.get('mode'):
                view['mode'] = details['mode']
            if 'publish' in details:
                view['publish'] = details['publish']
            if stage in stages:
                stages[stage] = {'outcome': outcome, 'timestamp': event['timestamp']}
                if stage != 'claim':  # claim spans the entire processing call.
                    view['current_stage'] = stage
            task_id = event['task_id']
            if task_id:
                task = tasks.setdefault(task_id, dict(id=task_id, status='unknown', attempts=0,
                    repairs=0, criteria_count=None, criteria_text='withheld', stages={}, diagnostics=[]))
                task['attempts'] = max(task['attempts'], event['attempt'])
                if stage == 'repair' and details.get('phase') == 'TASK':
                    task['repairs'] = max(task['repairs'], details.get('repair_count', 0))
                if 'criteria_count' in details:
                    task['criteria_count'] = details['criteria_count']
                if stage == 'task':
                    task['status'] = outcome
                if stage in ('task_validation', 'task_gate', 'task_commit', 'repair'):
                    task['stages'][stage] = {'outcome': outcome, 'attempt': event['attempt']}
            if 'diagnostic_code' in details:
                diagnostic = {k: details[k] for k in ('diagnostic_code', 'summary')}
                diagnostic.update(stage=stage, outcome=outcome, task_id=task_id,
                                  attempt=event['attempt'], timestamp=event['timestamp'])
                diagnostics.append(diagnostic)
                if task_id:
                    tasks[task_id]['diagnostics'].append(diagnostic)
            if 'checks' in details:
                checks[:] = details['checks']
            for key in ('head', 'base'):
                if key in details:
                    view['pr'][key] = details[key]
            if stage == 'pr_publish' and outcome == 'PASS' and details.get('pr_number'):
                view['pr'].update(published=True, number=details['pr_number'],
                    url=f"https://github.com/{event['repository']}/pull/{details['pr_number']}")
            if stage == 'merge' and details.get('merged') is True and outcome == 'PASS':
                view['pr']['merge_confirmed'] = True
        # Missing optional stage is explicitly skipped only for a known mode.
        if view['mode'] in ('NORMAL', 'FAST') and stages['debate']['outcome'] == 'unknown':
            stages['debate'] = {'outcome': 'skipped', 'timestamp': None}
        if errors:
            view['status'] = 'unknown'
        view['tasks'] = list(tasks.values())[:12]
        view['diagnostics'] = diagnostics[-100:]
        for task in view['tasks']:
            task['diagnostics'] = task['diagnostics'][-20:]
        view['age_seconds'] = age(view['updated_at'], now)
        view['elapsed_seconds'] = age(view['started_at'], dt.datetime.fromisoformat(
            view['ended_at'].replace('Z', '+00:00')) if view['ended_at'] else now)
        view['freshness'] = ('unknown' if view['age_seconds'] is None or errors else
            'stale' if view['age_seconds'] > FRESH_SECONDS else 'observed')
        return view


class Server(http.server.HTTPServer):
    def __init__(self, state, port=8765):
        self.reader = Reader(state)
        super().__init__(('127.0.0.1', port), Handler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(5)
        return sock, address


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'GatewayObserver'
    sys_version = ''

    def log_message(self, *args):
        pass  # Do not log attacker-supplied paths/headers or private state.

    def send_error(self, code, message=None, explain=None):
        self.response(code, {'error': 'invalid-http'})

    def response(self, status, value, content_type='application/json; charset=utf-8'):
        raw = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode() if isinstance(value, dict) else value
        if len(raw) > MAX_RESPONSE:
            status, raw = 503, b'{"error":"response-limit"}'
            content_type = 'application/json; charset=utf-8'
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def boundary(self):
        authority = '127.0.0.1:' + str(self.server.server_port)
        hosts, origins = self.headers.get_all('Host', []), self.headers.get_all('Origin', [])
        fetch = self.headers.get_all('Sec-Fetch-Site', [])
        return (hosts == [authority] and (not origins or origins == ['http://' + authority])
                and (not fetch or fetch in (['same-origin'], ['none']))
                and self.client_address[0] == '127.0.0.1')

    def do_GET(self):
        if not self.boundary():
            return self.response(403, {'error': 'request-boundary'})
        if len(self.path) > 2048 or '\\' in self.path:
            return self.response(400, {'error': 'invalid-request'})
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise ValueError()
            args = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=5)
            if any(len(v) != 1 for v in args.values()):
                raise ValueError()
            if parsed.path in ASSETS:
                if args:
                    raise ValueError()
                filename, kind = ASSETS[parsed.path]
                raw = (Path(__file__).parent / 'dashboard_static' / filename).read_bytes()
                return self.response(200, raw, kind)
            if parsed.path == '/api/snapshot':
                if set(args) - {'cursor'}:
                    raise ValueError()
                cursor = args.get('cursor', [''])[0]
                if cursor and not re.fullmatch('[0-9a-f]{64}', cursor):
                    raise ValueError()
                value = self.server.reader.snapshot()
                value['changed'] = value['cursor'] != cursor
                return self.response(200, value)
            if parsed.path in ('/api/events', '/api/runs'):
                allowed = {'run', 'after', 'limit'} if parsed.path == '/api/events' else {'after', 'limit'}
                if set(args) - allowed:
                    raise ValueError()
                after = args.get('after', ['0'])[0]
                limit = args.get('limit', ['100'])[0]
                if not re.fullmatch('[0-9]{1,9}', after) or not re.fullmatch('[0-9]{1,3}', limit):
                    raise ValueError()
                after, limit = int(after), int(limit)
                if not 1 <= limit <= 100:
                    raise ValueError()
                if parsed.path == '/api/runs':
                    value = self.server.reader.snapshot()
                    items = value['runs'][after:after + limit]
                    return self.response(200, {'runs': items, 'next_cursor': after + len(items), 'quality': value['quality']})
                identity = run_identity(args.get('run', [''])[0])
                history, problems = self.server.reader.history()
                if identity not in history:
                    return self.response(404, {'error': 'run-unavailable'})
                records, errors = history[identity]
                selected = [e for e in records if e['sequence'] > after][:limit]
                return self.response(200, {'events': selected,
                    'next_cursor': selected[-1]['sequence'] if selected else after,
                    'problems': sorted(set(problems + errors)), 'quality': 'unknown' if problems or errors else 'observed'})
            if parsed.path.startswith('/api/runs/'):
                if args:
                    raise ValueError()
                identity = run_identity(parsed.path[len('/api/runs/'):])
                history, problems = self.server.reader.history()
                if identity not in history:
                    return self.response(404, {'error': 'run-unavailable'})
                records, errors = history[identity]
                return self.response(200, {'run': Reader.project(identity, records, errors, dt.datetime.now(dt.timezone.utc)), 'problems': problems})
            return self.response(404, {'error': 'not-found'})
        except ValueError:
            return self.response(400, {'error': 'invalid-request'})
        except (OSError, EventError):
            return self.response(503, {'error': 'observer-unavailable'})

    def reject_method(self):
        self.response(405, {'error': 'read-only'})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = reject_method


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True, help='Trusted existing Gateway state directory')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be 1..65535')
    with Server(args.state, args.port) as server:
        print(f'Read-only Gateway Dashboard: http://127.0.0.1:{args.port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()

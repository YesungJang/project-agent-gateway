"""Private, append-only observability contract. No command or agent payloads."""

import datetime as dt
import fcntl
import json
import os
import re
import stat
import uuid
from pathlib import Path

VERSION = 1
MAX_LINE = 16384
STAGES = frozenset(('worker', 'queue', 'claim', 'worktree', 'bootstrap', 'context',
                    'debate', 'plan', 'plan_gate', 'task', 'task_validation',
                    'task_gate', 'repair', 'task_commit', 'full_validation',
                    'pr_prepare', 'pr_publish', 'ci', 'merge_gate', 'merge',
                    'issue_close', 'state', 'run'))
OUTCOMES = frozenset(('started', 'PASS', 'FAIL', 'ERROR', 'RISK', 'pending',
                      'skipped', 'approval-required', 'pr-ready', 'done',
                      'running', 'idle', 'exited', 'lock-contended', 'unknown'))
DIAGNOSTICS = {
    'OBSERVATION_FAILED': 'Event recording failed; history may be incomplete.',
    'BOOTSTRAP_FAILED': 'Environment preparation failed; operator review required.',
    'VERIFIER_ERROR': 'Independent verification failed; operator review required.',
    'VERIFIER_RISK': 'Independent verifier detected risk; operator review required.',
    'GATE_FAILED': 'Gate or validation failed; inspect private evidence locally.',
    'APPROVAL_REQUIRED': 'Execution stopped for operator review.',
    'TIMEOUT': 'An operation timed out; operator review required.',
    'CRASH': 'Worker exited with an error; inspect private evidence locally.',
}
_TOKEN = re.compile(r'[A-Za-z0-9_.:/%+@ -]{1,256}\Z')
_REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
_SHA = re.compile(r'[0-9a-f]{40}\Z')


class EventError(RuntimeError):
    """Observation failure only; must never cause external-operation retries."""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def _number(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**53 - 1


def safe_details(details):
    """Drop unknown fields at BOTH write and HTTP projection boundaries."""
    result = {}
    for key in ('criteria_count', 'task_count', 'repair_count', 'observed_count',
                'queue_limit', 'pid', 'pr_number', 'issue_number'):
        if _number(details.get(key)):
            result[key] = details[key]
    for key in ('publish', 'merged', 'queue_total_known'):
        if type(details.get(key)) is bool:
            result[key] = details[key]
    if details.get('mode') in ('NORMAL', 'FAST', 'DEBATE'):
        result['mode'] = details['mode']
    if details.get('phase') in ('PLAN', 'TASK'):
        result['phase'] = details['phase']
    for key in ('head', 'base'):
        if isinstance(details.get(key), str) and _SHA.fullmatch(details[key]):
            result[key] = details[key]
    code = details.get('diagnostic_code')
    if isinstance(code, str) and code in DIAGNOSTICS:
        result['diagnostic_code'] = code
        result['summary'] = DIAGNOSTICS[code]
    if isinstance(details.get('private_log_id'), str):
        try:
            result['private_log_id'] = str(uuid.UUID(details['private_log_id']))
        except ValueError:
            pass
    numbers = details.get('queue_issues')
    if isinstance(numbers, list):
        result['queue_issues'] = [n for n in numbers[:100] if _number(n, 1)]
    # Exact public CI identities are the ONLY free-form metadata permitted.
    checks = details.get('checks')
    if isinstance(checks, list):
        result['checks'] = []
        for check in checks[:100]:
            if not isinstance(check, dict):
                continue
            identity = check.get('identifier')
            outcome = check.get('outcome')
            if (isinstance(identity, str) and _TOKEN.fullmatch(identity)
                    and identity.startswith(('check-run:', 'status-context:'))
                    and outcome in ('PASS', 'FAIL', 'pending', 'unknown')):
                result['checks'].append({'identifier': identity, 'outcome': outcome})
    return result


def public_event(value):
    """Validate known v1 structure without forwarding unknown content."""
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
        raise EventError('Unsupported event version')
    for key in ('event_id', 'run_id'):
        try:
            if str(uuid.UUID(value[key])) != value[key]:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise EventError('Invalid event identity') from None
    repository = value.get('repository')
    if not isinstance(repository, str) or not _REPO.fullmatch(repository):
        raise EventError('Invalid repository')
    if (not _number(value.get('sequence'), 1)
            or not isinstance(value.get('stage'), str) or value['stage'] not in STAGES
            or not isinstance(value.get('outcome'), str) or value['outcome'] not in OUTCOMES
            or not _number(value.get('attempt'))):
        raise EventError('Invalid event contract')
    issue, task = value.get('issue'), value.get('task_id')
    if issue is not None and not _number(issue, 1):
        raise EventError('Invalid issue')
    if task is not None and (not isinstance(task, str) or not re.fullmatch(r'task-[1-9][0-9]{0,2}', task)):
        raise EventError('Invalid task identity')
    timestamp = value.get('timestamp')
    try:
        if not isinstance(timestamp, str) or not timestamp.endswith('Z') or len(timestamp) > 32:
            raise ValueError()
        dt.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError:
        raise EventError('Invalid UTC timestamp') from None
    result = {key: value[key] for key in ('version', 'event_id', 'run_id', 'repository',
              'sequence', 'timestamp', 'stage', 'attempt', 'outcome')}
    result.update(issue=issue, task_id=task, project=repository)
    result['details'] = safe_details(value.get('details') if isinstance(value.get('details'), dict) else {})
    return result


class EventWriter:
    """One new run/file per once invocation, while holding worker.lock."""

    def __init__(self, state, repository, lock_fd):
        self.state = Path(state)
        self.repository = repository
        self.lock_fd = lock_fd
        expected = (self.state / 'worker.lock').stat()
        actual = os.fstat(lock_fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise EventError('Worker lock required')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.run_id = str(uuid.uuid4())
        self.sequence = 0
        directory = self.state / 'events'
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise EventError('Unsafe event directory')
        directory.chmod(0o700)
        self.path = directory / ('run-' + self.run_id + '.jsonl')
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, 'wb', buffering=0)
        # Persist new directory entry as well as every completed line.
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        except OSError:
            self.file.close()
            raise
        finally:
            os.close(directory_fd)

    def append(self, stage, outcome, issue=None, task_id=None, attempt=0, **details):
        os.fstat(self.lock_fd)  # A closed worker lock must not keep recording.
        event = public_event(dict(version=VERSION, event_id=str(uuid.uuid4()),
            run_id=self.run_id, repository=self.repository, sequence=self.sequence + 1,
            timestamp=utc_now(), stage=stage, task_id=task_id, attempt=attempt,
            issue=issue, outcome=outcome, details=details))
        raw = (json.dumps(event, separators=(',', ':'), ensure_ascii=True) + '\n').encode()
        if len(raw) > MAX_LINE:
            raise EventError('Event too large')
        # No rewrite, truncation or retry after a partial append.
        if self.file.write(raw) != len(raw):
            raise EventError('Incomplete event write')
        self.file.flush()
        os.fsync(self.file.fileno())
        self.sequence += 1
        return event

    def close(self):
        self.file.close()


def read_lines(path):
    """Read complete bounded lines; readers never repair/truncate the file."""
    events, problems = [], []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise EventError('Not a regular event file')
        for _ in range(10000):
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                problems.append('oversize-line')
                break
            if not line.endswith(b'\n'):
                problems.append('incomplete-tail')
                break
            try:
                events.append(public_event(json.loads(line)))
            except (ValueError, UnicodeError, EventError):
                problems.append('invalid-or-unsupported-event')
        else:
            if stream.read(1):
                problems.append('event-limit')
    return events, sorted(set(problems))

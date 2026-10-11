import fcntl
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from events import EventWriter, EventError, read_lines


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.lock = (self.state / 'worker.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def tearDown(self):
        self.lock.close()
        self.temp.cleanup()

    def writer(self):
        return EventWriter(self.state, 'owner/repo', self.lock.fileno())

    def test_append_restart_permissions_and_redaction(self):
        w = self.writer()
        try:
            w.append('worker', 'started', prompt='SECRET', stdout='SECRET', config={'token': 'SECRET'})
            first = w.path.read_bytes()
            w.append('task_gate', 'PASS', task_id='task-1', attempt=1, diagnostic_code='GATE_FAILED', summary='SECRET')
            self.assertTrue(w.path.read_bytes().startswith(first))
            self.assertNotIn(b'SECRET', w.path.read_bytes())
            records, errors = read_lines(w.path)
            self.assertEqual([e['sequence'] for e in records], [1, 2])
            self.assertEqual(errors, [])
            self.assertEqual(stat.S_IMODE(w.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(w.path.parent.stat().st_mode), 0o700)
            old_id = w.run_id
        finally:
            w.close()
        w2 = self.writer()
        try:
            self.assertNotEqual(w2.run_id, old_id)
            self.assertEqual(w2.append('worker', 'started')['sequence'], 1)
        finally:
            w2.close()

    def test_tail_unknown_fields_and_unknown_version(self):
        w = self.writer()
        e = w.append('worker', 'started')
        w.close()
        e['secret'] = 'SECRET'
        with w.path.open('ab') as f:
            f.write((json.dumps(e) + '\n').encode())
            e['version'] = 99
            f.write((json.dumps(e) + '\n').encode())
            f.write(b'{"secret":')
        before = w.path.read_bytes()
        records, errors = read_lines(w.path)
        self.assertEqual(len(records), 2)
        self.assertNotIn('secret', records[-1])
        self.assertEqual(errors, ['incomplete-tail', 'invalid-or-unsupported-event'])
        self.assertEqual(before, w.path.read_bytes())

    def test_disk_failure_is_explicit_without_rewrite(self):
        w = self.writer()
        try:
            first = w.append('worker', 'started')
            with patch('events.os.fsync', side_effect=OSError('PRIVATE PATH SECRET')):
                with self.assertRaises(OSError):
                    w.append('run', 'done')
            self.assertEqual(w.sequence, first['sequence'])
            self.assertEqual(len(read_lines(w.path)[0]), 2)
        finally:
            w.close()

    def test_symlink_and_nonworker_lock_rejected(self):
        other = (self.state / 'other').open('a')
        try:
            with self.assertRaises(EventError):
                EventWriter(self.state, 'owner/repo', other.fileno())
        finally:
            other.close()
        (self.state / 'events').symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(EventError):
            self.writer()

    def test_invalid_contract_and_opaque_reference(self):
        w = self.writer()
        try:
            with self.assertRaises(EventError):
                w.append('shell', 'PASS')
            e = w.append('worker', 'started', private_log_id='/private/log', pid=True)
            self.assertEqual(e['details'], {})
        finally:
            w.close()

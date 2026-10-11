import json
import tempfile
import threading
import unittest
import http.client
from pathlib import Path

import dashboard
import demo_dashboard


class DashboardUITests(unittest.TestCase):
    def test_synthetic_scenarios_refuse_existing_and_in_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'demo'
            identities = demo_dashboard.generate(output)
            self.assertEqual(len(identities), 12)
            before = {p.name: p.read_bytes() for p in (output / 'events').iterdir()}
            with self.assertRaises(FileExistsError):
                demo_dashboard.generate(output)
            self.assertEqual(before, {p.name: p.read_bytes() for p in (output / 'events').iterdir()})
            with self.assertRaises(ValueError):
                demo_dashboard.generate(Path(dashboard.__file__).parent / 'demo-state')

    def test_scenario_projection_covers_stops_repairs_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'demo'
            identities = demo_dashboard.generate(output)
            snapshot = dashboard.Reader(output).snapshot()
            runs = {v['id']: v for v in snapshot['runs']}
            self.assertEqual(runs[identities['completed']]['status'], 'done')
            self.assertEqual(runs[identities['repair']]['tasks'][0]['repairs'], 1)
            for scenario in ('repair-limit', 'risk', 'verifier-error', 'bootstrap-failed', 'ci-pending', 'ci-failed'):
                self.assertEqual(runs[identities[scenario]]['status'], 'approval-required')
            self.assertEqual(runs[identities['risk']]['tasks'][0]['stages']['task_gate']['outcome'], 'RISK')
            self.assertEqual(runs[identities['verifier-error']]['tasks'][0]['stages']['task_gate']['outcome'], 'ERROR')
            self.assertEqual(runs[identities['crash']]['status'], 'unknown')
            self.assertEqual(runs[identities['cut-tail']]['status'], 'unknown')
            self.assertIn('incomplete-tail', snapshot['problems'])
            self.assertEqual(runs[identities['FAST']]['stages']['task_gate']['outcome'], 'skipped')
            self.assertFalse(runs[identities['FAST']]['pr']['merge_confirmed'])
            self.assertEqual(runs[identities['DEBATE']]['stages']['debate']['outcome'], 'PASS')

    def test_only_fixed_static_manifest_is_served(self):
        with tempfile.TemporaryDirectory() as tmp, dashboard.Server(tmp, 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for route, (_, mime) in dashboard.ASSETS.items():
                    c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    c.request('GET', route)
                    r = c.getresponse()
                    body = r.read()
                    self.assertEqual(r.status, 200)
                    self.assertEqual(r.getheader('Content-Type'), mime)
                    self.assertIn("frame-ancestors 'none'", r.getheader('Content-Security-Policy'))
                    self.assertNotIn('config.example.json', body.decode())
                    c.close()
            finally:
                server.shutdown()
                thread.join(timeout=3)

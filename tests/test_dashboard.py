"""Checks the dashboard's local-only read API and empty-state behavior."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from autarky_trade import community, dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'research.sqlite'
        community.init(self.database)

    def test_empty_snapshot_has_agents_no_ticks_or_jobs(self):
        result = dashboard.snapshot(self.database)
        self.assertEqual(len(result['account']['agents']), 3)
        self.assertEqual(result['ticks'], [])
        self.assertEqual(result['jobs'], [])
        self.assertTrue(result['events'])

    def test_local_server_serves_page_and_json_without_mutating_state(self):
        before = dashboard.snapshot(self.database)
        server = dashboard.make_server(self.database, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(server.server_address[0], '127.0.0.1')
            base = f'http://127.0.0.1:{server.server_port}'
            with urlopen(base + '/') as response:
                page = response.read().decode('utf-8')
            self.assertIn('Research monitor', page)
            with urlopen(base + '/api/overview') as response:
                result = json.load(response)
            self.assertEqual(result['account']['equity'], 1000)
            self.assertEqual(result['jobs'], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(before, dashboard.snapshot(self.database))


if __name__ == '__main__':
    unittest.main()

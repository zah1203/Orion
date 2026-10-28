import json
import sqlite3
import unittest
from datetime import timedelta
from orion.core import stamp
from orion.portal.trade_chart import chart


class TradeChartTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE source_events(id TEXT PRIMARY KEY,received_at TEXT,body TEXT)')
        self.now = stamp('2026-10-28T10:00:00Z')
        self.position = dict(status='OPEN', source_time='2026-10-20T10:00:00Z',
                             contract=dict(segment='mcx_fo', token='42'))

    def tearDown(self):
        self.db.close()

    def quote(self, id, seconds, bid='100', **overrides):
        at = (self.now - timedelta(seconds=seconds)).isoformat()
        body = dict(type='quote', segment='mcx_fo', token='42', source_time=at,
                    bid=bid, ask=str(float(bid)+1), ltp=bid, market_open=True)
        body.update(overrides)
        self.db.execute('INSERT INTO source_events VALUES(?,?,?)', (str(id), at, json.dumps(body)))

    def get(self, window='1D', events=None):
        return chart(self.db, self.position, events or [], window, self.now)

    def test_exact_contract_and_gap_and_invalid_quotes(self):
        self.quote(1, 300)
        self.quote(2, 290, '105')
        self.quote(3, 20, '102')
        self.quote(4, 10, token='99')
        self.quote(5, 9, segment='nse_fo')
        self.quote(6, 8, ask='1')
        self.quote(7, 7, market_open=False)
        self.quote(8, 6, source_time='2026-10-20T10:00:00Z')
        c = self.get()
        self.assertEqual(c['observations'], 3)
        self.assertEqual([len(s) for s in c['segments']], [2, 1])
        self.assertEqual(c['segments'][0][-1]['bid'], '105')

    def test_ranges_and_tracking_boundary(self):
        self.quote(1, 60)
        self.quote(2, 2*86400)
        self.quote(3, 9*86400)
        self.assertEqual(self.get()['observations'], 1)
        self.assertEqual(self.get('1W')['observations'], 2)
        self.assertEqual(self.get('1M')['observations'], 2)
        with self.assertRaises(ValueError):
            self.get('bad')

    def test_closed_window_execution_markers_and_missing_quotes(self):
        self.position['status'] = 'CLOSED'
        self.quote(1, 100)
        self.quote(2, 1)
        events = [dict(at=(self.now-timedelta(seconds=50)).isoformat(), event='PAPER_EXIT', body=dict(price='98', reason='STOP'))]
        c = self.get(events=events)
        self.assertEqual(c['observations'], 1)
        self.assertEqual(c['markers'][0]['label'], 'Exit: STOP')
        self.db.execute('DROP TABLE source_events')
        self.assertEqual(self.get(events=events)['segments'], [])

    def test_sampling_preserves_extremes_and_bounds_rows(self):
        for i in range(8002):
            self.quote(i, i, '150' if i == 42 else '50' if i == 44 else '100')
        c = self.get()
        self.assertTrue(c['truncated'])
        self.assertEqual(c['observations'], 8000)
        prices = [float(p['bid']) for seg in c['segments'] for p in seg]
        self.assertEqual((min(prices), max(prices)), (50, 150))
        self.assertLess(len(prices), 750)

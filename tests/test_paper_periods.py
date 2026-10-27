import copy
import json
import sqlite3
import unittest
from datetime import timedelta

from orion.core import stamp
import test_portal


class PaperPeriodTests(unittest.TestCase):
    setUp = test_portal.PortalTests.setUp
    tearDown = test_portal.PortalTests.tearDown

    def post(self, body=None):
        return self.client.post('/api/paper-period', headers=self.headers, json=body or dict(
            amount=400000, label='Post-fix test', expected_period='legacy', confirmation='START NEW PERIOD'))

    def trade(self, new=False):
        self.accounts.set_enabled(self.alice, True)
        events = copy.deepcopy(self.events)
        if new:
            for event in events:
                event['event_id'] += '-new'
                event['source_time'] = (stamp(event['source_time']) + timedelta(seconds=10)).isoformat()
            events[0]['message_id'] = '2'
            events[0]['text'] = events[0]['text'].replace('BUY 170', 'BUY 171')
            events[2].update(bid='171', ask='171.5', ltp='171')
        else:
            # The old period has an actual stopped-out loss.
            events = events[:4]
            events[-1].update(bid='150', ask='150.5', ltp='150')
        for event in events:
            self.accounts.process(self.alice, event, self.master, stamp(event['source_time']), True)
        self.accounts.set_enabled(self.alice, False)

    def db(self):
        return sqlite3.connect(self.store.account_dir(self.alice) / 'paper.db')

    def test_reset_preserves_ledger_and_comparison_but_zeros_current_totals(self):
        self.trade()
        old = self.accounts.summary(self.alice)
        old_perf = self.accounts.performance(self.alice, period='all')
        self.assertLess(float(old_perf['realized']), 0)
        sid = next(iter(old['state']['positions']))
        before_detail = self.accounts.trade_details(self.alice, sid)
        with self.db() as db:
            audit = db.execute('SELECT * FROM audit').fetchall()
            seen = db.execute('SELECT * FROM seen').fetchall()
            raw = json.loads(db.execute('SELECT body FROM state').fetchone()[0])
            # Source evidence stays in the same database.
            db.execute('CREATE TABLE source_events (id TEXT PRIMARY KEY,received_at TEXT,body TEXT)')
            db.execute('INSERT INTO source_events VALUES(?,?,?)', ('original', self.events[0]['source_time'], json.dumps(self.events[0])))
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        summary = self.accounts.summary(self.alice)
        self.assertEqual(float(summary['state']['cash']), 400000)
        self.assertEqual(summary['state']['days'], {})
        self.assertEqual(summary['state']['positions'], {})
        self.assertEqual(float(summary['pnl']['totals']['realized']), 0)
        self.assertEqual(summary['settings'], old['settings'])
        self.assertFalse(summary['enabled'])
        current = self.accounts.performance(self.alice, period='all')
        self.assertEqual(float(current['realized']), 0)
        self.assertEqual(current['entries'], 0)
        archived = self.accounts.performance(self.alice, period='all', paper_period='legacy')
        self.assertTrue(archived['archived'])
        for key in ('realized', 'entries', 'trades', 'daily'):
            self.assertEqual(archived[key], old_perf[key])
        detail = self.accounts.trade_details(self.alice, sid)
        self.assertEqual(detail['realized_pnl'], before_detail['realized_pnl'])
        self.assertTrue(any(e['kind'] == 'telegram' for e in detail['timeline']))
        with self.db() as db:
            self.assertEqual(db.execute('SELECT * FROM audit ORDER BY seq LIMIT ?', (len(audit),)).fetchall(), audit)
            self.assertEqual(db.execute('SELECT * FROM seen').fetchall(), seen)
            after = json.loads(db.execute('SELECT body FROM state').fetchone()[0])
            self.assertEqual(after['positions'], raw['positions'])
            self.assertEqual(after['fingerprints'], raw['fingerprints'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_events').fetchone()[0], 1)
        capital = self.accounts.capital(self.alice)
        self.assertEqual(capital['status'], 'consistent', capital)
        self.assertEqual(float(capital['starting_cash']), 400000)
        # Network retries / a stale tab cannot reset again.
        self.assertEqual(self.post().status_code, 409)

    def test_new_trade_same_day_is_separate_and_dedup_survives(self):
        cfg = self.store.user(self.alice)['settings']
        cfg.update(max_entries_per_day=1, daily_loss_limit='1')
        self.accounts.save_settings(self.alice, cfg)
        self.trade()
        self.assertEqual(self.post().status_code, 200)
        self.accounts.set_enabled(self.alice, True)
        original = copy.deepcopy(self.events[0])
        self.assertEqual(self.accounts.process(self.alice, original, self.master, stamp(original['source_time']), True), [])
        original['event_id'] = 'duplicate-new-envelope'
        result = self.accounts.process(self.alice, original, self.master, stamp(original['source_time']), True)
        self.assertEqual(result[0]['event'], 'DUPLICATE_MESSAGE')
        self.trade(new=True)
        current = self.accounts.performance(self.alice, period='all')
        self.assertEqual(current['entries'], 1)
        self.assertEqual(len(current['trades']), 1)
        self.assertTrue(current['trades'][0]['signal_id'].endswith(':2'))
        self.assertGreater(float(current['realized']), 0)
        capital = self.accounts.capital(self.alice)
        self.assertEqual(capital['status'], 'consistent', capital)
        self.accounts.adjust_balance(self.alice, '410000', 'extra test cash')
        self.assertEqual(self.accounts.capital(self.alice)['status'], 'consistent')
        first_id = current['paper_period']['id']
        response = self.post(dict(amount=500000, label='Third period', expected_period=first_id,
                                  confirmation='START NEW PERIOD'))
        self.assertEqual(response.status_code, 200, response.text)
        archived = self.accounts.performance(self.alice, paper_period=first_id, period='all')
        self.assertEqual(archived['realized'], current['realized'])
        self.assertEqual(archived['entries'], 1)
        self.assertEqual(len(archived['paper_periods']), 3)
        self.assertEqual(self.accounts.capital(self.alice)['status'], 'consistent')

    def test_enabled_pending_and_open_guards(self):
        self.accounts.set_enabled(self.alice, True)
        self.assertEqual(self.post().status_code, 409)
        self.accounts.process(self.alice, self.events[0], self.master, stamp(self.events[0]['source_time']), True)
        # Directly pause flag, retaining PENDING, to exercise the backend guard.
        self.store.set_enabled(self.alice, False)
        self.assertEqual(self.post().status_code, 409)
        self.accounts.set_enabled(self.alice, True)
        for event in self.events[1:3]:
            self.accounts.process(self.alice, event, self.master, stamp(event['source_time']), True)
        self.accounts.set_enabled(self.alice, False)
        self.assertEqual(self.post().status_code, 409)
        with self.db() as db:
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='paper_periods'").fetchone())

    def test_auth_validation_and_account_isolation(self):
        body = dict(amount=400000, label='test', expected_period='legacy', confirmation='START NEW PERIOD')
        self.assertEqual(self.client.post('/api/paper-period', json=body, headers={'Origin': test_portal.ORIGIN}).status_code, 403)
        for changes in ({'confirmation':'yes'}, {'amount':0}, {'amount':-1}, {'amount':100000001}, {'user_id':self.bob}):
            self.assertEqual(self.post({**body, **changes}).status_code, 422)
        for changes in ({'label':'   '}, {'amount':1.001}, {'expected_period':'unknown'}):
            self.assertEqual(self.post({**body, **changes}).status_code, 409)
        self.assertEqual(self.post(body).status_code, 200)
        self.assertFalse((self.store.account_dir(self.bob)/'paper.db').exists())
        self.assertEqual(self.client.get('/api/performance?paper_period=unknown').status_code, 422)
        self.assertEqual(self.client.get('/api/admin/users/'+self.bob+'/performance?paper_period=legacy').status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.post(body).status_code, 401)

    def test_archive_and_state_write_roll_back_together(self):
        self.trade()
        with self.db() as db:
            before = db.execute('SELECT body FROM state').fetchone()[0]
            db.execute("CREATE TRIGGER reject_reset BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.accounts.start_paper_period(self.alice, 400000, 'test', 'legacy')
        with self.db() as db:
            self.assertEqual(db.execute('SELECT body FROM state').fetchone()[0], before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE event='PAPER_PERIOD_STARTED'").fetchone()[0], 0)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='paper_periods'").fetchone())

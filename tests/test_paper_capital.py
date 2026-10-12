import json
import sqlite3
import unittest

from orion.core import stamp
from orion.portal.capital import reconcile
import test_portal


class CapitalTests(unittest.TestCase):
    setUp = test_portal.PortalTests.setUp
    tearDown = test_portal.PortalTests.tearDown

    def trade(self):
        self.accounts.set_enabled(self.alice, True)
        for event in self.events:
            self.accounts.process(self.alice, event, self.master, stamp(event['source_time']), True)
        self.accounts.set_enabled(self.alice, False)

    def test_full_replay_adjustment_explains_profit_without_reset(self):
        self.trade()
        prior = self.accounts.summary(self.alice)['state']
        before = self.accounts.capital(self.alice)
        self.assertEqual(before['status'], 'consistent', before)
        self.accounts.adjust_balance(self.alice, '300000', 'test')
        path = self.store.account_dir(self.alice) / 'paper.db'
        with sqlite3.connect(path) as db:
            original = list(db.iterdump())
        result = self.client.get('/api/paper-capital')
        self.assertEqual(result.status_code, 200, result.text)
        result = result.json()
        self.assertEqual(result['status'], 'consistent', result)
        self.assertEqual(result['actual_cash'], '300000')
        self.assertEqual(float(result['net_contributed_capital']) + float(result['realized_pnl']), 300000)
        self.assertEqual(float(result['unexplained_difference']), 0)
        self.assertTrue(result['audit_complete'])
        self.assertFalse(result['order_submission_available'])
        self.assertEqual(self.accounts.summary(self.alice)['state']['positions'], prior['positions'])
        with sqlite3.connect(path) as db:
            self.assertEqual(list(db.iterdump()), original)

    def test_missing_ledger_does_not_create_one_and_owner_isolation(self):
        self.assertEqual(self.client.get('/api/paper-capital').json()['status'], 'not-started')
        self.assertFalse((self.store.account_dir(self.alice)/'paper.db').exists())
        url = '/api/admin/users/' + self.bob + '/paper-capital'
        self.assertEqual(self.client.get(url).status_code, 403)
        self.store.bootstrap_owner('alice')
        self.assertEqual(self.client.get(url).json()['status'], 'not-started')
        self.assertEqual(self.client.get('/api/admin/users/'+'0'*32+'/paper-capital').status_code,404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/paper-capital').status_code,401)

    def test_open_position_cost_and_drift_are_not_hidden(self):
        self.accounts.set_enabled(self.alice, True)
        for event in self.events[:3]:
            self.accounts.process(self.alice, event, self.master, stamp(event['source_time']), True)
        result = self.accounts.capital(self.alice)
        self.assertEqual(result['status'], 'consistent', result)
        self.assertGreater(float(result['open_cost_basis']), 0)
        path = self.store.account_dir(self.alice)/'paper.db'
        with sqlite3.connect(path) as db:
            state = json.loads(db.execute('SELECT body FROM state').fetchone()[0])
            state['cash'] = str(float(state['cash']) + 123)
            db.execute('UPDATE state SET body=?', (json.dumps(state),))
        result = self.accounts.capital(self.alice)
        self.assertEqual(result['status'], 'review-required')
        self.assertEqual(float(result['unexplained_difference']),123)
        self.assertIn('audit-cash-mismatch', result['issues'])

    def test_full_adjustment_history_not_latest_100_events(self):
        self.trade()
        self.accounts.adjust_balance(self.alice,'300000','first')
        self.accounts.adjust_balance(self.alice,'290000','withdrawal')
        path = self.store.account_dir(self.alice)/'paper.db'
        with sqlite3.connect(path) as db:
            db.executemany('INSERT INTO audit(at,event,body) VALUES(?,?,?)',
                           [('2026-10-12T00:00:00Z','IGNORED_CHANNEL','{}')]*150)
        result = self.accounts.capital(self.alice)
        self.assertEqual(result['status'],'consistent',result)
        self.assertEqual(len(result['adjustments']),2)
        self.assertEqual(result['adjustments'][1]['adjustment'],'-10000')

    def test_committed_wal_read_does_not_acquire_worker_lease_or_write(self):
        import fcntl
        self.trade()
        path = self.store.account_dir(self.alice)/'paper.db'
        with open(path.parent/'worker.lock','a') as lease, sqlite3.connect(path) as writer:
            fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            state = json.loads(writer.execute('SELECT body FROM state').fetchone()[0])
            before = state['cash']
            from decimal import Decimal
            state['cash'] = str(Decimal(before)+Decimal('100'))
            writer.execute('UPDATE state SET body=?',(json.dumps(state),))
            writer.execute('INSERT INTO audit(at,event,body) VALUES(?,?,?)',
                ('2026-10-12T00:00:00Z','PAPER_BALANCE_ADJUSTED',json.dumps(
                    dict(before=before,after=state['cash'],adjustment='100'))))
            writer.commit()
            self.assertTrue(path.with_name('paper.db-wal').exists())
            version = writer.execute('PRAGMA data_version').fetchone()
            result = self.accounts.capital(self.alice)
            self.assertEqual(result['status'],'consistent',result)
            self.assertEqual(result['actual_cash'],state['cash'])
            self.assertEqual(writer.execute('PRAGMA data_version').fetchone(),version)

    def test_corrupt_or_deleted_audit_is_not_certified(self):
        self.trade()
        path = self.store.account_dir(self.alice)/'paper.db'
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM audit WHERE event='PAPER_BUY'")
        self.assertEqual(self.accounts.capital(self.alice)['status'],'review-required')
        with sqlite3.connect(path) as db:
            db.execute("UPDATE state SET body='private malformed data'")
        response = self.client.get('/api/paper-capital')
        self.assertEqual(response.status_code,409)
        self.assertNotIn('private malformed',response.text)


class CapitalArithmeticTests(unittest.TestCase):
    def test_two_trades_straddling_cash_change_keep_historical_profit(self):
        def position(pnl):
            return dict(lots=1, remaining=0, entry_fill='100', pnl=pnl,
                        status='CLOSED', contract=dict(premium_multiplier='1'))
        state = dict(cash='319407.50', positions={'first':position('38292.50'),
                     'second':position('19407.50')}, days={'one':dict(pnl='38292.50'),'two':dict(pnl='19407.50')})
        events = [
            ('PAPER_BUY',dict(signal_id='first',lots=1,price='100',pnl='0',fee='0')),
            ('PAPER_EXIT',dict(signal_id='first',price='38392.50',pnl='38292.50',fee='0')),
            ('PAPER_BALANCE_ADJUSTED',dict(before='238292.50',after='300000',adjustment='61707.50')),
            ('PAPER_BUY',dict(signal_id='second',lots=1,price='100',pnl='0',fee='0')),
            ('PAPER_EXIT',dict(signal_id='second',price='19507.50',pnl='19407.50',fee='0')),
        ]
        records = [dict(seq=i,at='2026-10-12T00:00:00Z',event=e,body=b) for i,(e,b) in enumerate(events)]
        result = reconcile(state,records,'200000')
        self.assertEqual(result['status'],'consistent',result)
        self.assertEqual(result['realized_pnl'],'57700.00')
        self.assertEqual(result['net_contributed_capital'],'261707.50')
        self.assertEqual(result['unexplained_difference'],'0.00')
        # Altering a fee checkpoint cannot be hidden by an otherwise right cash equation.
        records[-1]['body']['fee']='10'
        result = reconcile(state,records,'200000')
        self.assertIn('audit-fee-mismatch',result['issues'])

    def test_adjusting_total_cash_is_not_adding_capital(self):
        state = dict(cash='319407.50', positions={}, days={})
        # No invented trades to make the ledger agree: missing history must fail.
        record = dict(seq=1,at='2026-10-10T00:00:00Z',event='PAPER_BALANCE_ADJUSTED',
                      body=dict(before='238292.50',after='300000',adjustment='61707.50'))
        result = reconcile(state,[record],'200000')
        self.assertEqual(result['net_contributed_capital'],'261707.50')
        self.assertEqual(result['status'],'review-required')
        self.assertIn('adjustment-before-mismatch',result['issues'])

    def test_invalid_nonfinite_values_rejected(self):
        for value in ('NaN','Infinity',True,'1e999'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reconcile(dict(cash=value),[],'200000')

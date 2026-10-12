"""Two-position synthetic broker acceptance; production release remains closed."""
from datetime import datetime, timezone
import json
import unittest

import test_live_strategy as fixtures
from test_live_execution import report
from orion.live.ledger import Refused
from orion.live.portfolio import PortfolioMonitor


class PortfolioTests(unittest.TestCase):
    tearDown = fixtures.StrategyTests.tearDown

    def setUp(self):
        fixtures.StrategyTests.setUp(self)
        self.second = 'secondentry'
        db = self.ledger.db
        row = dict(db.execute('SELECT * FROM intents WHERE tag=?', (self.tag,)).fetchone())
        row.update(tag=self.second, event='second', broker_id='broker2')
        db.execute('INSERT INTO intents('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        for table, field in [('execution_terms','tag'), ('reconciliation_instruments','entry_tag'), ('fill_contracts','entry_tag')]:
            row = dict(db.execute(f'SELECT * FROM {table} WHERE {field}=?', (self.tag,)).fetchone())
            row[field] = self.second
            db.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')', tuple(row.values()))
        db.execute('''CREATE TABLE entry_exit_plans(entry_tag TEXT PRIMARY KEY,source TEXT,source_digest TEXT,
            policy_version INTEGER,targets TEXT,stop_limit TEXT,target_timeout INTEGER)''')
        db.execute('INSERT INTO entry_exit_plans VALUES(?,?,?,?,?,?,?)',
                   (self.second,'second','synthetic',1,json.dumps(['110','120','130']),'89.95',10))
        self.extra = self.session.sdk.entry | dict(nOrdNo='broker2')
        self.session.sdk.order_report = lambda: report(self.session.sdk.entry,self.extra,*self.session.sdk.stops)
        def positions():
            qty = self.session.sdk.entry['fldQty'] + self.extra['fldQty'] - sum(r['fldQty'] for r in self.session.sdk.stops)
            return report(dict(actId='TESTUCC',exSeg='nse_fo',prod='NRML',tok='123',trdSym='TESTCE',cfBuyQty=0,cfSellQty=0,flBuyQty=qty,flSellQty=0)) if qty else report()
        self.session.sdk.positions = positions
        self.portfolio = PortfolioMonitor(self.ledger,self.session)
        self.portfolio.bind_stop_limit(self.second,'89.95')

    def cycle(self, first='105', second='105'):
        before = len(self.session.sdk.calls)
        result = self.portfolio.cycle(marks={self.tag:(first,datetime.now(timezone.utc)),self.second:(second,datetime.now(timezone.utc))})
        calls = self.session.sdk.calls[before:]
        self.assertLessEqual(sum('transaction_type' in c or 'order_id' in c for c in calls),1)
        return result

    def protected(self):
        self.assertEqual(self.cycle()['action'],'protection-requested')
        self.assertEqual(self.cycle()['action'],'strategy-protection-requested')
        value = self.cycle()
        self.assertEqual(value['state'],'observed')
        self.assertEqual(value['exposure'],6)
        self.assertEqual(value['uncovered'],0)
        self.ledger._check_incidents(allow_protected=True)

    def test_two_positions_keep_distinct_plans_and_observed_stops(self):
        self.protected()
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM strategy_plans').fetchone()[0],2)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(DISTINCT entry_tag) FROM protective_exits WHERE status='OPEN'").fetchone()[0],2)
        with self.assertRaises(Refused): self.ledger._check_incidents()  # Legacy flat-only callers remain strict.

    def test_waiting_first_target_does_not_starve_second_position(self):
        self.protected()
        self.assertEqual(self.cycle('111','111')['action'],'target-reached')
        self.assertEqual(self.cycle('111','111')['action'],'target-reached')
        self.assertEqual(self.cycle('111','111')['action'],'strategy-cancel-requested')
        self.assertEqual(self.cycle('111','111')['action'],'strategy-cancel-requested')
        ids = [c['order_id'] for c in self.session.sdk.calls if 'order_id' in c]
        self.assertEqual(len(set(ids)),2)
        with self.assertRaises(Refused): self.ledger._check_incidents(allow_protected=True)
        for _ in range(3): self.cycle('111','111')
        self.assertEqual(len([c for c in self.session.sdk.calls if 'order_id' in c]),2)

    def test_missing_stop_blocks_admission_and_other_position_keeps_its_stop(self):
        self.protected()
        stop = self.session.sdk.stops[0]
        stop['ordSt'] = 'rejected'
        value = self.cycle()
        self.assertEqual(value['state'],'review-required')
        with self.assertRaises(Refused): self.ledger._check_incidents(allow_protected=True)
        self.assertEqual(self.session.sdk.stops[1]['ordSt'],'trigger pending')

    def test_portfolio_premium_and_risk_are_aggregated_before_admission(self):
        from orion.live.accounting import Accounting
        from orion.live.entry_risk import assess
        from test_live_execution import LIMITS
        self.protected()
        accounting = Accounting(self.ledger, 'TESTUCC')
        now = datetime.now(timezone.utc)
        for tag, oid in ((self.tag, 'broker1'), (self.second, 'broker2')):
            accounting.record(account=self.ledger.account, ucc='TESTUCC',trade_id=oid,
                broker_id=oid,segment='nse_fo',symbol='TESTCE',side='BUY',quantity=3,
                price='100',fee='1',executed_at=now)
        self.cycle()
        limits = LIMITS | dict(max_lots=3,max_order_premium='800',max_open_premium='800')
        candidate = dict(account=self.ledger.account,source='third',limit_price='100',stop='90',lot_size=1,lots=3)
        marks = {tag:('105',datetime.now(timezone.utc)) for tag in (self.tag,self.second)}
        with self.ledger.transaction():
            value = assess(self.ledger,accounting,limits,candidate,now=datetime.now(timezone.utc),marks=marks)
            self.assertEqual(value['lots'],1)
            self.assertEqual(value['reserved_premium'],'620')
            with self.assertRaises(Refused):
                assess(self.ledger,accounting,limits | dict(max_trade_loss='90',max_open_risk='90'),candidate,
                       now=datetime.now(timezone.utc),marks=marks)

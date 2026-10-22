"""Synthetic broker lifecycle tests. No credentials, account or live orders."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from orion.core import allocations
from orion.live.accounting import Accounting
from orion.live.commands import Commands
from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.ledger import Ledger, Refused
from orion.live.strategy import ExitStrategy
from test_live_execution import LIMITS, report
from test_live_strategy import Session


class LiveTargetAllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.counter = 0

    def start(self, lots, *, filled_lots=None, average='100', final=True):
        self.counter += 1
        self.path = Path(self.temp.name)/str(self.counter)/'live.db'
        self.ledger = Ledger(self.path, 'a'*32)
        self.addCleanup(lambda ledger=self.ledger: ledger.close())
        self.session = Session()
        self.now = datetime.now(timezone.utc)-timedelta(seconds=1)
        limits = LIMITS | dict(max_lots=6, max_order_premium='10000',
                              max_open_premium='10000', max_trade_loss='1000', max_open_risk='1000')
        harness = ExecutionHarness(self.ledger, 'TESTUCC', limits,
                                   SimulatedBroker(), enrolled=True, reviewed=True)
        harness.check_book(report(), report(), self.now)
        self.ledger.development_resume()
        self.tag = harness.prepare_entry(event='entry', symbol='TESTCE', segment='nse_fo',
            product='NRML', token='123', lots=lots, lot_size=10, limit_price='100', stop='90',
            tick='.05', signal_time=self.now, quote_time=self.now, now=self.now)
        harness.check_book(report(), report(), self.now)
        self.ledger.mark_dispatching(self.tag)
        self.ledger.db.execute('INSERT INTO execution_attempts VALUES(?,?,?)',
                              (self.tag,self.now.isoformat(),self.now.date().isoformat()))
        Commands(self.ledger, self.session).place('ENTRY', self.tag)
        filled = lots if filled_lots is None else filled_lots
        self.session.sdk.entry.update(qty=lots*10, fldQty=filled*10, avgPrc=average,
            ordSt=('complete' if filled == lots else 'cancelled') if final else 'open')
        self.strategy = ExitStrategy(self.ledger, self.session)
        self.strategy.observations.ingest_snapshot(self.session.request('snapshot'))
        if final:
            self.bind()
        self.book = Accounting(self.ledger, 'TESTUCC')
        self.book.bind_multiplier(self.tag, '1')
        self.record(self.session.sdk.entry, filled*10, average, 'buy')

    def bind(self):
        self.strategy.bind(self.tag, targets=['110','120','130'], stop_limit='89.95', target_timeout=10)

    def record(self, row, quantity, price, trade_id):
        self.book.record(account=self.ledger.account, ucc='TESTUCC', trade_id=trade_id,
            broker_id=row['nOrdNo'], segment='nse_fo', symbol='TESTCE',
            side='BUY' if row['trnsTp'] == 'B' else 'SELL', quantity=quantity,
            price=price, fee='2', executed_at=datetime.now(timezone.utc)-timedelta(microseconds=1))

    def plan(self):
        return dict(self.ledger.db.execute('SELECT * FROM strategy_plans').fetchone())

    def cycle(self, price='105'):
        return self.strategy.cycle(marks={self.tag:(price, datetime.now(timezone.utc))})

    def request_target(self, price):
        # A fake broker confirms only requested STOP cancellations. The real
        # strategy must wait for each observed confirmation before a target send.
        for _ in range(15):
            result = self.cycle(price)
            self.assertNotEqual(result['state'], 'review-required', result)
            if result['action'] == 'strategy-cancel-requested':
                oid = self.session.sdk.calls[-1]['order_id']
                row = next(r for r in self.session.sdk.stops if r['nOrdNo'] == oid)
                self.assertEqual(row['prcTp'], 'SL')
                row['ordSt'] = 'cancelled'
            if result['action'] == 'target-requested':
                target = self.session.sdk.stops[-1]
                self.assertEqual(target['prcTp'], 'L')
                return target
        self.fail('Target was not requested within its finite state transitions')

    def fill(self, row, *, quantity=None, price=None, trade_id=None):
        quantity = row['qty']-row['fldQty'] if quantity is None else quantity
        price = row['prc'] if price is None else price
        row.update(fldQty=row['fldQty']+quantity, avgPrc=price)
        row['ordSt'] = 'complete' if row['fldQty'] == row['qty'] else 'open'
        result = self.cycle(price)
        self.record(row, quantity, price, trade_id or row['nOrdNo'])
        return result

    def restart(self):
        before = self.plan()
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a'*32)
        self.addCleanup(lambda ledger=self.ledger: ledger.close())
        self.strategy = ExitStrategy(self.ledger, self.session)
        self.book = Accounting(self.ledger, 'TESTUCC')
        self.bind()
        self.assertEqual(self.plan(), before)

    def test_one_to_six_lots_close_at_allocated_targets_with_exact_pnl_and_fees(self):
        for lots in range(1, 7):
            with self.subTest(lots=lots):
                self.start(lots)
                expected_gross = Decimal(0)
                self.assertEqual(json.loads(self.plan()['quantities']), [n*10 for n in allocations(lots)])
                for stage, count in enumerate(allocations(lots)):
                    if not count:
                        continue
                    target = self.request_target(str(110+10*stage))
                    self.assertEqual((target['qty'], target['prc']), (count*10, str(110+10*stage)))
                    self.fill(target)
                    expected_gross += Decimal((stage+1)*10*count*10)
                    self.restart()
                    remaining = (lots-sum(allocations(lots)[:stage+1]))*10
                    now = datetime.now(timezone.utc)
                    summary = self.book.summary(now=now,
                        marks={self.tag:('130',now)} if remaining else {})
                    self.assertEqual(Decimal(summary['realized']), expected_gross)
                    self.assertEqual(Decimal(summary['fees']), 2*(stage+2))
                    self.assertEqual(summary['open_quantities'], {self.tag:remaining} if remaining else {})
                    if remaining:
                        self.assertEqual(self.cycle(str(110+10*stage))['action'], 'strategy-protection-requested')
                        stop = self.session.sdk.stops[-1]
                        self.assertEqual((stop['qty'], stop['trgPrc']), (remaining, '100' if stage == 0 else '110'))
                self.assertEqual(self.plan()['phase'], 'DONE')
                before = len(self.session.sdk.calls)
                self.assertEqual(self.cycle('150')['exposure'], 0)
                self.assertEqual(len(self.session.sdk.calls), before)
                self.assertEqual(sum(r['fldQty'] for r in self.session.sdk.stops), lots*10)

    def test_gap_above_all_targets_still_waits_for_each_confirmed_fill(self):
        for lots in (1, 2, 3):
            with self.subTest(lots=lots):
                self.start(lots)
                for stage in range(lots):
                    target = self.request_target('150')
                    self.assertEqual(target['prc'], str(110+stage*10))
                    for _ in range(2):
                        self.cycle('150')
                        self.assertEqual(self.plan()['stage'], stage)
                    self.fill(target)
                self.assertEqual(self.plan()['phase'], 'DONE')
                self.assertEqual(sum(r['prcTp'] == 'L' for r in self.session.sdk.stops), lots)
                before = len(self.session.sdk.calls)
                self.cycle('160')
                self.assertEqual(len(self.session.sdk.calls), before)

    def test_partial_t1_does_not_trail_until_full_lot_confirmed(self):
        self.start(2, average='99.975')
        target = self.request_target('110')
        self.fill(target, quantity=5, trade_id='half1')
        self.assertEqual(self.plan()['stage'], 0)
        self.assertEqual(self.session.sdk.stops[-1]['trgPrc'], '90')
        self.restart()
        self.fill(target, quantity=5, trade_id='half2')
        self.assertEqual(self.plan()['stage'], 1)
        self.assertEqual(self.cycle('110')['action'], 'strategy-cancel-requested')
        self.session.sdk.stops[-1]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('110')['action'], 'strategy-protection-requested')
        self.assertEqual((self.session.sdk.stops[-1]['qty'], self.session.sdk.stops[-1]['trgPrc']), (10, '99.95'))

    def test_cancelled_partial_entry_allocates_only_final_filled_lots(self):
        for filled in (1, 2):
            with self.subTest(filled_lots=filled):
                self.start(3, filled_lots=filled)
                self.assertEqual(json.loads(self.plan()['quantities']), [n*10 for n in allocations(filled)])
                for stage in range(filled):
                    self.fill(self.request_target(str(110+stage*10)))
                self.assertEqual(self.cycle('150')['exposure'], 0)

    def test_working_partial_entry_cannot_bind_policy(self):
        self.start(3, filled_lots=1, final=False)
        with self.assertRaisesRegex(Refused, 'Final confirmed entry fills'):
            self.bind()
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM strategy_plans').fetchone())

    def test_partial_final_target_recovery_and_restart_sell_only_remainder(self):
        self.start(5)
        self.fill(self.request_target('110'))
        self.fill(self.request_target('120'))
        target = self.request_target('130')
        self.assertEqual(target['qty'], 30)
        self.fill(target, quantity=10, trade_id='third-partial')
        self.ledger.db.execute('UPDATE strategy_plans SET requested_at=?',
                              ((datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat(),))
        self.assertEqual(self.cycle('130')['action'], 'strategy-cancel-requested')
        target['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('130')['action'], 'strategy-protection-requested')
        self.assertEqual((self.session.sdk.stops[-1]['qty'], self.session.sdk.stops[-1]['trgPrc']), (20, '110'))
        self.restart()
        self.assertEqual(self.cycle('130')['action'], 'target-waiting-for-rearm')
        self.assertEqual(self.cycle('129')['action'], 'target-rearmed')
        replacement = self.request_target('130')
        self.assertEqual(replacement['qty'], 20)
        self.fill(replacement)
        self.assertEqual(self.plan()['phase'], 'DONE')
        self.assertEqual(sum(r['fldQty'] for r in self.session.sdk.stops), 50)
        summary = self.book.summary(now=datetime.now(timezone.utc), marks={})
        self.assertEqual(Decimal(summary['realized']), 1200)
        self.assertEqual(Decimal(summary['fees']), 10)

    def test_odd_lot_target_cancellation_retains_review_gate(self):
        self.start(2)
        target = self.request_target('110')
        self.fill(target, quantity=5, trade_id='odd-partial')
        self.ledger.db.execute('UPDATE strategy_plans SET requested_at=?',
                              ((datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat(),))
        self.assertEqual(self.cycle('110')['action'], 'strategy-cancel-requested')
        target['ordSt'] = 'cancelled'
        result = self.cycle('110')
        self.assertEqual((result['state'], result['action']), ('review-required', 'odd-lot-exposure'))
        self.assertEqual(self.plan()['stage'], 0)

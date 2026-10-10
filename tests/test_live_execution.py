from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.ledger import Ledger, Refused

LIMITS = dict(capital='30000', max_order_premium='2000', max_open_premium='5000',
              daily_loss='1000', fee_reserve='10', max_trade_loss='500', max_open_risk='800',
              max_lots=1, max_entries=3)


def report(*rows):
    return dict(stat='Ok', stCode=200, data=list(rows))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'live.db'
        self.ledger = Ledger(self.path, 'a'*32)
        self.broker = SimulatedBroker()
        self.harness = ExecutionHarness(self.ledger, 'TESTUCC', LIMITS, self.broker, enrolled=True, reviewed=True)
        self.now = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.harness.check_book(report(), report(), self.now)
        self.ledger.development_resume()

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def prepare(self, **changes):
        return self.harness.prepare_entry(**(dict(event='signal1', symbol='TESTCE', segment='nse_fo',
            product='NRML', token='123', lots=1, lot_size=10, limit_price='100', stop='90', tick='.05',
            signal_time=self.now, quote_time=self.now, now=self.now) | changes))

    def dispatch(self, tag, **changes):
        return self.harness.dispatch_entry(tag, **(dict(now=self.now, quote_time=self.now, available_cash='10000') | changes))

    def fill(self, oid, trade_id, side, price, when):
        return self.harness.accounting.record(account='a'*32, ucc='TESTUCC', trade_id=trade_id,
            broker_id=oid, segment='nse_fo', symbol='TESTCE', side=side, quantity=10,
            price=price, fee='1', executed_at=when)

    def test_integrated_entry_protection_exit_and_accounting(self):
        tag = self.prepare()
        self.harness.check_book(report(), report(), self.now)
        oid = self.dispatch(tag)
        self.harness.entry_observation(tag, status='FILLED', filled=10, average='100')
        self.fill(oid, 'buy1', 'BUY', '100', self.now-timedelta(milliseconds=100))
        exit_tag, exit_oid = self.harness.protective_exit(tag)
        self.harness.protection.observe(exit_tag, account='a'*32, broker_id=exit_oid,
            symbol='TESTCE', segment='nse_fo', side='SELL', quantity=10, status='OPEN', filled=0)
        entry = dict(actId='TESTUCC', nOrdNo=oid, exSeg='nse_fo', prod='NRML', tok='123', trdSym='TESTCE',
                     qty=10, fldQty=10, ordSt='complete', trnsTp='B', avgPrc='100', prc='100', trgPrc='0', prcTp='L')
        exit_order = entry | dict(nOrdNo=exit_oid, trnsTp='S', fldQty=0, ordSt='trigger pending',
                                 avgPrc='0', prc='0', trgPrc='90', prcTp='SL-M')
        pos = dict(actId='TESTUCC', exSeg='nse_fo', prod='NRML', tok='123', trdSym='TESTCE',
                   cfBuyQty='0', cfSellQty='0', flBuyQty='10', flSellQty='0')
        self.harness.check_book(report(entry, exit_order), report(pos), self.now)
        with self.assertRaises(Refused):
            self.ledger.development_resume()
        self.harness.protection.observe(exit_tag, account='a'*32, broker_id=exit_oid,
            symbol='TESTCE', segment='nse_fo', side='SELL', quantity=10, status='FILLED', filled=10)
        self.fill(exit_oid, 'sell1', 'SELL', '90', self.now)
        self.harness.check_book(report(entry, exit_order | dict(fldQty=10, ordSt='complete', avgPrc='90')), report(), self.now)
        summary = self.harness.accounting.summary(now=self.now, marks={})
        self.assertEqual(summary['loss_used'], '102')
        self.assertEqual(summary['open_quantities'], {})
        self.assertEqual(len(self.broker.orders), 2)

    def test_cash_signal_and_stop_risk_rechecked_before_dispatch(self):
        tag = self.prepare()
        self.harness.check_book(report(), report(), self.now)
        for patch in [dict(available_cash='1000'), dict(now=self.now+timedelta(seconds=31), quote_time=self.now+timedelta(seconds=31)),
                      dict(quote_time=self.now-timedelta(seconds=31))]:
            with self.subTest(patch=patch), self.assertRaises(Refused):
                self.dispatch(tag, **patch)
        self.harness.limits['max_trade_loss'] = '100'
        with self.assertRaises(Refused):
            self.dispatch(tag)
        self.assertEqual(self.broker.orders, [])
        self.assertEqual(self.ledger.get(tag)['status'], 'PREPARED')

    def test_ambiguous_acceptance_never_retries_even_after_restart(self):
        tag = self.prepare()
        self.harness.check_book(report(), report(), self.now)
        self.broker.timeout_after_accept = True
        with self.assertRaises(Refused):
            self.dispatch(tag)
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a'*32)
        self.harness = ExecutionHarness(self.ledger, 'TESTUCC', LIMITS, self.broker, enrolled=True, reviewed=True)
        with self.assertRaises(Refused):
            self.dispatch(tag)
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(self.ledger.get(tag)['status'], 'UNKNOWN')

    def test_prepared_intents_reserve_cash_and_do_not_duplicate(self):
        first = self.prepare()
        self.harness.check_book(report(), report(), self.now)
        self.assertEqual(self.prepare(), first)
        second = self.prepare(event='signal2')
        self.harness.check_book(report(), report(), self.now)
        with self.assertRaises(Refused):
            self.dispatch(second, available_cash='1500')
        self.assertEqual(self.broker.orders, [])

    def test_rejects_real_transport_and_unreviewed_enrollment(self):
        with self.assertRaises(Refused):
            ExecutionHarness(self.ledger, 'TESTUCC', LIMITS, object(), enrolled=True, reviewed=True)
        with self.assertRaises(Refused):
            ExecutionHarness(self.ledger, 'TESTUCC', LIMITS, self.broker, enrolled=True, reviewed=False)

    def test_transaction_rolls_back_reservation_when_terms_conflict(self):
        tag = self.prepare()
        before = self.ledger.get(tag)
        with self.assertRaises(Refused):
            self.prepare(stop='80')
        self.assertEqual(before, self.ledger.get(tag))
        self.assertEqual(self.ledger.db.execute('SELECT stop FROM execution_terms').fetchone()[0], '90')

from datetime import datetime, timedelta, timezone
import unittest

import test_live_trades as trades
from orion.live.ledger import Refused


class FeeEstimateTests(unittest.TestCase):
    setUp = trades.TradeTests.setUp
    tearDown = trades.TradeTests.tearDown
    committed = trades.TradeTests.committed
    books = trades.TradeTests.books
    start = trades.TradeTests.start

    def ingest(self, *, split=False):
        snapshot=self.start()
        if split:
            snapshot['trades'][0]['quantity']=5
            snapshot['trades'].append(snapshot['trades'][0] | dict(trade_id='trade2'))
        self.observer.ingest_evidence(snapshot)

    def estimate(self, fee='10'):
        now=datetime.now(timezone.utc)
        return self.harness.accounting.estimate_budget(now=now,marks={self.tag:('100',now)},fee_reserve=fee)

    def correct(self, fee):
        now=datetime.now(timezone.utc)
        from orion.live.accounting import IST
        self.harness.accounting.correct_fee(account=self.ledger.account,ucc='TESTUCC',
            source_ref='synthetic-statement',segment='nse_fo',trade_day=now.astimezone(IST).date().isoformat(),
            trade_id='trade1',total_fee=fee,reported_at=now)

    def test_reserve_is_provisional_and_does_not_change_strict_accounting(self):
        self.ingest()
        before=list(self.ledger.db.iterdump())
        value=self.estimate()
        self.assertEqual(value['recorded_fees'],'0')
        self.assertEqual(value['fees'],'10')
        self.assertEqual(value['loss_used'],'10')
        self.assertEqual(value['fee_basis'],'provisional-reserve')
        self.assertFalse(value['fees_verified'])
        self.assertFalse(value['order_submission_available'])
        self.assertEqual(list(self.ledger.db.iterdump()),before)
        with self.assertRaisesRegex(Refused,'charges unverified'):
            self.harness.accounting.summary(now=datetime.now(timezone.utc),marks={})

    def test_partial_fills_reserve_once_per_entry_not_per_fill(self):
        self.ingest(split=True)
        value=self.estimate()
        self.assertEqual(value['unverified_fee_entries'],1)
        self.assertEqual(value['fees'],'10')

    def test_corrections_do_not_double_count_or_release_reserve(self):
        self.ingest()
        self.correct('2')
        value=self.estimate()
        self.assertEqual(value['recorded_fees'],'2')
        self.assertEqual(value['additional_fee_reserve'],'8')
        self.assertEqual(value['fees'],'10')
        self.assertEqual(value['loss_used'],'10')
        self.assertEqual(self.ledger.db.execute('SELECT fee_status FROM broker_fill_evidence').fetchone()[0],'unverified')

    def test_larger_recorded_charges_dominate_estimate(self):
        self.ingest()
        self.correct('40')
        value=self.estimate()
        self.assertEqual(value['fees'],'40')
        self.assertEqual(value['additional_fee_reserve'],'0')
        self.assertEqual(value['loss_used'],'40')

    def test_bound_reserve_cannot_be_reduced_by_caller(self):
        self.ingest()
        self.assertEqual(self.estimate('1')['fees'],'10')
        self.assertEqual(self.estimate('20')['fees'],'20')
        for fee in ('0','NaN',True,'-1'):
            with self.assertRaises(Refused): self.estimate(fee)

    def test_prior_day_unverified_fees_require_reconciliation(self):
        self.ingest()
        from orion.live.accounting import IST
        day=(datetime.now(IST)-timedelta(days=1)).date().isoformat()
        self.ledger.db.execute('UPDATE broker_fill_evidence SET trade_day=?',(day,))
        with self.assertRaisesRegex(Refused,'Prior-day charges'):
            self.estimate()

    def test_missing_bound_fee_reserve_is_not_assumed_zero(self):
        self.ingest()
        self.ledger.db.execute('DELETE FROM execution_terms')
        with self.assertRaisesRegex(Refused,'Bound fee reserve'):
            self.estimate()

    def test_closing_position_does_not_release_unverified_fee_reserve(self):
        self.ingest()
        protection=self.harness.protection
        tag=protection.prepare(self.tag,trigger_price='90',tick_size='.05',lot_size=10)['tag']
        protection.dispatch(tag)
        protection.observe(tag,account=self.ledger.account,broker_id='exit1',symbol='TESTCE',
            segment='nse_fo',side='SELL',quantity=10,status='FILLED',filled=10)
        now=datetime.now(timezone.utc)
        self.harness.accounting.record(account=self.ledger.account,ucc='TESTUCC',trade_id='exit-fill',
            broker_id='exit1',segment='nse_fo',symbol='TESTCE',side='SELL',quantity=10,
            price='100',fee='0',executed_at=now)
        from orion.live.accounting import IST
        self.ledger.db.execute('INSERT INTO broker_fill_evidence VALUES(?,?,?,?,?)',
            ('nse_fo',now.astimezone(IST).date().isoformat(),'exit-fill',now.isoformat(),'unverified'))
        result=self.estimate()
        self.assertEqual(result['open_quantities'],{})
        self.assertEqual(result['fees'],'10')
        self.assertEqual(result['unverified_fee_entries'],1)

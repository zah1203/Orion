from datetime import timedelta
import unittest

import test_live_foundation as foundation
import test_live_execution as execution
from orion.live.ledger import Limits, Refused
from orion.live.protection import Protection


class ReservationTests(unittest.TestCase):
    setUp = foundation.LiveLedgerTests.setUp
    tearDown = foundation.LiveLedgerTests.tearDown
    reserve = foundation.LiveLedgerTests.reserve
    observe = foundation.LiveLedgerTests.observe

    def test_daily_count_resets_but_same_day_rejections_still_count(self):
        self.ledger.development_resume()
        limits = Limits(1, 1, 2000, 500)
        row = self.reserve(limits=limits)
        self.ledger.mark_dispatching(row['tag'])
        self.observe(row['tag'], status='REJECTED')
        with self.assertRaisesRegex(Refused, 'Entry limit'):
            self.reserve(event='second', limits=limits)
        tomorrow = self.now+timedelta(days=1)
        self.assertEqual(self.reserve(event='tomorrow', limits=limits, now=tomorrow,
            signal_time=tomorrow, quote_time=tomorrow)['status'], 'PREPARED')

    def test_day_change_does_not_release_unsent_reservations(self):
        self.ledger.development_resume()
        limits = Limits(1, 1, 1000, 500)
        self.reserve(limits=limits)
        tomorrow = self.now+timedelta(days=1)
        with self.assertRaisesRegex(Refused, 'budget'):
            self.reserve(event='tomorrow', limits=limits, now=tomorrow,
                         signal_time=tomorrow, quote_time=tomorrow)

    def test_partial_cancel_and_stop_ack_release_only_confirmed_units(self):
        self.ledger.development_resume()
        tag = self.reserve()['tag']
        self.ledger.mark_dispatching(tag)
        self.observe(tag, status='CANCELLED', filled=4, average='99')
        self.assertEqual(self.ledger.reserved_units(self.ledger.get(tag)), 4)
        protection = Protection(self.ledger)
        stop = protection.prepare(tag, trigger_price='90', tick_size='.05', lot_size=1)['tag']
        protection.dispatch(stop)
        self.assertEqual(self.ledger.reserved_units(self.ledger.get(tag)), 4)
        protection.observe(stop, account=self.account, broker_id='exit1', symbol='TESTCE', segment='nse_fo',
                           side='SELL', quantity=4, status='PARTIAL', filled=2)
        self.assertEqual(self.ledger.reserved_units(self.ledger.get(tag)), 2)
        protection.request_cancel(stop)
        self.assertEqual(self.ledger.reserved_units(self.ledger.get(tag)), 2)


class ClosedExposureTests(unittest.TestCase):
    setUp = execution.ExecutionTests.setUp
    tearDown = execution.ExecutionTests.tearDown
    prepare = execution.ExecutionTests.prepare
    dispatch = execution.ExecutionTests.dispatch
    fill = execution.ExecutionTests.fill

    def test_closed_exposure_releases_premium_but_realized_loss_still_counts(self):
        execution.ExecutionTests.test_integrated_entry_protection_exit_and_accounting(self)
        self.ledger.development_resume()
        self.harness.limits['max_open_premium'] = '1500'
        self.harness.limits['max_order_premium'] = '1500'
        tag = self.prepare(event='second')
        # Refresh the original terminal order book plus positions for the new
        # reservation; the prepared second intent has no broker identity yet.
        first, stop = self.broker.orders
        entry = dict(actId='TESTUCC', nOrdNo=first['broker_id'], exSeg='nse_fo', prod='NRML', tok='123',
            trdSym='TESTCE', qty=10, fldQty=10, ordSt='complete', trnsTp='B', avgPrc='100', prc='100', trgPrc='0', prcTp='L')
        exit_order = entry | dict(nOrdNo=stop['broker_id'], trnsTp='S', avgPrc='90', prc='0', trgPrc='90', prcTp='SL-M')
        self.harness.check_book(execution.report(entry, exit_order), execution.report(), self.now)
        self.harness.limits['daily_loss'] = '200'
        with self.assertRaisesRegex(Refused, 'loss budget'):
            self.dispatch(tag, available_cash='1500')  # Previous loss 102 + reserved risk 110.
        self.harness.limits['daily_loss'] = '1000'
        self.assertEqual(self.dispatch(tag, available_cash='1500'), 'sim3')

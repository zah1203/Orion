from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from orion.live.accounting import Accounting
from orion.live.ledger import Ledger, Limits, Refused
from orion.live.protection import Protection
from orion.live.reconciliation import Reconciliation, fingerprint


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'admin'/'live.db'
        self.account = 'a'*32
        self.ledger = Ledger(self.path, self.account)
        self.now = datetime(2025, 7, 2, 5, tzinfo=timezone.utc)
        self.entry = self.make_entry(self.ledger)
        self.book = Accounting(self.ledger, 'ADMINUCC')
        self.book.bind_multiplier(self.entry, '1')

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def make_entry(self, ledger):
        ledger.development_resume()
        row = ledger.reserve(event='signal', symbol='TESTCE', segment='nse_fo',
            lots=1, lot_size=10, limit_price='100', tick_size='.05',
            signal_time=self.now, quote_time=self.now, now=self.now,
            limits=Limits(2, 20, 10000, 500), realized_loss=0)
        ledger.mark_dispatching(row['tag'])
        ledger.reconcile(row['tag'], account=ledger.account, broker_id='entry1',
            symbol='TESTCE', quantity=10, status='FILLED', filled=10, average='100')
        return row['tag']

    def record(self, **changes):
        return self.book.record(**(dict(account=self.account, ucc='ADMINUCC',
            trade_id='trade1', broker_id='entry1', segment='nse_fo', symbol='TESTCE',
            side='BUY', quantity=10, price='100', fee='2',
            executed_at=self.now-timedelta(minutes=10)) | changes))

    def summary(self, **changes):
        return self.book.summary(**(dict(now=self.now, marks={self.entry: ('90', self.now)}) | changes))

    def sell_order(self, filled):
        protection = Protection(self.ledger)
        tag = protection.prepare(self.entry, trigger_price='90', tick_size='.05', lot_size=1)['tag']
        protection.dispatch(tag)
        protection.observe(tag, account=self.account, broker_id='exit1', symbol='TESTCE',
            segment='nse_fo', side='SELL', quantity=10,
            status='FILLED' if filled == 10 else 'PARTIAL', filled=filled)
        return tag

    def test_partial_exit_fees_and_open_losses(self):
        self.record()
        self.sell_order(4)
        self.record(trade_id='sell1', broker_id='exit1', side='SELL', quantity=4,
                    price='95', fee='1', executed_at=self.now-timedelta(minutes=5))
        result = self.summary()
        self.assertEqual(result['realized'], '-20')
        self.assertEqual(result['fees'], '3')
        self.assertEqual(result['unrealized_loss'], '60')
        self.assertEqual(result['loss_used'], '83')
        self.assertEqual(result['open_premium'], '600')
        self.assertEqual(result['open_quantities'], {self.entry: 6})

    def test_restart_and_duplicate_trade_are_idempotent(self):
        self.assertTrue(self.record())
        self.ledger.close()
        self.ledger = Ledger(self.path, self.account)
        self.book = Accounting(self.ledger, 'ADMINUCC')
        self.assertFalse(self.record(price='100.00', fee='2.0'))
        self.assertEqual(self.summary()['filled_entries'], 1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM fill_history').fetchone()[0], 1)

    def test_conflicting_trade_rolls_back_and_latches(self):
        self.record()
        with self.assertRaises(Refused):
            self.record(fee='3')
        self.assertEqual(self.summary()['fees'], '2')
        with self.assertRaises(Refused):
            self.ledger.development_resume()

    def test_admin_and_new_account_are_isolated(self):
        self.record()
        other = Ledger(Path(self.tmp.name)/'new'/'live.db', 'b'*32)
        try:
            entry = self.make_entry(other)
            other_book = Accounting(other, 'NEWUCC')
            other_book.bind_multiplier(entry, '2')
            other_book.record(account='b'*32, ucc='NEWUCC', trade_id='trade1', broker_id='entry1',
                segment='nse_fo', symbol='TESTCE', side='BUY', quantity=10, price='100', fee='7',
                executed_at=self.now-timedelta(minutes=10))
            self.assertEqual(other_book.summary(now=self.now, marks={entry: ('100', self.now)})['fees'], '7')
            self.assertEqual(self.summary()['fees'], '2')
            with self.assertRaises(Refused):
                self.record(account='b'*32, ucc='NEWUCC')
            # One account's incident does not lock the other.
            self.assertEqual(other.db.execute('SELECT COUNT(*) FROM live_incidents').fetchone()[0], 0)
        finally:
            other.close()

    def test_missing_fill_history_and_average_mismatch(self):
        self.record(quantity=4)
        with self.assertRaisesRegex(Refused, 'Incomplete'):
            self.summary()
        self.record(trade_id='trade2', quantity=6, price='99')
        with self.assertRaisesRegex(Refused, 'average'):
            self.summary()

    def test_unknown_order_identity_and_overfill(self):
        for patch in [dict(broker_id='unknown'), dict(quantity=11), dict(side='SELL'),
                      dict(ucc='OTHER'), dict(symbol='OTHER'), dict(price='101'),
                      dict(quantity=True), dict(fee='NaN')]:
            with self.subTest(patch=patch), self.assertRaises(Refused):
                self.record(**patch)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM fill_history').fetchone()[0], 0)

    def test_indian_day_boundary_and_carry_loss(self):
        # 18:30 UTC is midnight in India, including summer.
        boundary = datetime(2025, 7, 1, 18, 30, tzinfo=timezone.utc)
        self.record(executed_at=boundary-timedelta(seconds=1))
        result = self.summary(now=boundary+timedelta(seconds=1), marks={self.entry: ('90', boundary)})
        self.assertEqual(result['day'], '2025-07-02')
        self.assertEqual(result['filled_entries'], 0)
        self.assertEqual(result['fees'], '0')
        self.assertEqual(result['loss_used'], '100')  # Carried loss does not disappear.

    def test_realized_gain_does_not_offset_fees(self):
        self.record(executed_at=self.now-timedelta(days=1))
        self.sell_order(10)
        self.record(trade_id='sell1', broker_id='exit1', side='SELL', price='110', fee='3')
        result = self.summary(marks={})
        self.assertEqual(result['realized'], '100')
        self.assertEqual(result['loss_used'], '3')
        self.assertEqual(result['open_quantities'], {})

    def test_marks_fail_closed(self):
        self.record()
        for marks in [{}, {self.entry: ('90', self.now-timedelta(seconds=31))},
                      {self.entry: ('90', self.now+timedelta(seconds=1))},
                      {self.entry: ('NaN', self.now)}]:
            with self.subTest(marks=marks), self.assertRaises(Refused):
                self.summary(marks=marks)

    def test_explicit_multiplier_and_snapshot_invalidation(self):
        before = fingerprint(self.ledger)
        self.record()
        self.assertNotEqual(before, fingerprint(self.ledger))
        with self.assertRaises(Refused):
            self.book.bind_multiplier(self.entry, 2)
        self.ledger.db.execute('DELETE FROM fill_contracts')
        with self.assertRaises(Refused):
            self.summary()
        self.book.bind_multiplier(self.entry, 10)
        self.assertEqual(self.summary()['loss_used'], '1002')

    def test_limits_pause_without_mutating_positions(self):
        self.record()
        before = self.ledger.get(self.entry)
        for patch in [dict(max_daily_loss='102'), dict(max_filled_entries=1), dict(max_open_premium='1000')]:
            with self.subTest(patch=patch), self.assertRaises(Refused):
                self.book.check_limits(**(dict(now=self.now, marks={self.entry: ('90', self.now)},
                    max_daily_loss='500', max_filled_entries=10, max_open_premium='10000') | patch))
        self.assertEqual(self.ledger.get(self.entry), before)
        self.assertEqual(self.ledger.db.execute('SELECT paused FROM control').fetchone()[0], 1)

    def test_ucc_binding_matches_reconciliation_in_both_directions(self):
        with self.assertRaises(Refused):
            Reconciliation(self.ledger, 'WRONG')
        Reconciliation(self.ledger, 'ADMINUCC')
        with self.assertRaises(Refused):
            Accounting(self.ledger, 'WRONG')

    def test_future_naive_and_sell_before_buy(self):
        for stamp in [self.now.replace(tzinfo=None), datetime.now(timezone.utc)+timedelta(days=1)]:
            with self.assertRaises(Refused):
                self.record(executed_at=stamp)
        # Use a fresh fixture DB after intentional validation incidents.
        self.ledger.db.execute('DELETE FROM live_incidents')
        self.record()
        self.sell_order(10)
        self.record(trade_id='sell1', broker_id='exit1', side='SELL', fee='1',
                    executed_at=self.now-timedelta(days=1))
        with self.assertRaisesRegex(Refused, 'precedes'):
            self.summary()

    def test_multi_fill_fifo_and_out_of_order_arrival(self):
        self.ledger.db.execute("UPDATE intents SET average='99.6' WHERE tag=?", (self.entry,))
        self.record(trade_id='later', quantity=6, price='100', fee='1')
        self.record(trade_id='earlier', quantity=4, price='99', fee='1',
                    executed_at=self.now-timedelta(minutes=20))
        self.sell_order(5)
        self.record(trade_id='sell', broker_id='exit1', side='SELL', quantity=5, price='90',
                    fee='1', executed_at=self.now-timedelta(minutes=5))
        result = self.summary()
        self.assertEqual(result['realized'], '-46')
        self.assertEqual(result['loss_used'], '99')

    def test_trade_id_can_repeat_on_different_indian_day(self):
        self.record(quantity=4, executed_at=self.now-timedelta(days=1))
        self.record(quantity=6)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM fill_history').fetchone()[0], 2)
        self.assertEqual(self.summary()['filled_entries'], 0)

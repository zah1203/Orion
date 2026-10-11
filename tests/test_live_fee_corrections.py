from datetime import timedelta
import unittest

import test_live_accounting as fixtures
from orion.live.accounting import Accounting
from orion.live.ledger import Ledger, Refused
from orion.live.reconciliation import fingerprint


class FeeCorrectionTests(unittest.TestCase):
    setUp = fixtures.AccountingTests.setUp
    tearDown = fixtures.AccountingTests.tearDown
    make_entry = fixtures.AccountingTests.make_entry
    record = fixtures.AccountingTests.record
    summary = fixtures.AccountingTests.summary

    def correct(self, **changes):
        return self.book.correct_fee(**(dict(account=self.account, ucc='ADMINUCC', source_ref='statement1',
            segment='nse_fo', trade_day='2025-07-02', trade_id='trade1', total_fee='5',
            reported_at=self.now-timedelta(minutes=1)) | changes))

    def test_append_only_correction_and_replay_after_restart(self):
        self.record()
        original = tuple(self.ledger.db.execute('SELECT * FROM fill_history').fetchone())
        before = fingerprint(self.ledger)
        self.assertTrue(self.correct())
        self.assertNotEqual(before, fingerprint(self.ledger))
        self.assertEqual(self.summary()['fees'], '5')
        self.assertEqual(self.summary()['loss_used'], '105')
        self.ledger.close()
        self.ledger = Ledger(self.path, self.account)
        self.book = Accounting(self.ledger, 'ADMINUCC')
        self.assertFalse(self.correct(total_fee='5.00'))
        self.assertEqual(tuple(self.ledger.db.execute('SELECT * FROM fill_history').fetchone()), original)

    def test_later_refund_and_historical_asof_are_exact(self):
        self.record()
        self.correct()
        self.correct(source_ref='statement2', total_fee='1.25', reported_at=self.now)
        self.assertEqual(self.summary()['fees'], '1.25')
        earlier = self.now-timedelta(seconds=30)
        self.assertEqual(self.summary(now=earlier, marks={self.entry:('90', earlier)})['fees'], '5')
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM fee_corrections').fetchone()[0], 2)

    def test_changed_or_older_report_latches_without_changing_fees(self):
        self.record()
        self.correct()
        for changes in [dict(total_fee='8'), dict(source_ref='old', reported_at=self.now-timedelta(minutes=2))]:
            with self.assertRaises(Refused): self.correct(**changes)
        self.assertEqual(self.summary()['fees'], '5')
        self.assertTrue(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())

    def test_wrong_account_unknown_fill_and_negative_fee_rejected(self):
        self.record()
        for changes in [dict(account='b'*32), dict(ucc='OTHER'), dict(trade_id='missing'), dict(total_fee='-1')]:
            with self.assertRaises(Refused): self.correct(**changes)
        self.assertEqual(self.summary()['fees'], '2')

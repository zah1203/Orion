from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from orion.live.ledger import Ledger, Limits, Refused


class LiveLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'live.db'
        self.account = 'a' * 32
        self.ledger = Ledger(self.path, self.account)
        self.now = datetime.now(timezone.utc)
        self.limits = Limits(1, 3, Decimal('2000'), Decimal('500'))
        self.args = dict(event='channel:message:1', symbol='TESTCE', segment='nse_fo',
                         lots=1, lot_size=10, limit_price='100', tick_size='0.05',
                         signal_time=self.now, quote_time=self.now, now=self.now,
                         limits=self.limits, realized_loss='0')

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def reserve(self, **changes):
        return self.ledger.reserve(**(self.args | changes))

    def observe(self, tag, **changes):
        return self.ledger.reconcile(tag, **(dict(account=self.account, broker_id='order1',
                 symbol='TESTCE', quantity=10, status='OPEN', filled=0, average='0') | changes))

    def test_default_pause_and_separate_paper_storage(self):
        paper = Path(self.temp.name) / 'paper.db'
        paper.write_bytes(b'untouched')
        with self.assertRaises(Refused):
            self.reserve()
        with self.assertRaises(Refused):
            Ledger(paper, self.account)
        self.assertEqual(paper.read_bytes(), b'untouched')

    def test_account_binding_and_private_file(self):
        with self.assertRaisesRegex(Refused, 'Account mismatch'):
            Ledger(self.path, 'b' * 32)
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_recovery_marker_forbids_ledger(self):
        (Path(self.temp.name) / 'RECOVERY_ONLY').touch()
        with self.assertRaisesRegex(Refused, 'Recovery'):
            Ledger(self.path, self.account)

    def test_idempotent_intent_and_conflict(self):
        self.ledger.development_resume()
        first = self.reserve()
        self.assertEqual(first, self.reserve())
        with self.assertRaisesRegex(Refused, 'Duplicate'):
            self.reserve(limit_price='101')

    def test_stale_future_and_invalid_numbers(self):
        self.ledger.development_resume()
        for changes in [
            dict(signal_time=self.now - timedelta(seconds=31)),
            dict(quote_time=self.now + timedelta(seconds=1)),
            dict(limit_price='NaN'), dict(limit_price='100.001'),
            dict(lots=True), dict(lot_size=1.5), dict(realized_loss='500'),
            dict(lots=2), dict(segment='nse_cm'),
        ]:
            with self.subTest(changes=changes), self.assertRaises(Refused):
                self.reserve(**changes)

    def test_reservation_serializes_across_connections(self):
        self.ledger.development_resume()
        self.reserve()
        other = Ledger(self.path, self.account)
        try:
            other.reserve(**(self.args | dict(event='second')))
            with self.assertRaisesRegex(Refused, 'budget'):
                self.reserve(event='third')
        finally:
            other.close()

    def test_crash_reopen_prevents_resubmission(self):
        self.ledger.development_resume()
        row = self.reserve()
        self.ledger.mark_dispatching(row['tag'])
        self.ledger.close()
        self.ledger = Ledger(self.path, self.account)
        with self.assertRaises(Refused):
            self.ledger.mark_dispatching(row['tag'])
        with self.assertRaises(Refused):
            self.reserve(event='second')
        with self.assertRaises(Refused):
            self.ledger.development_resume()
        self.observe(row['tag'])
        self.assertEqual(self.ledger.get(row['tag'])['status'], 'OPEN')

    def test_unknown_requires_matching_observation_and_manual_resume(self):
        self.ledger.development_resume()
        tag = self.reserve()['tag']
        self.ledger.mark_dispatching(tag)
        self.ledger.mark_unknown(tag)
        with self.assertRaises(Refused):
            self.ledger.development_resume()
        for changes in (dict(account='b'*32), dict(symbol='OTHER'), dict(quantity=20)):
            with self.assertRaises(Refused):
                self.observe(tag, **changes)
        self.observe(tag, status='REJECTED')
        with self.assertRaisesRegex(Refused, 'paused'):
            self.reserve(event='second')
        self.ledger.development_resume()
        self.reserve(event='second')

    def test_partial_cancel_retains_risk_and_never_decreases_fills(self):
        self.ledger.development_resume()
        tag = self.reserve()['tag']
        self.ledger.mark_dispatching(tag)
        self.observe(tag, status='PARTIAL', filled=4, average='99')
        with self.assertRaises(Refused):
            self.observe(tag)
        self.observe(tag, status='CANCELLED', filled=4, average='99')
        self.reserve(event='second')
        with self.assertRaisesRegex(Refused, 'budget'):
            self.reserve(event='third')
        with self.assertRaises(Refused):
            self.observe(tag, status='FILLED', filled=10, average='99')

    def test_duplicate_broker_order_id_rejected(self):
        import sqlite3
        self.ledger.development_resume()
        first = self.reserve()['tag']
        self.ledger.mark_dispatching(first)
        self.observe(first, status='REJECTED')
        second = self.reserve(event='second')['tag']
        self.ledger.mark_dispatching(second)
        with self.assertRaises(sqlite3.IntegrityError):
            self.observe(second)
        self.assertEqual(self.ledger.get(second)['status'], 'DISPATCHING')

    def test_pause_blocks_prepared_orders_but_allows_reconciliation(self):
        self.ledger.development_resume()
        first = self.reserve()['tag']
        second = self.reserve(event='second')['tag']
        self.ledger.mark_dispatching(first)
        self.ledger.pause()
        with self.assertRaisesRegex(Refused, 'paused'):
            self.ledger.mark_dispatching(second)
        self.observe(first, status='FILLED', filled=10, average='99')

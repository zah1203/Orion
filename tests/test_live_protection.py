from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from orion.live.ledger import Ledger, Limits, Refused
from orion.live.protection import Protection


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'live.db'
        self.account = 'a' * 32
        self.ledger = Ledger(self.path, self.account)
        self.ledger.development_resume()
        now = datetime.now(timezone.utc)
        self.entry = self.ledger.reserve(event='test', symbol='TESTCE', segment='nse_fo',
            lots=1, lot_size=10, limit_price='100', tick_size='.05',
            signal_time=now, quote_time=now, now=now,
            limits=Limits(2, 10, 10000, 500), realized_loss=0)['tag']
        self.ledger.mark_dispatching(self.entry)
        self.entry_observe('FILLED', 10)
        self.protection = Protection(self.ledger)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def entry_observe(self, status, filled):
        return self.ledger.reconcile(self.entry, account=self.account, broker_id='entry1',
            symbol='TESTCE', quantity=10, status=status, filled=filled, average='99' if filled else '0')

    def prepare(self, lot_size=1):
        return self.protection.prepare(self.entry, trigger_price='90', tick_size='.05', lot_size=lot_size)['tag']

    def observe(self, tag, **changes):
        return self.protection.observe(tag, **(dict(account=self.account, broker_id='exit1',
            symbol='TESTCE', segment='nse_fo', side='SELL', quantity=10,
            status='OPEN', filled=0) | changes))

    def test_serialized_exit_and_no_double_send(self):
        tag = self.prepare()
        with self.assertRaises(Refused):
            self.prepare()
        self.protection.dispatch(tag)
        with self.assertRaises(Refused):
            self.protection.dispatch(tag)
        self.observe(tag)
        with self.assertRaises(Refused):
            self.ledger.development_resume()
        self.observe(tag, status='FILLED', filled=10)
        self.ledger.development_resume()
        with self.assertRaises(Refused):
            self.prepare()

    def test_cancel_race_preserves_pending_and_reserves_only_remainder(self):
        tag = self.prepare()
        self.protection.dispatch(tag)
        self.observe(tag)
        self.protection.request_cancel(tag)
        self.assertEqual(self.observe(tag, status='PARTIAL', filled=4)['status'], 'CANCEL_PENDING')
        with self.assertRaises(Refused):
            self.prepare()
        self.observe(tag, status='CANCELLED', filled=6)
        replacement = self.prepare()
        self.assertEqual(self.protection.get(replacement)['quantity'], 4)
        self.protection.dispatch(replacement)
        self.observe(replacement, quantity=4, broker_id='exit2', status='FILLED', filled=4)
        with self.assertRaises(Refused):
            self.ledger.development_resume()  # Incident does not clear itself.

    def test_fill_wins_cancel_race(self):
        tag = self.prepare()
        self.protection.dispatch(tag)
        self.observe(tag)
        self.protection.request_cancel(tag)
        self.observe(tag, status='FILLED', filled=10)
        with self.assertRaises(Refused):
            self.prepare()

    def test_restart_retains_uncertainty_and_never_resends(self):
        tag = self.prepare()
        self.protection.dispatch(tag)
        self.ledger.close()
        self.ledger = Ledger(self.path, self.account)
        self.protection = Protection(self.ledger)
        with self.assertRaises(Refused):
            self.protection.dispatch(tag)
        with self.assertRaises(Refused):
            self.prepare()
        self.protection.uncertain(tag)
        self.observe(tag)
        with self.assertRaises(Refused):
            self.ledger.development_resume()

    def test_rejected_stop_latches_incident_and_preserves_exposure(self):
        tag = self.prepare()
        self.protection.dispatch(tag)
        self.observe(tag, status='REJECTED')
        replacement = self.prepare()
        self.assertEqual(self.protection.get(replacement)['quantity'], 10)
        with self.assertRaises(Refused):
            self.ledger.development_resume()
        self.assertEqual(self.ledger.get(self.entry)['filled'], 10)

    def test_terminal_contradiction_blocks_already_prepared_replacement(self):
        tag = self.prepare()
        self.protection.dispatch(tag)
        self.observe(tag, status='CANCELLED', filled=2)
        replacement = self.prepare()
        with self.assertRaises(Refused):
            self.observe(tag, status='CANCELLED', filled=3)
        with self.assertRaises(Refused):
            self.protection.dispatch(replacement)
        self.assertEqual(self.protection.get(tag)['filled'], 2)

    def test_identity_and_fill_conflicts_are_atomic(self):
        for changes in [dict(account='b'*32), dict(side='BUY'), dict(segment='mcx_fo'),
                        dict(broker_id='entry1'), dict(quantity=True), dict(filled=11),
                        dict(status='FILLED', filled=5)]:
            with self.subTest(changes=changes):
                # Isolated DB state per case, preserving the latched incident.
                self.ledger.db.execute('DELETE FROM live_incidents')
                self.ledger.db.execute('DELETE FROM protective_exits')
                tag = self.prepare()
                self.protection.dispatch(tag)
                with self.assertRaises(Refused):
                    self.observe(tag, **changes)
                self.assertEqual(self.protection.get(tag)['status'], 'DISPATCHING')
                with self.assertRaises(Refused):
                    self.ledger.development_resume()

    def test_partial_entry_must_be_terminal_and_no_lot_rounding(self):
        # Synthetic fixture represents a partially filled entry cancelled by broker.
        self.ledger.db.execute("UPDATE intents SET status='PARTIAL',filled=4 WHERE tag=?", (self.entry,))
        with self.assertRaises(Refused):
            self.prepare()
        self.entry_observe('CANCELLED', 4)
        with self.assertRaises(Refused):
            self.prepare(lot_size=10)
        self.assertEqual(self.protection.get(self.prepare())['quantity'], 4)

    def test_parallel_connection_cannot_reserve_same_exposure(self):
        self.prepare()
        other = Ledger(self.path, self.account)
        try:
            with self.assertRaises(Refused):
                Protection(other).prepare(self.entry, trigger_price='90', tick_size='.05', lot_size=1)
        finally:
            other.close()

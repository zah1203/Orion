from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from orion.live.commands import Commands
from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.kotak import KotakSession, OrderRequest
from orion.live.ledger import Ledger, Refused
from test_live_execution import LIMITS, report
from test_live_kotak import CREDS, FakeSDK


class InProcessFakeSession:
    """The real adapter, driven only by a fake SDK; never imports NeoAPI."""
    ucc = 'TESTUCC'

    def __init__(self):
        self.sdk = FakeSDK()
        self.adapter = KotakSession(self.sdk, CREDS, '123456')

    def request(self, operation, **args):
        oid = (self.adapter.place(OrderRequest(**args['request'])) if operation == 'place'
               else self.adapter.cancel(args['broker_id']))
        return dict(broker_id=oid)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'live.db'
        self.ledger = Ledger(self.path, 'a'*32)
        self.harness = ExecutionHarness(self.ledger, 'TESTUCC', LIMITS, SimulatedBroker(), enrolled=True, reviewed=True)
        self.session = InProcessFakeSession()
        self.commands = Commands(self.ledger, self.session)
        self.now = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.harness.check_book(report(), report(), self.now)
        self.ledger.development_resume()
        self.tag = self.harness.prepare_entry(event='test', symbol='TESTCE', segment='nse_fo', product='NRML',
            token='123', lots=1, lot_size=10, limit_price='100', stop='90', tick='.05',
            signal_time=self.now, quote_time=self.now, now=self.now)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def committed(self):
        self.harness.check_book(report(), report(), self.now)
        with self.ledger.transaction():
            self.ledger.mark_dispatching(self.tag)
            self.ledger.db.execute('INSERT INTO execution_attempts VALUES(?,?,?)', (self.tag, self.now.isoformat(), self.now.date().isoformat()))

    def observe(self, status='OPEN', filled=0):
        self.ledger.reconcile(self.tag, account='a'*32, broker_id='broker1', symbol='TESTCE',
                              quantity=10, status=status, filled=filled, average='100' if filled else '0')

    def test_only_committed_entry_then_ack_keeps_dispatch_unresolved(self):
        with self.assertRaises(Refused): self.commands.place('ENTRY', self.tag)
        self.assertEqual(self.session.sdk.calls, [])
        self.committed()
        self.commands.place('ENTRY', self.tag)
        self.assertEqual(self.ledger.get(self.tag)['status'], 'DISPATCHING')
        with self.assertRaises(Refused): self.commands.confirm('ENTRY', self.tag, 'place')
        self.observe()
        self.commands.confirm('ENTRY', self.tag, 'place')
        self.assertEqual(self.ledger.db.execute('SELECT status FROM broker_commands').fetchone()[0], 'CONFIRMED')
        with self.assertRaises(Refused): self.commands.place('ENTRY', self.tag)
        self.assertEqual(len(self.session.sdk.calls), 1)

    def test_timeout_and_restart_never_resend(self):
        self.committed()
        self.session.sdk.fail = TimeoutError('synthetic-sensitive-error')
        with self.assertRaises(Refused): self.commands.place('ENTRY', self.tag)
        self.assertEqual(self.ledger.get(self.tag)['status'], 'UNKNOWN')
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a'*32)
        commands = Commands(self.ledger, self.session)
        with self.assertRaises(Refused): commands.place('ENTRY', self.tag)
        with self.assertRaises(Refused): self.ledger.development_resume()
        self.assertEqual(len(self.session.sdk.calls), 1)

    def test_crash_after_journal_commit_before_send_is_not_retried(self):
        self.committed()
        self.ledger.db.execute("INSERT INTO broker_commands VALUES('ENTRY',?,'place','{}','SENDING',NULL)", (self.tag,))
        with self.assertRaises(Refused): self.commands.place('ENTRY', self.tag)
        self.assertEqual(self.session.sdk.calls, [])

    def test_cancel_ack_and_partial_fill_do_not_confirm_cancellation(self):
        self.committed()
        self.commands.place('ENTRY', self.tag)
        self.observe()
        self.commands.confirm('ENTRY', self.tag, 'place')
        self.commands.cancel('ENTRY', self.tag)
        self.assertEqual(self.ledger.get(self.tag)['status'], 'OPEN')
        with self.assertRaises(Refused): self.commands.confirm('ENTRY', self.tag, 'cancel')
        with self.assertRaises(Refused): self.commands.cancel('ENTRY', self.tag)
        self.observe('PARTIAL', 4)
        with self.assertRaises(Refused): self.commands.confirm('ENTRY', self.tag, 'cancel')
        self.observe('CANCELLED', 6)
        self.commands.confirm('ENTRY', self.tag, 'cancel')
        self.assertEqual(self.ledger.get(self.tag)['filled'], 6)
        self.assertEqual(len(self.session.sdk.calls), 2)

    def test_stop_ack_cannot_reuse_entry_broker_id(self):
        self.committed()
        self.commands.place('ENTRY', self.tag)
        self.observe('FILLED', 10)
        self.commands.confirm('ENTRY', self.tag, 'place')
        protection = self.harness.protection
        tag = protection.prepare(self.tag, trigger_price='90', tick_size='.05', lot_size=10)['tag']
        protection.dispatch(tag)
        with self.assertRaises(Refused): self.commands.place('STOP', tag, stop_limit='89.95')
        self.assertEqual(protection.get(tag)['status'], 'UNKNOWN')
        self.assertEqual(self.ledger.get(self.tag)['broker_id'], 'broker1')
        self.assertIsNone(protection.get(tag)['broker_id'])

    def test_cross_account_session_rejected(self):
        self.session.ucc = 'OTHER'
        with self.assertRaises(Refused): Commands(self.ledger, self.session)
        self.assertFalse(self.session.sdk.calls)

    def test_pause_or_delay_after_commit_prevents_network_send(self):
        self.committed()
        self.ledger.pause()
        with self.assertRaises(Refused): self.commands.place('ENTRY', self.tag)
        self.ledger.db.execute('UPDATE control SET paused=0')
        old = (self.now-timedelta(seconds=31)).isoformat()
        self.ledger.db.execute('UPDATE execution_attempts SET at=?', (old,))
        with self.assertRaises(Refused): self.commands.place('ENTRY', self.tag)
        self.assertFalse(self.session.sdk.calls)
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM broker_commands').fetchone())

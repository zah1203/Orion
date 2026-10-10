from datetime import datetime, timezone
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cryptography.fernet import Fernet
import test_live_commands as fixtures
from test_live_kotak import FakeSDK, CREDS
from test_live_execution import report, LIMITS
from orion.live.kotak import KotakSession, OrderRequest
from orion.live.ledger import Ledger, Refused
from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.monitor import ProtectionMonitor, AccountMonitor
from orion.portal.store import Store
from orion.portal.app import defaults


class BookSDK(FakeSDK):
    def __init__(self):
        super().__init__()
        self.entry = dict(actId='TESTUCC', nOrdNo='broker1', exSeg='nse_fo', prod='NRML', tok='123',
            trdSym='TESTCE', qty=10, fldQty=0, ordSt='open', trnsTp='B', avgPrc='0',
            prc='100', trgPrc='0', prcTp='L')
        self.stops = []
        self.fail_cancel = False
        self.fail_stop = False

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs['transaction_type'] == 'B':
            return dict(stat='Ok', stCode=200, nOrdNo='broker1')
        oid = 'stop'+str(len(self.stops)+1)
        self.stops.append(self.entry | dict(nOrdNo=oid, trnsTp='S', qty=int(kwargs['quantity']),
            fldQty=0, ordSt='trigger pending', avgPrc='0', prc=kwargs['price'],
            trgPrc=kwargs['trigger_price'], prcTp='SL'))
        if self.fail_stop:
            raise TimeoutError('synthetic accepted stop with lost acknowledgement')
        return dict(stat='Ok', stCode=200, nOrdNo=oid)

    def cancel_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_cancel: raise TimeoutError('synthetic lost cancellation response')
        # ACK intentionally leaves the book unchanged until the next observation.
        return dict(stat='Ok', stCode=200, nOrdNo=kwargs['order_id'])

    def order_report(self):
        return report(self.entry, *self.stops)

    def positions(self):
        qty = self.entry['fldQty']-sum(r['fldQty'] for r in self.stops)
        return report(dict(actId='TESTUCC', exSeg='nse_fo', prod='NRML', tok='123', trdSym='TESTCE',
                           cfBuyQty=0, cfSellQty=0, flBuyQty=qty, flSellQty=0)) if qty else report()


class BookSession:
    ucc = 'TESTUCC'
    def __init__(self):
        self.sdk = BookSDK()
        self.adapter = KotakSession(self.sdk, CREDS, '123456')
    def request(self, operation, **kwargs):
        if operation == 'snapshot': return self.adapter.snapshot()
        oid = (self.adapter.place(OrderRequest(**kwargs['request'])) if operation == 'place'
               else self.adapter.cancel(kwargs['broker_id']))
        return dict(broker_id=oid)
    def close(self):
        self.adapter.closed = True


class MonitorTests(unittest.TestCase):
    setUp = fixtures.CommandTests.setUp
    tearDown = fixtures.CommandTests.tearDown
    committed = fixtures.CommandTests.committed

    def start(self, *, filled=4, status='open', policy=True):
        self.session = BookSession()
        self.monitor = ProtectionMonitor(self.ledger, self.session)
        self.ledger.db.execute('UPDATE execution_terms SET lot_size=1')
        if policy: self.monitor.bind_stop_limit(self.tag, '89.95')
        self.committed()
        self.monitor.commands.place('ENTRY', self.tag)
        self.fill(filled, status)

    def fill(self, quantity, status='open'):
        self.session.sdk.entry.update(fldQty=quantity, ordSt=status, avgPrc='100' if quantity else '0')

    def test_partial_fill_cancel_then_protect_and_cover_late_fill(self):
        self.start()
        self.assertEqual(self.monitor.cycle(entries_allowed=True)['action'], 'entry-cancel-requested')
        self.assertEqual(self.monitor.cycle(entries_allowed=True)['action'], 'protection-requested')
        self.assertEqual(self.monitor.health()['uncovered'], 4)  # ACK is not protection.
        self.assertEqual(self.monitor.cycle()['uncovered'], 0)
        self.fill(6, 'cancelled')
        self.assertEqual(self.monitor.cycle()['action'], 'protection-requested')
        self.assertEqual([r['qty'] for r in self.session.sdk.stops], [4, 2])
        self.assertEqual(self.monitor.cycle()['uncovered'], 0)
        self.assertEqual(sum('order_id' in r for r in self.session.sdk.calls), 1)
        self.assertEqual(sum(r.get('transaction_type') == 'B' for r in self.session.sdk.calls), 1)

    def test_pause_cancels_unfilled_entry_once(self):
        self.start(filled=0)
        self.assertEqual(self.monitor.cycle()['action'], 'entry-cancel-requested')
        self.assertEqual(self.monitor.cycle()['state'], 'awaiting-broker')
        self.assertEqual(len(self.session.sdk.calls), 2)
        self.fill(0, 'cancelled')
        self.assertEqual(self.monitor.cycle()['state'], 'observed')

    def test_missing_stop_policy_or_odd_lot_escalates_without_rounding(self):
        self.start(status='cancelled', policy=False)
        self.assertEqual(self.monitor.cycle()['state'], 'review-required')
        self.assertEqual(self.session.sdk.stops, [])
        self.assertEqual(self.monitor.health()['uncovered'], 4)

    def test_rejected_protection_does_not_loop_replacement_orders(self):
        self.start(filled=10, status='complete')
        self.monitor.cycle()
        self.session.sdk.stops[0]['ordSt'] = 'rejected'
        self.assertEqual(self.monitor.cycle()['state'], 'review-required')
        self.monitor.cycle()
        self.assertEqual(len(self.session.sdk.stops), 1)

    def test_lost_cancel_response_blocks_other_commands(self):
        self.start()
        self.session.sdk.fail_cancel = True
        with self.assertRaises(Refused): self.monitor.cycle()
        with self.assertRaises(Refused): self.monitor.cycle()
        self.assertEqual(len(self.session.sdk.calls), 2)
        self.assertEqual(self.monitor.health()['state'], 'review-required')

    def test_restart_never_resends_accepted_unknown_stop(self):
        self.start(filled=10, status='complete')
        self.session.sdk.fail_stop = True
        with self.assertRaises(Refused): self.monitor.cycle()
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a'*32)
        self.monitor = ProtectionMonitor(self.ledger, self.session)
        with self.assertRaises(Refused): self.monitor.cycle()
        self.assertEqual(len(self.session.sdk.stops), 1)

    def test_restart_reconciles_confirmed_stop_without_duplicate(self):
        self.start(filled=10, status='complete')
        self.monitor.cycle()
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a'*32)
        self.monitor = ProtectionMonitor(self.ledger, self.session)
        self.assertEqual(self.monitor.cycle()['uncovered'], 0)
        self.assertEqual(len(self.session.sdk.stops), 1)

    def test_stop_limit_immutable_and_invalid_limit_never_dispatched(self):
        self.start(filled=10, status='complete')
        with self.assertRaises(Refused): self.monitor.bind_stop_limit(self.tag, '89.90')
        with self.assertRaises(Refused): self.monitor.bind_stop_limit(self.tag, '91')
        self.assertEqual(len(self.session.sdk.calls), 1)

    def test_crash_with_prepared_stop_does_not_report_protected_or_resend(self):
        self.start(filled=10, status='complete')
        self.monitor.observations.ingest_snapshot(self.session.request('snapshot'))
        self.monitor.protection.prepare(self.tag, trigger_price='90', tick_size='.05', lot_size=1)
        result = self.monitor.cycle()
        self.assertEqual(result['state'], 'review-required')
        self.assertEqual(result['uncovered'], 10)
        self.assertEqual(len(self.session.sdk.calls), 1)


class MonitorLeaseTests(unittest.TestCase):
    def test_live_monitor_refuses_paper_worker_and_preserves_paper_file(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp, Fernet.generate_key())
            uid = store.create_user('owner', 'synthetic password', defaults())
            account = store.account_dir(uid)
            paper = account/'paper.db'
            paper.write_bytes(b'synthetic-paper-untouched')
            live = Ledger(account/'live.db', uid)
            ExecutionHarness(live, 'TESTUCC', LIMITS, SimulatedBroker(), enrolled=True, reviewed=True)
            live.close()
            factory = Mock(side_effect=BookSession)
            with open(account/'worker.lock', 'a') as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(Refused):
                    with AccountMonitor(store, uid, factory): pass
            factory.assert_not_called()
            with AccountMonitor(store, uid, factory) as first:
                with self.assertRaises(Refused):
                    with AccountMonitor(store, uid, factory): pass
            self.assertEqual(factory.call_count, 1)
            self.assertIsNone(first.ledger)
            self.assertEqual(paper.read_bytes(), b'synthetic-paper-untouched')

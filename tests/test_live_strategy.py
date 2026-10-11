from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from orion.live.commands import Commands
from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.kotak import KotakSession, OrderRequest
from orion.live.ledger import Ledger, Refused
from orion.live.strategy import ExitStrategy
from test_live_execution import LIMITS, report
from test_live_kotak import CREDS
from test_live_monitor import BookSDK


class StrategySDK(BookSDK):
    def place_order(self, **kwargs):
        result = super().place_order(**kwargs)
        if kwargs['transaction_type'] == 'S':
            self.stops[-1]['prcTp'] = kwargs['order_type']
            self.stops[-1]['ordSt'] = 'open' if kwargs['order_type'] == 'L' else 'trigger pending'
        return result


class Session:
    ucc = 'TESTUCC'
    def __init__(self):
        self.sdk = StrategySDK()
        self.adapter = KotakSession(self.sdk, CREDS, '123456')
    def request(self, operation, **kwargs):
        if operation == 'snapshot': return self.adapter.snapshot()
        oid = self.adapter.place(OrderRequest(**kwargs['request'])) if operation == 'place' else self.adapter.cancel(kwargs['broker_id'])
        return dict(broker_id=oid)
    def close(self):
        self.adapter.closed = True


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'live.db'
        self.ledger = Ledger(self.path, 'a'*32)
        self.session = Session()
        self.harness = ExecutionHarness(self.ledger, 'TESTUCC', LIMITS | dict(max_lots=3), SimulatedBroker(), enrolled=True, reviewed=True)
        self.now = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.harness.check_book(report(), report(), self.now)
        self.ledger.development_resume()
        self.tag = self.harness.prepare_entry(event='entry', symbol='TESTCE', segment='nse_fo', product='NRML', token='123',
            lots=3, lot_size=1, limit_price='100', stop='90', tick='.05', signal_time=self.now, quote_time=self.now, now=self.now)
        self.harness.check_book(report(), report(), self.now)
        self.ledger.mark_dispatching(self.tag)
        self.ledger.db.execute('INSERT INTO execution_attempts VALUES(?,?,?)', (self.tag,self.now.isoformat(),self.now.date().isoformat()))
        Commands(self.ledger, self.session).place('ENTRY', self.tag)
        self.session.sdk.entry.update(qty=3, fldQty=3, avgPrc='100', ordSt='complete')
        self.strategy = ExitStrategy(self.ledger, self.session)
        self.strategy.observations.ingest_snapshot(self.session.request('snapshot'))
        self.strategy.bind(self.tag, targets=['110','120','130'], stop_limit='89.95', target_timeout=10)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def cycle(self, price='105', age=0):
        return self.strategy.cycle(marks={self.tag:(price, datetime.now(timezone.utc)-timedelta(seconds=age))})

    def to_target(self):
        self.assertEqual(self.cycle()['action'], 'strategy-protection-requested')
        self.assertEqual(self.cycle('111')['action'], 'target-reached')
        self.assertEqual(self.cycle('111')['action'], 'strategy-cancel-requested')
        self.assertEqual(self.cycle('111')['action'], 'cancel-confirmation-pending')
        self.session.sdk.stops[0]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('111')['action'], 'target-requested')
        return self.session.sdk.stops[1]

    def test_target_waits_for_cancel_confirmation_and_trails_after_fill(self):
        target = self.to_target()
        self.assertEqual(target['qty'], 1)
        self.assertEqual(target['prcTp'], 'L')
        self.assertEqual(self.cycle('111')['action'], 'strategy-protection-requested')
        self.assertEqual(self.cycle('111')['uncovered'], 1)  # Limit target is not a stop.
        target.update(ordSt='complete', fldQty=1, avgPrc='110')
        self.assertEqual(self.cycle('111')['action'], 'target-fill-confirmed')
        self.assertEqual(self.cycle('111')['action'], 'strategy-cancel-requested')
        self.session.sdk.stops[2]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('111')['action'], 'strategy-protection-requested')
        self.assertEqual(self.session.sdk.stops[-1]['trgPrc'], '100')
        self.assertEqual(self.session.sdk.stops[-1]['qty'], 2)
        self.assertEqual(self.cycle('111')['uncovered'], 0)
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())

    def test_cancel_fill_race_never_oversells(self):
        self.cycle(); self.cycle('111'); self.cycle('111')
        self.session.sdk.stops[0].update(ordSt='cancelled', fldQty=2, avgPrc='90')
        self.assertEqual(self.cycle('111')['action'], 'target-requested')
        self.assertEqual(self.session.sdk.stops[-1]['qty'], 1)
        self.session.sdk.stops[-1].update(ordSt='complete', fldQty=1, avgPrc='110')
        self.assertEqual(self.cycle('111')['exposure'], 0)
        self.assertEqual(len(self.session.sdk.stops), 2)

    def test_stale_or_retreated_price_after_cancel_reprotects_without_target(self):
        self.cycle(); self.cycle('111'); self.cycle('111')
        self.session.sdk.stops[0]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('109', age=31)['action'], 'strategy-protection-requested')
        self.assertTrue(all(r['prcTp'] == 'SL' for r in self.session.sdk.stops))

    def test_target_timeout_cancels_once_then_protects_without_retry(self):
        target = self.to_target()
        self.ledger.db.execute('UPDATE strategy_plans SET requested_at=?', ((self.now-timedelta(seconds=11)).isoformat(),))
        self.assertEqual(self.cycle('111')['action'], 'strategy-cancel-requested')
        self.assertEqual(self.cycle('111')['action'], 'cancel-confirmation-pending')
        target['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('111')['action'], 'target-ended-before-completion')
        self.assertEqual(self.cycle('111')['action'], 'strategy-protection-requested')
        self.assertEqual(self.cycle('111')['action'], 'strategy-halted')
        self.assertEqual(sum(r['prcTp'] == 'L' for r in self.session.sdk.stops), 1)

    def test_restart_retains_pending_cancellation_and_never_duplicates(self):
        self.cycle(); self.cycle('111'); self.cycle('111')
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a'*32)
        self.strategy = ExitStrategy(self.ledger, self.session)
        self.assertEqual(self.cycle('111')['action'], 'cancel-confirmation-pending')
        self.assertEqual(sum('order_id' in r for r in self.session.sdk.calls), 1)
        self.session.sdk.stops[0]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('111')['action'], 'target-requested')

    def test_gap_escalates_without_repricing_and_policy_is_immutable(self):
        self.cycle()
        self.assertEqual(self.cycle('80')['action'], 'stop-limit-gap')
        self.assertEqual(len(self.session.sdk.stops), 1)
        with self.assertRaises(Refused):
            self.strategy.bind(self.tag, targets=['111','120','130'], stop_limit='89.95', target_timeout=10)

    def test_target_cannot_borrow_stop_capacity_or_be_dispatched_as_stop(self):
        self.cycle(); self.cycle()
        with self.assertRaises(Refused):
            self.strategy.protection.prepare_limit(self.tag, quantity=1, limit_price='110', tick_size='.05', lot_size=1)
        with self.assertRaises(Refused):
            self.strategy.commands.cancel('EXIT', self.ledger.db.execute('SELECT tag FROM protective_exits').fetchone()[0])

    def test_all_three_targets_close_exact_quantity_and_trail_to_t1(self):
        target = self.to_target()
        target.update(ordSt='complete', fldQty=1, avgPrc='110')
        self.cycle('111')
        # No remainder stop existed yet, so the next cycle places it at break-even.
        self.assertEqual(self.cycle('111')['action'], 'strategy-protection-requested')
        self.assertEqual(self.session.sdk.stops[-1]['trgPrc'], '100')
        self.assertEqual(self.cycle('121')['action'], 'target-reached')
        self.assertEqual(self.cycle('121')['action'], 'strategy-cancel-requested')
        self.session.sdk.stops[-1]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('121')['action'], 'target-requested')
        self.session.sdk.stops[-1].update(ordSt='complete', fldQty=1, avgPrc='120')
        self.assertEqual(self.cycle('121')['action'], 'target-fill-confirmed')
        self.assertEqual(self.cycle('121')['action'], 'strategy-protection-requested')
        self.assertEqual(self.session.sdk.stops[-1]['trgPrc'], '110')
        self.cycle('131'); self.cycle('131')
        self.session.sdk.stops[-1]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('131')['action'], 'target-requested')
        self.session.sdk.stops[-1].update(ordSt='complete', fldQty=1, avgPrc='130')
        self.assertEqual(self.cycle('131')['exposure'], 0)
        self.assertEqual(self.ledger.db.execute('SELECT phase FROM strategy_plans').fetchone()[0], 'DONE')
        self.assertEqual(sum(r['fldQty'] for r in self.session.sdk.stops), 3)
        before = len(self.session.sdk.calls)
        self.cycle('140')
        self.assertEqual(len(self.session.sdk.calls), before)

    def test_one_lot_trails_at_zero_allocation_targets_then_sells_only_at_t3(self):
        # Set up an independent one-lot scenario, without changing any real data.
        self.ledger.db.execute('DELETE FROM strategy_plans')
        self.ledger.db.execute('UPDATE intents SET filled=1,body=replace(body,\'"quantity": 3\',\'"quantity": 1\')')
        self.session.sdk.entry.update(qty=1,fldQty=1)
        self.strategy.bind(self.tag, targets=['110','120','130'], stop_limit='89.95', target_timeout=10)
        self.cycle()
        self.assertEqual(self.cycle('111')['action'], 'trailing-milestone')
        self.cycle('111'); self.session.sdk.stops[-1]['ordSt'] = 'cancelled'
        self.cycle('111')
        self.assertEqual(self.session.sdk.stops[-1]['trgPrc'], '100')
        self.assertEqual(self.cycle('121')['action'], 'trailing-milestone')
        self.cycle('121'); self.session.sdk.stops[-1]['ordSt'] = 'cancelled'
        self.cycle('121')
        self.assertEqual(self.session.sdk.stops[-1]['trgPrc'], '110')
        self.cycle('131'); self.cycle('131')
        self.session.sdk.stops[-1]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('131')['action'], 'target-requested')
        self.assertEqual(sum(r['prcTp'] == 'L' for r in self.session.sdk.stops), 1)
        self.assertEqual(self.session.sdk.stops[-1]['qty'], 1)
        self.assertEqual(self.session.sdk.stops[-1]['prc'], '130')

    def test_accepted_target_timeout_is_unknown_and_never_retried(self):
        self.cycle(); self.cycle('111'); self.cycle('111')
        self.session.sdk.stops[0]['ordSt'] = 'cancelled'
        self.session.sdk.fail_stop = True  # Test SDK fails any SELL after acceptance.
        with self.assertRaises(Refused): self.cycle('111')
        self.assertEqual(len(self.session.sdk.stops), 2)
        with self.assertRaises(Refused): self.cycle('111')
        self.assertEqual(len(self.session.sdk.stops), 2)

    def test_lost_quote_before_cancel_keeps_observed_stop(self):
        self.cycle(); self.cycle('111')
        count = len(self.session.sdk.calls)
        self.assertEqual(self.cycle('111', age=31)['action'], 'target-no-longer-current')
        self.assertEqual(len(self.session.sdk.calls), count)
        self.assertEqual(self.session.sdk.stops[0]['ordSt'], 'trigger pending')

    def test_target_fill_below_committed_limit_rolls_back(self):
        target = self.to_target()
        target.update(ordSt='complete', fldQty=1, avgPrc='100')
        with self.assertRaises(Refused): self.cycle('111')
        self.assertEqual(self.ledger.db.execute("SELECT filled FROM protective_exits WHERE trigger='0'").fetchone()[0], 0)
        self.assertTrue(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())

    def test_incident_after_target_dispatch_commit_blocks_transport(self):
        row = self.strategy.protection.prepare_limit(self.tag, quantity=1, limit_price='110', tick_size='.05', lot_size=1)
        self.strategy.protection.dispatch(row['tag'])
        self.strategy.protection.incident('strategy-requires-review')
        before = len(self.session.sdk.calls)
        with self.assertRaises(Refused): self.strategy.commands.place('EXIT', row['tag'])
        self.assertEqual(len(self.session.sdk.calls), before)

class AccountStrategyTests(unittest.TestCase):
    def test_protection_to_targets_keeps_lease_and_session_without_paper_mutation(self):
        import fcntl
        from unittest.mock import Mock
        from cryptography.fernet import Fernet
        from orion.live.monitor import AccountMonitor
        from orion.portal.store import Store
        from orion.portal.app import defaults
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp, Fernet.generate_key())
            uid = store.create_user('owner', 'synthetic password', defaults())
            path = store.account_dir(uid)
            paper = path/'paper.db'
            paper.write_bytes(b'paper fixture untouched')
            ledger = Ledger(path/'live.db', uid)
            ExecutionHarness(ledger, 'TESTUCC', LIMITS, SimulatedBroker(), enrolled=True, reviewed=True)
            ledger.close()
            factory = Mock(side_effect=Session)
            with AccountMonitor(store, uid, factory) as worker:
                ledger = worker.ledger
                harness = ExecutionHarness(ledger, 'TESTUCC', LIMITS | dict(max_lots=3), SimulatedBroker(), enrolled=True, reviewed=True)
                now = datetime.now(timezone.utc)-timedelta(seconds=1)
                harness.check_book(report(), report(), now)
                ledger.development_resume()
                tag = harness.prepare_entry(event='entry', symbol='TESTCE', segment='nse_fo', product='NRML', token='123',
                    lots=3, lot_size=1, limit_price='100', stop='90', tick='.05', signal_time=now, quote_time=now, now=now)
                harness.check_book(report(), report(), now)
                ledger.mark_dispatching(tag)
                ledger.db.execute('INSERT INTO execution_attempts VALUES(?,?,?)', (tag,now.isoformat(),now.date().isoformat()))
                worker.monitor.commands.place('ENTRY', tag)
                worker.session.sdk.entry.update(qty=3,fldQty=3,ordSt='complete',avgPrc='100')
                worker.monitor.bind_stop_limit(tag, '89.95')
                self.assertEqual(worker.cycle()['action'], 'protection-requested')
                original = worker.session
                worker.bind_strategy(tag, targets=['110','120','130'], stop_limit='89.95', target_timeout=10)
                self.assertIs(worker.session, original)
                self.assertIsInstance(worker.monitor, ExitStrategy)
                with open(path/'worker.lock', 'a') as competing:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(worker.cycle(marks={tag:('111',datetime.now(timezone.utc))})['action'], 'target-reached')
                self.assertEqual(factory.call_count, 1)
            self.assertEqual(paper.read_bytes(), b'paper fixture untouched')

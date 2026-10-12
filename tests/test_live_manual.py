from datetime import datetime, timedelta, timezone
import unittest

import test_live_strategy as strategy
import test_live_monitor as monitor
from orion.live.ledger import Ledger, Refused
from orion.live.manual import request
from orion.live.strategy import ExitStrategy


class ManualExitTests(unittest.TestCase):
    setUp = strategy.StrategyTests.setUp
    tearDown = strategy.StrategyTests.tearDown
    cycle = strategy.StrategyTests.cycle

    def ask(self, **changes):
        return request(self.strategy, **(dict(request_id='1'*32, entry_tag=self.tag,
            action='EXIT', limit_price='104') | changes))

    def test_cancel_confirmation_then_exact_remaining_exit_and_no_reentry(self):
        self.cycle()
        value = self.ask()
        self.assertEqual(value['max_quantity'], 3)
        self.assertEqual(self.cycle()['action'], 'manual-cancellation-pending')
        self.assertEqual(self.cycle()['action'], 'manual-cancellation-pending')
        self.assertEqual(len(self.session.sdk.stops), 1)
        # Stop fills one unit while the cancel is in flight.
        self.session.sdk.stops[0].update(ordSt='cancelled', fldQty=1, avgPrc='90')
        self.assertEqual(self.cycle()['action'], 'manual-exit-pending')
        self.assertEqual(self.session.sdk.stops[-1]['qty'], 2)
        self.assertEqual(self.session.sdk.stops[-1]['prc'], '104')
        self.assertEqual(self.ask()['state'], 'PENDING')  # Replay is harmless.
        self.cycle()
        self.assertEqual(len(self.session.sdk.stops), 2)
        self.session.sdk.stops[-1].update(ordSt='complete', fldQty=2, avgPrc='104')
        self.assertEqual(self.cycle()['exposure'], 0)
        self.assertEqual(self.ask()['state'], 'DONE')
        self.cycle('140')
        self.assertEqual(len(self.session.sdk.stops), 2)
        self.assertEqual(sum(r.get('transaction_type') == 'B' for r in self.session.sdk.calls), 1)

    def test_restart_preserves_pending_cancel_without_duplicate(self):
        self.cycle(); self.ask(); self.cycle()
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a'*32)
        self.strategy = ExitStrategy(self.ledger, self.session)
        self.assertEqual(self.cycle()['action'], 'manual-cancellation-pending')
        self.assertEqual(sum('order_id' in r for r in self.session.sdk.calls), 1)
        self.session.sdk.stops[0]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle()['action'], 'manual-exit-pending')

    def test_price_loss_preserves_stop_and_halts_request(self):
        self.cycle(); self.ask()
        self.assertEqual(self.cycle(age=10)['action'], 'manual-exit-price-unavailable')
        self.assertEqual(self.ask()['state'], 'HALTED')
        self.assertEqual(self.cycle()['uncovered'], 0)
        self.assertEqual(len(self.session.sdk.stops), 1)
        self.assertFalse(any('order_id' in r for r in self.session.sdk.calls))

    def test_quote_loss_after_cancel_restores_protection(self):
        self.cycle(); self.ask(); self.cycle()
        self.session.sdk.stops[0]['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle(age=10)['action'], 'manual-exit-price-unavailable')
        self.assertEqual(self.cycle(age=10)['action'], 'strategy-protection-requested')
        self.assertTrue(all(r['prcTp'] == 'SL' for r in self.session.sdk.stops))

    def test_working_exit_timeout_cancels_once_and_never_retries(self):
        self.ask(); self.cycle()
        self.ledger.db.execute('UPDATE manual_requests SET expires_at=?',
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.cycle()['action'], 'manual-cancellation-pending')
        self.assertEqual(self.cycle()['action'], 'manual-cancellation-pending')
        self.session.sdk.stops[0].update(ordSt='cancelled', fldQty=1, avgPrc='104')
        self.assertEqual(self.cycle()['action'], 'manual-exit-ended')
        self.assertEqual(self.cycle()['action'], 'strategy-protection-requested')
        self.assertEqual(self.session.sdk.stops[-1]['qty'], 2)
        self.assertEqual(sum(r['prcTp'] == 'L' for r in self.session.sdk.stops), 1)

    def test_unknown_exit_never_retried(self):
        self.ask(); self.session.sdk.fail_stop = True
        with self.assertRaises(Refused): self.cycle()
        with self.assertRaises(Refused): self.cycle()
        self.assertEqual(len(self.session.sdk.stops), 1)

    def test_mismatched_retry_and_invalid_instructions_refused_without_commands(self):
        self.ask()
        for changes in (dict(limit_price='105'), dict(entry_tag='other'), dict(action='CANCEL',limit_price=None)):
            with self.assertRaises(Refused): self.ask(**changes)
        self.assertEqual(self.session.sdk.stops, [])
        with self.assertRaises(Refused): self.ask(request_id='2'*32)

    def test_fully_filled_stop_during_cancel_suppresses_manual_sell(self):
        self.cycle(); self.ask(); self.cycle()
        self.session.sdk.stops[0].update(ordSt='complete', fldQty=3, avgPrc='90')
        self.assertEqual(self.cycle()['exposure'], 0)
        self.assertEqual(self.ask()['state'], 'DONE')
        self.assertEqual(len(self.session.sdk.stops), 1)

    def test_existing_target_is_cancelled_before_manual_exit(self):
        target = strategy.StrategyTests.to_target(self)
        self.ask()
        self.assertEqual(self.cycle()['action'], 'manual-cancellation-pending')
        self.assertEqual(self.cycle()['action'], 'manual-cancellation-pending')
        target.update(ordSt='cancelled', fldQty=1, avgPrc='110')
        self.assertEqual(self.cycle()['action'], 'manual-exit-pending')
        self.assertEqual(self.session.sdk.stops[-1]['qty'], 2)

    def test_lost_price_does_not_forget_timeout_of_existing_target(self):
        strategy.StrategyTests.to_target(self)
        self.ask()
        self.assertEqual(self.cycle(age=10)['action'], 'manual-exit-price-unavailable')
        self.ledger.db.execute('UPDATE strategy_plans SET requested_at=?',
            ((datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat(),))
        self.assertEqual(self.cycle(age=10)['action'], 'strategy-cancel-requested')

    def test_explicit_exit_can_reduce_known_review_exposure_without_clearing_incident(self):
        self.strategy.protection.incident('strategy-requires-review')
        self.ask()
        self.assertEqual(self.cycle()['action'], 'manual-exit-pending')
        self.assertTrue(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())
        self.assertEqual(self.session.sdk.stops[-1]['qty'], 3)

    def test_manual_exit_never_overrides_unknown_broker_outcome(self):
        self.ask()
        self.strategy.protection.incident('broker-command-unknown')
        with self.assertRaises(Refused): self.cycle()
        self.assertEqual(self.session.sdk.stops, [])

    def test_expiry_between_dispatch_commit_and_transport_prevents_send(self):
        self.ask()
        real = self.strategy.commands.place
        def expire(kind, tag, **kwargs):
            self.ledger.db.execute('UPDATE manual_requests SET expires_at=?',
                ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
            return real(kind, tag, **kwargs)
        self.strategy.commands.place = expire
        with self.assertRaises(Refused): self.cycle()
        self.assertEqual(self.session.sdk.stops, [])
        with self.assertRaises(Refused): self.cycle()
        self.assertEqual(self.session.sdk.stops, [])


class ManualCancelTests(unittest.TestCase):
    setUp = monitor.MonitorTests.setUp
    tearDown = monitor.MonitorTests.tearDown
    committed = monitor.MonitorTests.committed
    start = monitor.MonitorTests.start
    fill = monitor.MonitorTests.fill

    def test_partial_fill_keeps_protection_while_cancellation_pending(self):
        self.start()
        self.monitor.observations.ingest_snapshot(self.session.request('snapshot'))
        request(self.monitor, request_id='1'*32, entry_tag=self.tag, action='CANCEL')
        self.assertEqual(self.monitor.cycle()['action'], 'manual-cancellation-pending')
        self.assertEqual(self.monitor.cycle()['action'], 'protection-requested')
        self.fill(6, 'cancelled')
        self.assertEqual(self.monitor.cycle()['action'], 'protection-requested')
        self.assertEqual([r['qty'] for r in self.session.sdk.stops], [4, 2])
        self.assertEqual(self.ledger.db.execute('SELECT state FROM manual_requests').fetchone()[0], 'DONE')
        self.assertEqual(sum('order_id' in r for r in self.session.sdk.calls), 1)

    def test_exit_refuses_working_entry_and_cannot_cancel_stop_as_entry(self):
        self.start()
        self.monitor.cycle(); self.monitor.cycle()
        with self.assertRaises(Refused):
            request(self.monitor, request_id='1'*32, entry_tag=self.tag, action='EXIT', limit_price='100')
        stop = self.ledger.db.execute('SELECT tag FROM protective_exits').fetchone()[0]
        with self.assertRaises(Refused):
            request(self.monitor, request_id='1'*32, entry_tag=stop, action='CANCEL')

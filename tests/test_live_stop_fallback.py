"""Synthetic broker tests. No network or production account is used."""
from datetime import datetime, timedelta, timezone

from orion.live.ledger import Refused, Ledger
from orion.live.strategy import ExitStrategy
from orion.live.kotak import OrderRequest
import test_live_strategy as strategy_tests
import unittest


class StopFallbackTests(unittest.TestCase):
    setUpBase = strategy_tests.StrategyTests.setUp
    tearDown = strategy_tests.StrategyTests.tearDown
    cycle = strategy_tests.StrategyTests.cycle
    def setUp(self):
        self.setUpBase()
        self.ledger.db.execute("UPDATE strategy_plans SET stop_gap='1'")
        self.ledger.db.execute('INSERT INTO stop_fallback_policy VALUES(?)', (self.tag,))
        self.modifications = []
        self.fail_modify = False
        def modify(**args):
            self.modifications.append(args)
            if self.fail_modify:
                raise TimeoutError('Synthetic uncertain modification')
            return dict(stat='Ok', stCode=200, nOrdNo=args['order_id'])
        self.session.sdk.modify_order = modify
        original = self.session.request
        def request(operation, **args):
            if operation == 'modify':
                return dict(broker_id=self.session.adapter.modify(args['broker_id'], OrderRequest(**args['request']), args['deadline']))
            return original(operation, **args)
        self.session.request = request

    def age(self, seconds):
        first = datetime.now(timezone.utc)-timedelta(seconds=seconds)
        self.ledger.db.execute('UPDATE stop_fallback_state SET first_triggered=?,deadline=?',
            (first.isoformat(), (first+timedelta(seconds=30)).isoformat()))

    def triggered(self):
        self.cycle()
        self.session.sdk.stops[0]['ordSt'] = 'open'
        self.assertEqual(self.cycle('90')['action'], 'stop-recovery-wait')

    def test_pending_does_not_start_timer_from_quote(self):
        self.cycle(); self.cycle('90')
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM stop_fallback_state').fetchone())
        self.assertEqual(self.modifications, [])

    def test_widen_once_and_confirm_only_changed_book(self):
        self.triggered(); self.age(6)
        self.assertEqual(self.cycle('88')['action'], 'stop-widening-requested')
        self.assertEqual(self.modifications[0]['price'], '87.50')
        self.assertEqual(self.modifications[0]['quantity'], '3')
        self.assertEqual(self.cycle('88')['action'], 'stop-widening-pending')
        self.session.sdk.stops[0]['prc'] = '87.50'
        self.assertEqual(self.cycle('88')['action'], 'stop-widening-observed')
        self.assertEqual(len(self.modifications), 1)
        self.assertEqual(len(self.session.sdk.stops), 1)

    def test_partial_fills_keep_total_quantity_and_same_order(self):
        self.triggered(); self.age(6)
        self.session.sdk.stops[0].update(fldQty=1, avgPrc='90')
        self.cycle('88')
        self.assertEqual(self.modifications[0]['quantity'], '3')
        self.assertEqual(self.modifications[0]['order_id'], 'stop1')
        self.session.sdk.stops[0].update(prc='87.50', fldQty=3, avgPrc='89', ordSt='complete')
        self.assertEqual(self.cycle('88')['exposure'], 0)
        self.assertEqual(len(self.session.sdk.stops), 1)

    def test_restart_keeps_original_deadline_and_no_resend(self):
        self.triggered(); self.age(6); self.cycle('88')
        before = tuple(self.ledger.db.execute('SELECT * FROM stop_fallback_state').fetchone())
        self.ledger.close(); self.ledger = Ledger(self.path, 'a'*32)
        self.strategy = ExitStrategy(self.ledger, self.session)
        self.cycle('88')
        self.assertEqual(tuple(self.ledger.db.execute('SELECT * FROM stop_fallback_state').fetchone()), before)
        self.assertEqual(len(self.modifications), 1)

    def test_deadline_leaves_existing_order_and_alerts(self):
        self.triggered(); self.age(31)
        self.assertEqual(self.cycle('80')['action'], 'stop-recovery-deadline')
        self.assertEqual(self.session.sdk.stops[0]['ordSt'], 'open')
        self.assertEqual(self.modifications, [])
        self.assertTrue(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())

    def test_fill_before_attempt_sends_nothing(self):
        self.triggered(); self.age(6)
        self.session.sdk.stops[0].update(fldQty=3, avgPrc='90', ordSt='complete')
        self.assertEqual(self.cycle('88')['exposure'], 0)
        self.assertEqual(self.modifications, [])

    def test_uncertain_modify_never_retries(self):
        self.triggered(); self.age(6); self.fail_modify = True
        with self.assertRaises(Refused): self.cycle('88')
        self.assertEqual(self.ledger.db.execute("SELECT status FROM broker_commands WHERE operation='modify'").fetchone()[0], 'UNKNOWN')
        with self.assertRaises(Refused): self.cycle('88')
        self.assertEqual(len(self.modifications), 1)

    def test_rejected_stop_never_widens(self):
        self.triggered(); self.age(6)
        self.session.sdk.stops[0]['ordSt'] = 'rejected'
        self.assertEqual(self.cycle('88')['state'], 'review-required')
        self.assertEqual(self.modifications, [])

    def test_no_opt_in_preserves_old_behavior(self):
        self.ledger.db.execute('DELETE FROM stop_fallback_policy')
        self.cycle(); self.session.sdk.stops[0]['ordSt'] = 'open'
        self.assertEqual(self.cycle('88')['action'], 'stop-limit-gap')
        self.assertEqual(self.modifications, [])

    def test_missing_trigger_state_is_not_inferred(self):
        self.cycle()
        snapshot = self.session.request('snapshot')
        for order in snapshot['orders']:
            order.pop('trigger_state', None)
        with self.assertRaises(Refused):
            self.strategy.observations.ingest_snapshot(snapshot)
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM stop_fallback_state').fetchone())
        self.assertEqual(self.modifications, [])

    def test_observation_starts_timer_before_strategy_runs(self):
        self.cycle()
        self.session.sdk.stops[0]['ordSt'] = 'open'
        self.strategy.observations.ingest_snapshot(self.session.request('snapshot'))
        self.age(31)
        self.assertEqual(self.cycle()['action'], 'stop-recovery-deadline')
        self.assertEqual(self.modifications, [])

    def test_deadline_after_widen_does_not_cancel_or_replace(self):
        self.triggered(); self.age(6); self.cycle('88')
        self.session.sdk.stops[0]['prc'] = '87.50'
        self.age(31)
        self.assertEqual(self.cycle('80')['action'], 'stop-recovery-deadline')
        self.assertEqual(len(self.modifications), 1)
        self.assertEqual(len(self.session.sdk.stops), 1)
        self.assertFalse(self.ledger.db.execute("SELECT 1 FROM broker_commands WHERE operation='cancel'").fetchone())

    def test_quote_gap_before_trigger_does_not_poison_recovery(self):
        self.cycle()
        self.assertEqual(self.cycle('88')['action'], 'stop-trigger-pending')
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM stop_fallback_state').fetchone())
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())
        self.session.sdk.stops[0]['ordSt'] = 'open'
        self.assertEqual(self.cycle('88')['action'], 'stop-recovery-wait')
        self.age(6)
        self.assertEqual(self.cycle('88')['action'], 'stop-widening-requested')
        self.assertEqual(len(self.modifications), 1)

    def test_pending_quote_gap_does_not_clear_unrelated_incident(self):
        self.cycle()
        self.strategy.protection.incident('strategy-requires-review')
        self.assertEqual(self.cycle('88')['state'], 'review-required')
        self.session.sdk.stops[0]['ordSt'] = 'open'
        self.cycle('88'); self.age(6)
        self.assertEqual(self.cycle('88')['action'], 'stop-widening-blocked')
        self.assertEqual(self.modifications, [])

    def test_target_timeout_and_reprotection_survive_stop_deadline(self):
        target = strategy_tests.StrategyTests.to_target(self)
        self.assertEqual(self.cycle('111')['action'], 'strategy-protection-requested')
        self.cycle('111')
        residual = self.session.sdk.stops[-1]
        residual['ordSt'] = 'open'
        self.cycle('90')
        self.ledger.db.execute('UPDATE strategy_plans SET requested_at=?',
            ((datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat(),))
        self.age(31)
        self.assertEqual(self.cycle('88')['action'], 'strategy-cancel-requested')
        self.assertEqual(self.cycle('88')['state'], 'review-required')
        self.assertEqual(self.cycle('88')['action'], 'cancel-confirmation-pending')
        cancels = self.ledger.db.execute(
            "SELECT * FROM broker_commands WHERE operation='cancel' AND kind='EXIT'").fetchall()
        self.assertEqual(len(cancels), 1)
        self.assertEqual(self.modifications, [])
        self.assertEqual(residual['ordSt'], 'open')
        target['ordSt'] = 'cancelled'
        self.assertEqual(self.cycle('88')['action'], 'strategy-protection-requested')
        self.assertEqual(self.cycle('88')['uncovered'], 0)
        self.assertEqual(residual['ordSt'], 'open')
        self.assertTrue(self.ledger.db.execute(
            "SELECT 1 FROM live_incidents WHERE code='strategy-requires-review'").fetchone())

    def test_working_target_does_not_prevent_eligible_widening(self):
        strategy_tests.StrategyTests.to_target(self)
        self.cycle('111'); self.cycle('111')
        self.session.sdk.stops[-1]['ordSt'] = 'open'
        self.cycle('90'); self.age(6)
        self.assertEqual(self.cycle('88')['action'], 'stop-widening-requested')
        self.assertEqual(len(self.modifications), 1)
        self.assertEqual(self.modifications[0]['quantity'], '2')

    def test_terminal_original_terms_after_ack_are_reconciled_but_reviewed(self):
        self.triggered(); self.age(6); self.cycle('88')
        self.session.sdk.stops[0].update(ordSt='complete', fldQty=3, avgPrc='90')
        result = self.cycle('88')
        self.assertEqual(result['exposure'], 0)
        self.assertEqual(result['state'], 'review-required')
        self.assertEqual(result['action'], 'stop-modification-unconfirmed')
        self.assertEqual(self.ledger.db.execute(
            "SELECT status FROM broker_commands WHERE operation='modify'").fetchone()[0], 'ACKNOWLEDGED')
        self.assertTrue(self.ledger.db.execute('SELECT paused FROM control').fetchone()[0])
        self.assertEqual(len(self.modifications), 1)
        self.ledger.close(); self.ledger = Ledger(self.path, 'a'*32)
        self.strategy = ExitStrategy(self.ledger, self.session)
        self.assertEqual(self.cycle('88')['state'], 'review-required')
        self.assertEqual(len(self.modifications), 1)

    def test_terminal_modify_with_working_target_still_reports_review(self):
        strategy_tests.StrategyTests.to_target(self)
        self.cycle('111'); self.cycle('111')
        residual = self.session.sdk.stops[-1]
        residual['ordSt'] = 'open'
        self.cycle('90'); self.age(6); self.cycle('88')
        residual.update(ordSt='complete', fldQty=2, avgPrc='90')
        result = self.cycle('88')
        self.assertEqual(result['exposure'], 1)
        self.assertEqual(result['state'], 'review-required')
        self.assertEqual(result['action'], 'target-working')
        self.assertEqual(self.ledger.db.execute(
            "SELECT status FROM broker_commands WHERE operation='modify'").fetchone()[0], 'ACKNOWLEDGED')
        self.assertEqual(len(self.modifications), 1)

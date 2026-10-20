import json
from types import SimpleNamespace
import unittest

from orion.live.entry_plans import EntryPlans, handoff
from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.ledger import Ledger, Refused
from orion.live.monitor import ProtectionMonitor
from orion.live.pilot import validate_limits
from test_live_execution import report
from test_live_pilot import LIMITS
import test_live_routing as routing
from test_live_routing import CHANNEL


class EntryPlanTests(unittest.TestCase):
    setUp = routing.RoutingTests.setUp
    tearDown = routing.RoutingTests.tearDown
    message = routing.RoutingTests.message

    def prepare(self, **changes):
        self.message()
        ledger = self.ledgers[self.owner]
        harness = ExecutionHarness(ledger, 'TESTOWNER', LIMITS, SimulatedBroker(), enrolled=True, reviewed=True)
        harness.check_book(report(), report(), self.now)
        ledger.development_resume()
        self.tag = harness.prepare_entry(**(dict(event=CHANNEL+':1', symbol='NIFTYTESTCE', segment='nse_fo',
            product='NRML', token='123', lots=1, lot_size=10, limit_price='101', stop='90', tick='.05',
            signal_time=self.now, quote_time=self.now, now=self.now) | changes))
        self.monitor = ProtectionMonitor(ledger, SimpleNamespace(ucc='TESTOWNER'))
        self.plans = EntryPlans(self.store, self.owner, self.monitor)
        return ledger

    def test_bind_persists_exact_signal_and_reviewed_limits_without_orders(self):
        ledger = self.prepare()
        plan = self.plans.bind(self.tag)
        self.assertEqual(json.loads(plan['targets']), ['110', '120', '130'])
        self.assertEqual(plan['stop_limit'], '89.95')
        self.assertEqual(plan['target_timeout'], 10)
        self.assertEqual(self.plans.bind(self.tag), plan)
        self.assertEqual(ledger.get(self.tag)['status'], 'PREPARED')
        self.assertFalse(ledger.db.execute('SELECT 1 FROM broker_commands').fetchone())
        self.assertEqual(ledger.db.execute('SELECT stop_limit FROM protection_policy').fetchone()[0], '89.95')

    def test_changed_instrument_or_source_cannot_bind(self):
        ledger = self.prepare(token='456')
        with self.assertRaises(Refused): self.plans.bind(self.tag)
        self.assertFalse(ledger.db.execute('SELECT 1 FROM entry_exit_plans').fetchone())
        self.assertFalse(ledger.db.execute('SELECT 1 FROM protection_policy').fetchone())

    def test_edited_signal_and_revoked_policy_cannot_bind(self):
        ledger = self.prepare()
        ledger.db.execute("UPDATE live_signals SET state='INVALIDATED'")
        with self.assertRaises(Refused): self.plans.bind(self.tag)
        ledger.db.execute("UPDATE live_signals SET state='PENDING'")
        self.pilot.revoke(self.owner, self.owner)
        with self.assertRaises(Refused): self.plans.bind(self.tag)

    def test_policy_revision_cannot_replace_bound_plan(self):
        ledger = self.prepare()
        old = self.plans.bind(self.tag)
        self.pilot.configure(self.owner, self.owner, LIMITS | dict(stop_gap_ticks=2))
        self.pilot.consent(self.owner, 2)
        with self.assertRaises(Refused): self.plans.bind(self.tag)
        self.assertEqual(dict(ledger.db.execute('SELECT * FROM entry_exit_plans').fetchone()), old)

    def test_restart_then_revocation_keeps_protection_and_final_fill_handoff(self):
        ledger = self.prepare()
        self.plans.bind(self.tag)
        ledger.db.execute("UPDATE intents SET status='PARTIAL',filled=10,average='100'")
        self.assertIsNone(handoff(self.monitor))
        with self.assertRaises(Refused): self.plans.bind(self.tag)
        ledger.close()
        ledger = Ledger(self.store.account_dir(self.owner)/'live.db', self.owner)
        self.ledgers[self.owner] = ledger
        monitor = ProtectionMonitor(ledger, SimpleNamespace(ucc='TESTOWNER'))
        self.pilot.revoke(self.owner, self.owner)
        ledger.db.execute("UPDATE intents SET status='CANCELLED'")
        manager = handoff(monitor)
        self.assertIsNotNone(manager)
        plan = ledger.db.execute('SELECT * FROM strategy_plans').fetchone()
        self.assertEqual(json.loads(plan['quantities']), [10, 0, 0])
        self.assertEqual(plan['stop_gap'], '0.05')
        self.assertIsNotNone(handoff(manager))
        self.assertFalse(ledger.db.execute('SELECT 1 FROM broker_commands').fetchone())

    def test_missing_plan_or_odd_lot_never_guesses_exit_allocation(self):
        ledger = self.prepare()
        ledger.db.execute("UPDATE intents SET status='FILLED',filled=10")
        self.assertIsNone(handoff(self.monitor))
        ledger.db.execute("UPDATE intents SET status='PREPARED',filled=0")
        self.plans.bind(self.tag)
        ledger.db.execute("UPDATE intents SET status='CANCELLED',filled=1")
        with self.assertRaises(Refused): handoff(self.monitor)
        self.assertFalse(ledger.db.execute('SELECT 1 FROM strategy_plans').fetchone())

    def test_account_isolation(self):
        self.prepare()
        with self.assertRaises(Refused): EntryPlans(self.store, self.other, self.monitor)

    def test_old_policy_requires_new_configuration_and_review(self):
        with self.store.db() as db:
            legacy = {k:v for k,v in LIMITS.items() if k not in ('stop_gap_ticks','target_timeout_seconds')}
            db.execute('UPDATE live_pilots SET limits=? WHERE uid=?', (json.dumps(legacy), self.owner))
        status = self.pilot.status(self.owner)
        self.assertFalse(status['reviewed'])
        self.assertFalse(status['policy_complete'])
        with self.assertRaises(Refused): self.pilot.consent(self.owner, 1)
        for changes in (dict(stop_gap_ticks=0),dict(stop_gap_ticks=True),dict(target_timeout_seconds=61)):
            with self.assertRaises(Refused): validate_limits(LIMITS | changes)

    def test_conflicting_stop_policy_rolls_back_plan(self):
        ledger = self.prepare()
        self.monitor.bind_stop_limit(self.tag, '89.90')
        with self.assertRaises(Refused): self.plans.bind(self.tag)
        self.assertFalse(ledger.db.execute('SELECT 1 FROM entry_exit_plans').fetchone())
        self.assertEqual(ledger.db.execute('SELECT stop_limit FROM protection_policy').fetchone()[0], '89.90')

    def test_stale_source_cannot_bind(self):
        from datetime import timedelta
        ledger = self.prepare()
        ledger.db.execute('UPDATE live_signals SET signal_time=?', ((self.now-timedelta(seconds=31)).isoformat(),))
        with self.assertRaises(Refused): self.plans.bind(self.tag)
        self.assertFalse(ledger.db.execute('SELECT 1 FROM entry_exit_plans').fetchone())

"""Synthetic upgrade and stop-policy regression tests; no live account access."""
from decimal import Decimal
import json
import unittest

from orion.core import allocations
from orion.live.admission import protected_book, strategy_stop
from orion.live.entry_plans import EntryPlans, handoff
from orion.live.ledger import Ledger, Refused
from orion.live.monitor import ProtectionMonitor
from orion.live.portfolio import PortfolioMonitor
from orion.live.strategy import ExitStrategy
import test_live_entry_plans as entry_fixtures
import test_live_strategy as strategy_fixtures


class StrategyPolicyCompatibilityTests(unittest.TestCase):
    setUp = strategy_fixtures.StrategyTests.setUp
    tearDown = strategy_fixtures.StrategyTests.tearDown
    cycle = strategy_fixtures.StrategyTests.cycle
    to_target = strategy_fixtures.StrategyTests.to_target

    def size(self, lots, *, lot_size=1, average='99.975', filled_lots=None):
        """Change only this isolated, synthetic broker/ledger fixture."""
        db = self.ledger.db
        db.execute('DELETE FROM strategy_plans')
        body = json.loads(self.ledger.get(self.tag)['body'])
        body['quantity'] = lots * lot_size
        filled = (lots if filled_lots is None else filled_lots) * lot_size
        status = 'FILLED' if filled == body['quantity'] else 'CANCELLED'
        db.execute('UPDATE intents SET body=?,filled=?,average=?,status=? WHERE tag=?',
                   (json.dumps(body, sort_keys=True), filled, average, status, self.tag))
        db.execute('UPDATE execution_terms SET lot_size=? WHERE tag=?', (lot_size, self.tag))
        self.session.sdk.entry.update(qty=body['quantity'], fldQty=filled, avgPrc=average,
                                      ordSt='complete' if status == 'FILLED' else 'cancelled')

    def saved_entry_plan(self, *, version=None):
        db = self.ledger.db
        db.execute('''CREATE TABLE entry_exit_plans(entry_tag TEXT PRIMARY KEY,
            source TEXT UNIQUE,source_digest TEXT,policy_version INTEGER,targets TEXT,
            stop_limit TEXT,target_timeout INTEGER)''')
        db.execute('INSERT INTO entry_exit_plans VALUES(?,?,?,?,?,?,?)',
                   (self.tag, 'fixture:1', 'synthetic', 1, json.dumps(['110', '120', '130']), '89.95', 10))
        if version is not None:
            db.execute('ALTER TABLE entry_exit_plans ADD COLUMN allocation_version INTEGER NOT NULL DEFAULT 1')
            db.execute('UPDATE entry_exit_plans SET allocation_version=?', (version,))

    def legacy_strategy(self, quantities, *, stage=0, phase='WATCH'):
        db = self.ledger.db
        db.execute('DROP TABLE strategy_plans')
        db.execute('''CREATE TABLE strategy_plans(entry_tag TEXT PRIMARY KEY,
            targets TEXT NOT NULL,quantities TEXT NOT NULL,stop_gap TEXT NOT NULL,
            target_timeout INTEGER NOT NULL,stage INTEGER NOT NULL DEFAULT 0,
            phase TEXT NOT NULL DEFAULT 'WATCH',active_exit TEXT,requested_at TEXT)''')
        db.execute('INSERT INTO strategy_plans VALUES(?,?,?,?,?,?,?,?,?)',
                   (self.tag, json.dumps(['110', '120', '130']), json.dumps(quantities),
                    '0.05', 10, stage, phase, None, None))
        return dict(db.execute('SELECT * FROM strategy_plans').fetchone())

    def bind(self, **kwargs):
        self.strategy.bind(self.tag, targets=['110', '120', '130'], stop_limit='89.95',
                           target_timeout=10, **kwargs)

    def plan(self):
        return dict(self.ledger.db.execute('SELECT * FROM strategy_plans WHERE entry_tag=?', (self.tag,)).fetchone())

    def test_old_one_lot_strategy_migrates_and_handoff_rebinds_without_changes(self):
        self.size(1, lot_size=10)
        old = self.legacy_strategy([0, 0, 10], stage=1)
        self.saved_entry_plan()
        calls = list(self.session.sdk.calls)
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a' * 32)
        monitor = ProtectionMonitor(self.ledger, self.session)
        self.strategy = handoff(monitor)
        self.assertIsInstance(self.strategy, ExitStrategy)
        self.assertEqual(self.plan(), old | {'allocation_version': 1})
        self.bind()
        self.assertIsNotNone(handoff(self.strategy))
        self.assertEqual(self.plan(), old | {'allocation_version': 1})
        with self.assertRaises(Refused):
            self.bind(allocation_version=2)
        self.assertEqual(self.plan(), old | {'allocation_version': 1})
        self.assertEqual(self.session.sdk.calls, calls)
        terms = self.ledger.db.execute('SELECT * FROM execution_terms').fetchone()
        self.assertEqual(self.strategy._stop(self.ledger.get(self.tag), self.plan(), terms), Decimal('100'))

    def test_old_two_lot_recovery_survives_portfolio_and_handoff(self):
        self.size(2, lot_size=10)
        old = self.legacy_strategy([10, 0, 10], phase='RECOVER')
        self.saved_entry_plan()
        self.ledger.db.execute('INSERT INTO strategy_target_recoveries VALUES(?,?,?,?)',
                               ('cancelled-target', self.tag, 0, 0))
        self.ledger.db.execute('INSERT INTO strategy_cancellations VALUES(?,?)',
                               ('cancelled-target', self.tag))
        recoveries = [dict(r) for r in self.ledger.db.execute('SELECT * FROM strategy_target_recoveries')]
        cancellations = [dict(r) for r in self.ledger.db.execute('SELECT * FROM strategy_cancellations')]
        calls = list(self.session.sdk.calls)
        portfolio = PortfolioMonitor(self.ledger, self.session)
        portfolio._bind_ready()
        self.strategy = portfolio.exits
        self.bind()
        self.assertIsNotNone(handoff(portfolio))
        self.assertEqual(self.plan(), old | {'allocation_version': 1})
        self.assertEqual([dict(r) for r in self.ledger.db.execute('SELECT * FROM strategy_target_recoveries')], recoveries)
        self.assertEqual([dict(r) for r in self.ledger.db.execute('SELECT * FROM strategy_cancellations')], cancellations)
        self.assertEqual(self.session.sdk.calls, calls)

    def test_old_unbound_one_lot_entry_plan_uses_legacy_handoff(self):
        self.size(1, lot_size=10)
        self.saved_entry_plan()
        calls = list(self.session.sdk.calls)
        self.strategy = handoff(ProtectionMonitor(self.ledger, self.session))
        self.assertEqual(self.plan()['allocation_version'], 1)
        self.assertEqual(json.loads(self.plan()['quantities']), [0, 0, 10])
        self.assertEqual(self.session.sdk.calls, calls)

    def test_old_unbound_two_lot_entry_plan_uses_legacy_portfolio(self):
        self.size(2, lot_size=10)
        self.saved_entry_plan()
        calls = list(self.session.sdk.calls)
        portfolio = PortfolioMonitor(self.ledger, self.session)
        portfolio._bind_ready()
        self.assertEqual(self.plan()['allocation_version'], 1)
        self.assertEqual(json.loads(self.plan()['quantities']), [10, 0, 10])
        portfolio._bind_ready()
        self.assertEqual(self.session.sdk.calls, calls)

    def test_new_saved_entry_version_reaches_portfolio_unchanged(self):
        self.size(2, lot_size=10)
        self.saved_entry_plan(version=2)
        portfolio = PortfolioMonitor(self.ledger, self.session)
        portfolio._bind_ready()
        self.assertEqual(self.plan()['allocation_version'], 2)
        self.assertEqual(json.loads(self.plan()['quantities']), [10, 10, 0])

    def test_unknown_saved_entry_version_refuses_both_handoff_paths(self):
        self.size(1)
        self.saved_entry_plan(version=99)
        calls = list(self.session.sdk.calls)
        with self.assertRaises(Refused):
            handoff(ProtectionMonitor(self.ledger, self.session))
        portfolio = PortfolioMonitor(self.ledger, self.session)
        with self.assertRaises(Refused):
            portfolio._bind_ready()
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM strategy_plans').fetchone())
        self.assertEqual(self.session.sdk.calls, calls)

    def test_unknown_versions_and_policy_switches_cannot_mutate_bound_plan(self):
        self.size(2)
        for version in (0, 3, 99, True, '2'):
            with self.subTest(version=version), self.assertRaises(Refused):
                self.bind(allocation_version=version)
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM strategy_plans').fetchone())
        self.bind()
        old = self.plan()
        with self.assertRaises(Refused):
            self.bind(allocation_version=1)
        self.assertEqual(self.plan(), old)
        self.ledger.db.execute('UPDATE strategy_plans SET allocation_version=99')
        with self.assertRaises(Refused):
            self.bind()
        with self.assertRaises(Refused):
            strategy_stop(self.ledger.get(self.tag), self.plan(), {'stop': '90', 'tick': '.05'})

    def test_final_cancelled_fill_allocates_confirmed_lots_only(self):
        self.size(3, lot_size=10, filled_lots=1)
        self.bind()
        self.assertEqual(json.loads(self.plan()['quantities']), [10, 0, 0])
        self.assertEqual(self.ledger.get(self.tag)['status'], 'CANCELLED')
        self.size(3, lot_size=10, filled_lots=2)
        self.bind()
        self.assertEqual(json.loads(self.plan()['quantities']), [10, 10, 0])

    def test_weighted_average_rounds_down_and_never_loosens_original_stop(self):
        self.size(3, average='99.975')
        self.bind()
        self.ledger.db.execute('UPDATE strategy_plans SET stage=1')
        terms = self.ledger.db.execute('SELECT * FROM execution_terms').fetchone()
        self.assertEqual(self.strategy._stop(self.ledger.get(self.tag), self.plan(), terms), Decimal('99.95'))
        self.ledger.db.execute("UPDATE intents SET average='89.975'")
        self.assertEqual(self.strategy._stop(self.ledger.get(self.tag), self.plan(), terms), Decimal('90'))
        self.ledger.db.execute('UPDATE strategy_plans SET stage=2')
        self.assertEqual(self.strategy._stop(self.ledger.get(self.tag), self.plan(), terms), Decimal('110'))

    def test_missing_or_invalid_average_is_not_replaced_by_entry_limit(self):
        for average in ('0', '100.05', 'NaN'):
            with self.subTest(average=average):
                self.size(2, average=average)
                with self.assertRaises(Refused):
                    self.bind()
                self.assertFalse(self.ledger.db.execute('SELECT 1 FROM strategy_plans').fetchone())
        entry = {'average': '0', 'body': json.dumps({'limit_price': '100'})}
        plan = {'stage': 1, 'allocation_version': 2}
        with self.assertRaises(Refused):
            strategy_stop(entry, plan, {'stop': '90', 'tick': '.05'})

    def test_admission_accepts_observed_average_stop_after_confirmed_t1_fill(self):
        self.size(3, average='99.975')
        self.bind()
        target = self.to_target()
        target.update(ordSt='complete', fldQty=1, avgPrc='110')
        self.assertEqual(self.cycle('111')['action'], 'target-fill-confirmed')
        self.assertEqual(self.cycle('111')['action'], 'strategy-protection-requested')
        self.assertEqual(self.session.sdk.stops[-1]['trgPrc'], '99.95')
        self.cycle('111')  # Confirm the replacement stop in the synthetic book.
        protected_book(self.ledger)
        self.ledger._check_incidents(allow_protected=True)
        self.assertEqual(self.plan()['stage'], 1)

    def test_legacy_average_improvement_keeps_entry_limit_stop_and_admission(self):
        self.size(2, average='99.975')
        self.bind(allocation_version=1)
        target = self.to_target()
        target.update(ordSt='complete', fldQty=1, avgPrc='110')
        self.assertEqual(self.cycle('111')['action'], 'target-fill-confirmed')
        self.assertEqual(self.cycle('111')['action'], 'strategy-protection-requested')
        self.assertEqual(self.session.sdk.stops[-1]['trgPrc'], '100')
        self.cycle('111')
        protected_book(self.ledger)
        self.ledger._check_incidents(allow_protected=True)


class EntryPolicyCompatibilityTests(unittest.TestCase):
    setUp = entry_fixtures.EntryPlanTests.setUp
    tearDown = entry_fixtures.EntryPlanTests.tearDown
    message = entry_fixtures.EntryPlanTests.message
    prepare = entry_fixtures.EntryPlanTests.prepare

    def test_pre_fill_legacy_plan_migrates_idempotently_and_keeps_legacy_exit(self):
        ledger = self.prepare()
        current = self.plans.bind(self.tag)
        old = {key: value for key, value in current.items() if key != 'allocation_version'}
        ledger.db.execute('ALTER TABLE entry_exit_plans DROP COLUMN allocation_version')
        self.plans = EntryPlans(self.store, self.owner, self.monitor)
        self.assertEqual(self.plans.bind(self.tag), old | {'allocation_version': 1})
        self.assertEqual(ledger.get(self.tag)['status'], 'PREPARED')
        ledger.db.execute("UPDATE intents SET status='CANCELLED',filled=10,average='100.975'")
        manager = handoff(self.monitor)
        self.assertIsNotNone(manager)
        plan = ledger.db.execute('SELECT * FROM strategy_plans').fetchone()
        self.assertEqual(plan['allocation_version'], 1)
        self.assertEqual(json.loads(plan['quantities']), [0, 0, 10])
        self.assertFalse(ledger.db.execute('SELECT 1 FROM broker_commands').fetchone())

    def test_new_pre_fill_plan_pins_current_policy_before_final_fill(self):
        ledger = self.prepare()
        plan = self.plans.bind(self.tag)
        self.assertEqual(plan['allocation_version'], 2)
        self.assertEqual(self.plans.bind(self.tag), plan)
        ledger.db.execute("UPDATE intents SET status='PARTIAL',filled=10,average='100.975'")
        self.assertIsNone(handoff(self.monitor))
        ledger.db.execute("UPDATE intents SET status='CANCELLED'")
        self.assertIsNotNone(handoff(self.monitor))
        saved = ledger.db.execute('SELECT * FROM strategy_plans').fetchone()
        self.assertEqual(saved['allocation_version'], 2)
        self.assertEqual(json.loads(saved['quantities']), [10, 0, 0])
        self.assertFalse(ledger.db.execute('SELECT 1 FROM broker_commands').fetchone())

    def test_unknown_pre_fill_version_is_not_silently_upgraded(self):
        ledger = self.prepare()
        self.plans.bind(self.tag)
        ledger.db.execute('UPDATE entry_exit_plans SET allocation_version=99')
        before = dict(ledger.db.execute('SELECT * FROM entry_exit_plans').fetchone())
        with self.assertRaises(Refused):
            self.plans.bind(self.tag)
        self.assertEqual(dict(ledger.db.execute('SELECT * FROM entry_exit_plans').fetchone()), before)
        self.assertFalse(ledger.db.execute('SELECT 1 FROM broker_commands').fetchone())


class AllocationVersionTests(unittest.TestCase):
    def test_legacy_allocations_remain_exact(self):
        self.assertEqual([allocations(n, version=1) for n in range(1, 7)],
                         [[0, 0, 1], [1, 0, 1], [1, 1, 1], [1, 1, 2], [1, 2, 2], [2, 2, 2]])
        self.assertEqual([allocations(n) for n in range(1, 7)],
                         [[1, 0, 0], [1, 1, 0], [1, 1, 1], [1, 1, 2], [1, 1, 3], [1, 1, 4]])


if __name__ == '__main__':
    unittest.main()

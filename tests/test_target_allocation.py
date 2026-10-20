"""Synthetic paper regressions for quantity, cash, fees and persisted exits."""
import json
from decimal import Decimal
from pathlib import Path
import unittest

from orion.core import Engine, allocations
import test_engine as fixtures


class AllocationTests(unittest.TestCase):
    def test_new_and_legacy_allocations_conserve_whole_lots(self):
        for n in range(1, 1001):
            with self.subTest(lots=n):
                self.assertEqual(allocations(n), [1, int(n >= 2), max(0, n-2)])
                for version in (1, 2):
                    self.assertEqual(sum(allocations(n, version=version)), n)
                    self.assertTrue(all(type(v) is int and v >= 0 for v in allocations(n, version=version)))
        self.assertEqual([allocations(n, version=1) for n in range(1, 7)],
                         [[0,0,1], [1,0,1], [1,1,1], [1,1,2], [1,2,2], [2,2,2]])

    def test_invalid_lots_and_versions_are_refused(self):
        for n in (0, -1, True, 1.5, '2'):
            with self.subTest(lots=n), self.assertRaises(ValueError):
                allocations(n)
        for version in (0, 3, True, '2', None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                allocations(1, version=version)


class PaperTargetTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    tearDown = fixtures.EngineTests.tearDown
    msg = fixtures.EngineTests.msg
    quote = fixtures.EngineTests.quote
    pos = fixtures.EngineTests.pos
    enter = fixtures.EngineTests.enter

    def start(self, lots):
        self.cfg.update(max_lots=lots, risk_per_trade='10000', max_open_risk='10000')
        self.enter()
        self.assertEqual(self.pos()['lots'], lots)
        return Decimal(self.cfg['paper_cash'])

    def restart(self):
        before = self.engine.state()
        self.engine.db.close()
        self.engine = Engine(self.db, self.cfg, self.master, True)
        self.assertEqual(self.engine.state(), before)

    def test_one_two_three_and_larger_lots_exact_accounting(self):
        for n in range(1, 7):
            with self.subTest(lots=n):
                # Keep every fixture isolated, including its audit and dedup keys.
                self.engine.db.close()
                self.db = str(Path(self.tmp.name) / f'lots-{n}.db')
                self.engine = Engine(self.db, self.cfg, self.master, True)
                initial = self.start(n)
                expected = -Decimal(25)
                for i, price in enumerate((180, 200, 220)):
                    amount = allocations(n)[i]
                    out = self.quote(price)
                    if not amount:
                        self.assertEqual(out, [])
                        continue
                    expected += (Decimal(price)-Decimal('170.5'))*10*amount-25
                    self.assertEqual(len(out), 1)
                    self.assertEqual((out[0]['target'], out[0]['sold_lots']), (i+1, amount))
                    self.assertEqual(Decimal(out[0]['fee']), 25)
                    self.assertEqual(Decimal(self.pos()['pnl']), expected)
                    remaining = n-sum(allocations(n)[:i+1])
                    self.assertEqual(self.pos()['remaining'], remaining)
                    if remaining:
                        self.assertEqual(Decimal(self.pos()['stop']), Decimal('170.5' if i == 0 else '180'))
                    self.restart()
                state = self.engine.state()
                self.assertEqual(self.pos()['status'], 'CLOSED')
                self.assertEqual(Decimal(state['cash']), initial+expected)
                self.assertEqual(Decimal(state['days']['2026-09-18']['pnl']), expected)
                history = [json.loads(row[0]) for row in self.engine.db.execute(
                    "SELECT body FROM audit WHERE event IN ('PAPER_BUY','TARGET_REACHED')")]
                self.assertEqual(sum(Decimal(e['realized_delta']) for e in history), expected)
                self.assertEqual(sum(Decimal(e['fee']) for e in history), 25*(1+min(n, 3)))

    def test_gap_above_all_targets_stops_emitting_when_flat(self):
        for n in (1, 2, 3, 6):
            with self.subTest(lots=n):
                self.engine.db.close()
                self.db = str(Path(self.tmp.name) / f'gap-{n}.db')
                self.engine = Engine(self.db, self.cfg, self.master, True)
                initial = self.start(n)
                out = self.quote(225)
                self.assertEqual([e['target'] for e in out], list(range(1, min(n,3)+1)))
                self.assertEqual(sum(e['sold_lots'] for e in out), n)
                self.assertEqual(self.pos()['stage'], min(n,3))
                self.assertEqual(self.pos()['remaining'], 0)
                self.assertEqual(Decimal(self.engine.state()['cash']),
                                 initial+(Decimal(225)-Decimal('170.5'))*10*n-25*(1+min(n,3)))
                self.assertEqual(self.quote(226), [])

    def test_t1_then_entry_stop_keeps_first_lot_profit(self):
        self.start(2)
        self.quote(180)
        self.restart()
        out = self.quote(170.5)
        self.assertEqual(out[0]['reason'], 'STOP')
        self.assertEqual(self.pos()['status'], 'CLOSED')
        self.assertEqual(Decimal(self.pos()['pnl']), 20)  # 95 gross less three 25 fees.
        self.assertEqual(self.quote(220), [])

    def test_t2_then_t1_stop_keeps_staged_profit(self):
        self.start(3)
        self.quote(180)
        self.quote(200)
        self.restart()
        self.assertEqual(self.quote(180)[0]['reason'], 'STOP')
        self.assertEqual(Decimal(self.pos()['pnl']), 385)

    def test_saved_legacy_allocations_and_historical_audit_are_not_rewritten(self):
        self.start(1)
        state = self.engine.state()
        state['positions']['-1000000000001:1']['allocations'] = [0,0,1]
        self.engine.db.execute('UPDATE state SET body=?', (json.dumps(state),))
        self.engine.db.commit()
        audit = self.engine.db.execute('SELECT * FROM audit').fetchall()
        self.restart()
        self.assertEqual(self.engine.db.execute('SELECT * FROM audit').fetchall(), audit)
        self.assertEqual(self.quote(180)[0]['sold_lots'], 0)
        self.assertEqual(self.pos()['remaining'], 1)
        self.assertEqual(self.quote(200)[0]['sold_lots'], 0)
        self.assertEqual(self.quote(220)[0]['sold_lots'], 1)
        self.assertEqual(self.pos()['status'], 'CLOSED')

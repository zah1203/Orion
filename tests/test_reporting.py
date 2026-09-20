import copy
from datetime import timedelta
import json
from pathlib import Path
import unittest
from orion.core import Engine, stamp
from orion.portal.reporting import report

ROOT = Path(__file__).resolve().parents[1]


class ReportingTests(unittest.TestCase):
    def setUp(self):
        cfg = json.loads((ROOT / "config/paper.json").read_text())
        master = json.loads((ROOT / "examples/instruments.synthetic.json").read_text())
        self.engine = Engine(":memory:", cfg, master, True)
        self.events = [json.loads(x) for x in (ROOT / "examples/replay.jsonl").read_text().splitlines()]

    def tearDown(self):
        self.engine.db.close()

    def run_to(self, count):
        for event in self.events[:count]:
            self.engine.process(event, stamp(event["source_time"]))
        self.now = stamp(self.events[count - 1]["source_time"])
        return report(self.engine.state(), 5, self.now)

    def test_entry_fee_and_bid_mark(self):
        r = self.run_to(3)["totals"]
        self.assertEqual((r["realized"], r["unrealized"], r["total"]), ("-25", "-15.0", "-40.0"))
        self.assertEqual(r["equity"], "99960.0")

    def test_partial_exit_does_not_double_count(self):
        r = self.run_to(4)
        t = r["totals"]
        self.assertEqual((t["realized"], t["unrealized"], t["total"]), ("45.0", "190.0", "235.0"))
        self.assertEqual(t["equity"], "100235.0")
        self.assertEqual(r["positions"][0]["remaining"], 2)

    def test_closed_trade_realized_only(self):
        r = self.run_to(6)["totals"]
        self.assertEqual((r["realized"], r["unrealized"], r["total"]), ("785.0", "0", "785.0"))
        self.assertEqual(r["equity"], r["cash"])

    def test_missing_mark_is_unavailable_not_zero(self):
        self.run_to(3)
        state = self.engine.state()
        state.pop("marks")
        r = report(state, 5, self.now)["totals"]
        self.assertIsNone(r["unrealized"])
        self.assertIsNone(r["total"])
        self.assertIsNone(r["equity"])
        self.assertEqual(r["realized"], "-25")
        self.assertEqual(r["missing_positions"], 1)

    def test_stale_mark_keeps_estimate_with_warning(self):
        self.run_to(3)
        r = report(self.engine.state(), 5, self.now + timedelta(seconds=6))["totals"]
        self.assertEqual(r["unrealized"], "-15.0")
        self.assertEqual(r["mark_status"], "stale")
        self.assertEqual(r["stale_positions"], 1)

    def test_closed_market_quote_is_not_fresh(self):
        self.run_to(3)
        state = self.engine.state()
        next(iter(state["marks"].values()))["market_open"] = False
        self.assertEqual(report(state, 5, self.now)["totals"]["mark_status"], "stale")

    def test_rejected_quote_does_not_change_mark(self):
        self.run_to(3)
        before = self.engine.state()["marks"]
        e = {
            **self.events[3],
            "event_id": "stale",
            "source_time": (self.now - timedelta(seconds=20)).isoformat(),
            "bid": "9999",
            "ask": "10000",
        }
        self.engine.process(e, self.now)
        self.assertEqual(self.engine.state()["marks"], before)

    def test_exact_products_and_contract_rows(self):
        self.run_to(4)
        state = self.engine.state()
        p = next(iter(state["positions"].values()))
        for name in ("GOLD", "GOLDM"):
            q = copy.deepcopy(p)
            q["contract"]["product"] = name
            q["contract"]["symbol"] = name + "-TEST"
            q["remaining"] = 0
            q["status"] = "CLOSED"
            q["pnl"] = "10"
            state["positions"][name] = q
        r = report(state, 5, self.now)
        self.assertEqual([x["product"] for x in r["instruments"]], ["GOLD", "GOLDM", "NIFTY"])
        self.assertEqual(len(r["positions"]), 3)
        self.assertEqual(r["totals"]["realized"], "65.0")

    def test_pending_signal_is_not_a_trade(self):
        r = self.run_to(1)
        self.assertEqual(r["positions"], [])
        self.assertEqual(r["instruments"], [])
        self.assertEqual(r["totals"]["trades"], 0)


if __name__ == "__main__":
    unittest.main()

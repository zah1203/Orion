import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from orion.core import Engine, allocations, parse_signal, resolve, stamp

ROOT = Path(__file__).resolve().parents[1]
SIGNAL = "NIFTY 23450 PE\nBUY 170\nTGT 180/200/220\nSL 155"
TIME = "2026-09-18T10:00:00+05:30"


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = json.loads((ROOT / "config/paper.json").read_text())
        self.master = json.loads((ROOT / "examples/instruments.synthetic.json").read_text())
        self.db = str(Path(self.tmp.name) / "state.db")
        self.engine = Engine(self.db, self.cfg, self.master, True)
        self.t = stamp(TIME)
        self.i = 0

    def tearDown(self):
        self.engine.db.close()
        self.tmp.cleanup()

    def msg(self, text=SIGNAL, **extra):
        e = dict(
            event_id="message",
            type="message",
            channel_id="-1000000000001",
            message_id="1",
            source_time=TIME,
            text=text,
        )
        e.update(extra)
        return self.engine.process(e, self.t)

    def quote(self, bid, ask=None, ltp=None, **extra):
        self.i += 1
        self.t += timedelta(seconds=1)
        e = dict(
            event_id=f"q{self.i}",
            type="quote",
            source_time=self.t.isoformat(),
            segment="nse_fo",
            token="TEST1",
            bid=str(bid),
            ask=str(ask if ask is not None else bid + 0.5),
            ltp=str(ltp if ltp is not None else bid),
            market_open=True,
        )
        e.update(extra)
        return self.engine.process(e, self.t)

    def pos(self):
        return self.engine.state()["positions"]["-1000000000001:1"]

    def enter(self):
        self.msg()
        self.quote(169)
        self.quote(170)
        self.assertEqual(self.pos()["status"], "OPEN")

    def test_three_lots_trail_and_close(self):
        self.enter()
        self.assertEqual(self.pos()["lots"], 3)
        self.quote(180)
        self.assertEqual((self.pos()["remaining"], self.pos()["stop"]), (2, "170.5"))
        self.quote(200)
        self.assertEqual((self.pos()["remaining"], self.pos()["stop"]), (1, "180"))
        self.quote(220)
        self.assertEqual(self.pos()["status"], "CLOSED")

    def test_one_lot_trails_without_partial(self):
        self.cfg["max_lots"] = 1
        self.enter()
        self.quote(180)
        self.assertEqual((self.pos()["remaining"], self.pos()["stop"]), (1, "170.5"))
        self.quote(200)
        self.assertEqual(self.pos()["stop"], "180")
        self.quote(179)
        self.assertEqual(self.pos()["status"], "CLOSED")

    def test_gap_hits_all_targets_once(self):
        self.enter()
        out = self.quote(225)
        self.assertEqual(len(out), 3)
        self.assertEqual(self.pos()["remaining"], 0)
        self.assertEqual(self.quote(226), [])

    def test_stop_gap_fills_at_bid_not_stop(self):
        self.enter()
        out = self.quote(140)
        self.assertEqual(out[0]["price"], "140")
        self.assertEqual(self.pos()["status"], "CLOSED")

    def test_restart_preserves_position_and_dedup(self):
        self.enter()
        before = self.engine.state()
        self.engine.db.close()
        self.engine = Engine(self.db, self.cfg, self.master, True)
        self.assertEqual(self.engine.state(), before)
        self.assertEqual(self.msg(), [])

    def test_repost_does_not_duplicate(self):
        self.msg()
        out = self.msg(event_id="other", message_id="2")
        self.assertEqual(out[0]["event"], "DUPLICATE_SIGNAL")

    def test_edit_cancels_pending(self):
        self.msg()
        self.msg(event_id="edit", edited=True)
        self.assertEqual(self.pos()["status"], "CANCELLED")

    def test_already_above_is_missed(self):
        self.msg()
        self.quote(180)
        self.assertEqual(self.pos()["status"], "MISSED")

    def test_stale_quote_cannot_trigger(self):
        self.msg()
        self.quote(169)
        self.quote(170, source_time=(self.t - timedelta(seconds=10)).isoformat())
        self.assertEqual(self.pos()["status"], "PENDING")

    def test_quote_before_signal_cannot_enter(self):
        self.msg(text=SIGNAL.replace("BUY 170", "BUY 169-171"))
        self.quote(170, source_time=(stamp(TIME) - timedelta(seconds=1)).isoformat())
        self.assertEqual(self.pos()["status"], "PENDING")

    def test_range(self):
        self.msg(text=SIGNAL.replace("BUY 170", "BUY 169-171"))
        self.quote(170)
        self.assertEqual(self.pos()["status"], "OPEN")

    def test_risk_budget_prevents_trade(self):
        self.cfg["risk_per_trade"] = "10"
        self.msg()
        self.quote(169)
        self.quote(170)
        self.assertEqual(self.pos()["status"], "REJECTED")

    def test_closed_market_blocks_entry(self):
        self.msg()
        self.quote(169)
        self.quote(170, market_open=False)
        self.assertEqual(self.pos()["status"], "PENDING")

    def test_stale_signal(self):
        self.t += timedelta(minutes=3)
        self.assertEqual(self.msg()[0]["event"], "STALE_SIGNAL")

    def test_incomplete_commentary_rejected(self):
        for text in (
            "Active",
            "180",
            "Above 195 take new entry with sl 180 Tgt 205/220/240",
            "Below 23300 again new entry Tgt 23260 and sl 23320",
        ):
            with self.assertRaises(ValueError):
                parse_signal(text, TIME)

    def test_watchlist_rejected(self):
        with self.assertRaises(ValueError):
            parse_signal("Watchlist\n" + SIGNAL, TIME)

    def test_commodity_explicit_expiry_and_product(self):
        s = parse_signal(
            "Enter: GOLDM 25 SEP 153000 CALL\nAction: BUY\nTrade Type: BTST || Options Buying\nEntry Price Range: 2136.4 - 2223.6\nStop Loss: 1900.0\nTarget 1: 2350.0\nTarget 2: 2500.0\nTarget 3: 3200.0",
            TIME,
        )
        self.assertEqual(
            (s["product"], s["expiry"], s["strike"], s["option_type"]),
            ("GOLDM", "2026-09-25", "153000", "CE"),
        )
        self.assertTrue(s["overnight"])

    def test_btst_is_review_only(self):
        self.assertEqual(self.msg(text=SIGNAL + "\nBTST")[0]["event"], "REVIEW_OR_COMMENTARY")

    def test_nearest_expiry_exact_strike(self):
        s = parse_signal(SIGNAL, TIME)
        later = copy.deepcopy(self.master["contracts"][0])
        later.update(expiry="2026-09-29", expiry_at="2026-09-29T15:30:00+05:30", token="LATER")
        self.master["contracts"].insert(0, later)
        self.assertEqual(resolve(s, self.master, self.t, True)["token"], "TEST1")
        s["strike"] = "23500"
        with self.assertRaises(ValueError):
            resolve(s, self.master, self.t, True)

    def test_explicit_expiry_no_fallback(self):
        s = parse_signal(SIGNAL, TIME)
        s["expiry"] = "2026-09-29"
        with self.assertRaises(ValueError):
            resolve(s, self.master, self.t, True)

    def test_ambiguous_master_rejected(self):
        self.master["contracts"] *= 2
        with self.assertRaises(ValueError):
            resolve(parse_signal(SIGNAL, TIME), self.master, self.t, True)

    def test_synthetic_forbidden_for_live_inputs(self):
        with self.assertRaises(ValueError):
            resolve(parse_signal(SIGNAL, TIME), self.master, self.t)

    def test_old_master_rejected(self):
        self.master["as_of"] = "2026-09-17"
        with self.assertRaises(ValueError):
            resolve(parse_signal(SIGNAL, TIME), self.master, self.t, True)

    def test_live_execution_unavailable(self):
        self.cfg["mode"] = "live"
        with self.assertRaises(ValueError):
            Engine(":memory:", self.cfg, self.master)

    def test_whole_lot_allocations(self):
        self.assertEqual(
            [allocations(n) for n in range(1, 7)],
            [[0, 0, 1], [1, 0, 1], [1, 1, 1], [1, 1, 2], [1, 2, 2], [2, 2, 2]],
        )


if __name__ == "__main__":
    unittest.main()

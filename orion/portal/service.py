"""Per-user engine state. Only trusted workers can supply market events."""

from datetime import datetime, timezone
import json
import sqlite3
from ..core import Engine, dumps, stamp, dec
from .reporting import report


class Accounts:
    def __init__(self, store, template):
        self.store = store
        self.template = template

    def engine(self, uid, master, synthetic=False):
        return Engine(
            str(self.store.account_dir(uid) / "paper.db"), self.store.config(uid), master, synthetic
        )

    def summary(self, uid):
        with self.store.lock(uid):
            user = self.store.user(uid)
            path = self.store.account_dir(uid) / "paper.db"
            state = {"cash": user["settings"]["paper_cash"], "positions": {}, "days": {}}
            history = []
            if path.exists():
                db = sqlite3.connect(path)
                try:
                    row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
                    if row:
                        state = json.loads(row[0])
                    history = [
                        {"at": r[0], "event": r[1], **json.loads(r[2])}
                        for r in db.execute("SELECT at,event,body FROM audit ORDER BY seq DESC LIMIT 100")
                    ]
                finally:
                    db.close()
            return {
                **user,
                "credentials": self.store.credential_status(uid),
                "worker_online": self.store.worker_alive(uid),
                "state": state,
                "history": history,
                "pnl": report(state, user["settings"]["quote_max_age_seconds"]),
                "mode": "paper",
            }

    def active_positions(self, uid):
        path = self.store.account_dir(uid) / "paper.db"
        if not path.exists():
            return False
        db = sqlite3.connect(path)
        try:
            row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
            return bool(
                row
                and any(p["status"] in ("PENDING", "OPEN") for p in json.loads(row[0])["positions"].values())
            )
        finally:
            db.close()

    def set_enabled(self, uid, enabled):
        with self.store.lock(uid):
            self.store.set_enabled(uid, enabled)
            if not enabled:
                path = self.store.account_dir(uid) / "paper.db"
                if path.exists():
                    db = sqlite3.connect(path)
                    try:
                        with db:
                            db.execute("BEGIN IMMEDIATE")
                            row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
                            if row:
                                state = json.loads(row[0])
                                for p in state["positions"].values():
                                    if p["status"] == "PENDING":
                                        p["status"] = "CANCELLED"
                                db.execute("UPDATE state SET body=? WHERE id=1", (dumps(state),))
                                db.execute(
                                    "INSERT INTO audit(at,event,body) VALUES(?,?,?)",
                                    (datetime.now(timezone.utc).isoformat(), "ENTRIES_PAUSED", "{}"),
                                )
                    finally:
                        db.close()

    def save_settings(self, uid, settings):
        with self.store.lock(uid):
            if (
                self.store.user(uid)["enabled"]
                or self.store.worker_running(uid)
                or self.active_positions(uid)
            ):
                raise ValueError(
                    "Pause entries, stop the worker and finish open paper trades before changing settings"
                )
            # Starting paper capital cannot rewrite an existing ledger.
            if (self.store.account_dir(uid) / "paper.db").exists() and dec(settings["paper_cash"]) != dec(
                self.store.user(uid)["settings"]["paper_cash"]
            ):
                raise ValueError("Starting capital is fixed after the first paper event")
            self.store.save_settings(uid, settings)

    def process(self, uid, event, master, now=None, synthetic=False):
        """Internal interface, never exposed as a browser endpoint."""
        with self.store.lock(uid):
            engine = self.engine(uid, master, synthetic)
            try:
                return engine.process(event, now)
            finally:
                engine.db.close()

    def demo(self, uid):
        """A separate disposable synthetic engine, never the user's ongoing ledger."""
        cfg = self.store.config(uid)
        cfg["new_entries_enabled"] = True
        cfg["channels"] = {
            "-1000000000001": {"products": ["NIFTY"], "exit_time_ist": "15:15", "allow_overnight": False}
        }
        master = {
            "synthetic": True,
            "as_of": "2026-09-18",
            "contracts": [
                {
                    "product": "NIFTY",
                    "strike": "23450",
                    "option_type": "PE",
                    "segment": "nse_fo",
                    "expiry": "2026-09-22",
                    "expiry_at": "2026-09-22T15:30:00+05:30",
                    "symbol": "DEMO-NIFTY-23450-PE",
                    "token": "DEMO",
                    "order_quantity_per_lot": 1,
                    "premium_multiplier": "10",
                    "tick_size": "0.05",
                }
            ],
        }
        engine = Engine(":memory:", cfg, master, True)
        events = [
            {
                "type": "message",
                "event_id": "demo",
                "channel_id": "-1000000000001",
                "message_id": "1",
                "source_time": "2026-09-18T10:00:00+05:30",
                "text": "NIFTY 23450 PE\nBUY 170\nTGT 180/200/220\nSL 155",
            }
        ]
        for i, price in enumerate((169, 170, 180, 200, 220), 1):
            events.append(
                dict(
                    type="quote",
                    event_id=str(i),
                    source_time=f"2026-09-18T10:00:0{i}+05:30",
                    segment="nse_fo",
                    token="DEMO",
                    bid=str(price),
                    ask=str(price + 0.5),
                    ltp=str(price),
                    market_open=True,
                )
            )
        out = []
        try:
            for e in events:
                out.extend(engine.process(e, stamp(e["source_time"])))
            return {
                "synthetic": True,
                "note": "Fictional contract economics; uses your risk limits with a fixed NIFTY example. Does not change your ledger or channel settings.",
                "events": out,
                "state": engine.state(),
            }
        finally:
            engine.db.close()

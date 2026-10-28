"""Per-user engine state. Only trusted workers can supply market events."""

from datetime import datetime, timezone
import json
import sqlite3
from contextlib import closing
from ..core import Engine, dumps, stamp, dec
from .reporting import report
from .analytics import enrich
from . import paper_periods


class Accounts:
    def __init__(self, store, template):
        self.store = store
        self.template = template

    def engine(self, uid, master, synthetic=False):
        return Engine(
            str(self.store.account_dir(uid) / "paper.db"), self.store.config(uid), master, synthetic
        )

    def capital(self, uid):
        """One read snapshot, no Engine construction or production mutation."""
        from .capital import reconcile
        with self.store.lock(uid):
            user = self.store.user(uid)
            path = self.store.account_dir(uid) / 'paper.db'
            if not path.exists():
                return dict(status='not-started', adjustments=[], issues=[],
                            order_submission_available=False)
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError('Paper ledger requires review')
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    db.execute('PRAGMA query_only=ON')
                    db.execute('BEGIN')
                    row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
                    if not row:
                        raise ValueError
                    state = json.loads(row[0])
                    records = [dict(seq=r[0], at=r[1], event=r[2], body=json.loads(r[3]))
                               for r in db.execute("SELECT seq,at,event,body FROM audit WHERE event IN "
                                   "('PAPER_BALANCE_ADJUSTED','PAPER_BUY','PAPER_EXIT','TARGET_REACHED') ORDER BY seq")]
            except (sqlite3.Error, ValueError, TypeError):
                raise ValueError('Paper ledger cannot be reconciled; review required') from None
        try:
            period = paper_periods.metadata(state)
            records = [r for r in records if r['seq'] > period['start_seq']]
            result = reconcile(paper_periods.view(state), records,
                               period.get('starting_cash', user['settings']['paper_cash']))
            result['paper_period'] = {k: v for k, v in period.items() if k != 'excluded_signals'}
            if period['id'] != 'legacy':
                result['starting_cash_basis'] = 'paper-period-starting-balance'
                result['note'] = 'Current paper period only. ' + result['note'].replace('configured starting cash', 'period starting cash')
            return result
        except (ValueError, KeyError, TypeError, ArithmeticError):
            raise ValueError('Paper ledger cannot be reconciled; review required') from None

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
                        for r in db.execute("SELECT at,event,body FROM audit WHERE seq>=? ORDER BY seq DESC LIMIT 100",
                                          (paper_periods.metadata(state)["start_seq"],))
                    ]
                finally:
                    db.close()
            state = paper_periods.view(state)
            return {
                **user,
                "credentials": self.store.credential_status(uid),
                "worker_online": self.store.worker_alive(uid),
                "worker_health": self.store.health(uid),
                "state": state,
                "history": history,
                "pnl": enrich(report(state, user["settings"]["quote_max_age_seconds"]), state, path, user["settings"].get("channels", {})),
                "mode": "paper",
                "ledger_started": path.exists(),
            }

    def performance(self, uid, paper_period='current', **filters):
        from .performance import performance
        with self.store.lock(uid):
            user = self.store.user(uid)
            path = self.store.account_dir(uid) / "paper.db"
            state = {"cash": user["settings"]["paper_cash"], "positions": {}, "days": {}}
            records = []
            selected = paper_periods.metadata(state)
            options, archived = [selected], False
            if path.exists():
                with closing(sqlite3.connect(path)) as db:
                    db.execute("BEGIN")
                    row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
                    if row:
                        state = json.loads(row[0])
                    state, records, selected, options, archived = paper_periods.select(db, state, paper_period)
            elif paper_period not in ('current', 'legacy'):
                raise ValueError('Unknown paper testing period')
            result = performance(state, records, user["settings"]["quote_max_age_seconds"], **filters)
            result.update(paper_period=selected, paper_periods=options, archived=archived)
            return result

    def start_paper_period(self, uid, amount, label, expected_period):
        with self.store.lock(uid):
            if self.store.user(uid)["enabled"]:
                raise ValueError('Pause entries before starting a new paper period')
            engine = self.engine(uid, {"synthetic": False, "contracts": []})
            try:
                with engine.db:
                    engine.db.execute('BEGIN IMMEDIATE')
                    return paper_periods.start(engine.db, engine.state(), amount, label,
                                               expected_period, self.store.user(uid)['settings'])
            finally:
                engine.db.close()

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

    def trade_details(self, uid, signal_id, offset=0, chart_window='1D'):
        from .trade_details import details
        if offset < 0 or offset > 1000000:
            raise ValueError("Invalid timeline offset")
        with self.store.lock(uid):
            user = self.store.user(uid)
            return details(self.store.account_dir(uid) / "paper.db", signal_id,
                           user["settings"].get("channels", {}), offset,
                           quote_age=user["settings"]["quote_max_age_seconds"], chart_window=chart_window)

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
                or self.active_positions(uid)
            ):
                raise ValueError(
                    "Pause entries and finish open paper trades before changing settings. The worker can stay online."
                )
            # Starting paper capital cannot rewrite an existing ledger.
            if (self.store.account_dir(uid) / "paper.db").exists() and dec(settings["paper_cash"]) != dec(
                self.store.user(uid)["settings"]["paper_cash"]
            ):
                raise ValueError("Starting capital is fixed after the first paper event. Use Adjust paper balance instead.")
            self.store.save_settings(uid, settings)

    def adjust_balance(self, uid, amount, reason):
        amount = dec(amount)
        if not 0 <= amount <= 100000000 or amount != amount.quantize(dec("0.01")):
            raise ValueError("Enter a balance between 0 and 100,000,000 with at most two decimals")
        reason = reason.strip()
        if not reason or len(reason) > 200:
            raise ValueError("Enter a reason of 1–200 characters")
        with self.store.lock(uid):
            if self.store.user(uid)["enabled"] or self.active_positions(uid):
                raise ValueError("Pause entries and finish open paper trades before adjusting the balance")
            engine = self.engine(uid, {"synthetic": False, "contracts": []})
            try:
                with engine.db:
                    state = engine.state()
                    before = dec(state["cash"])
                    if before == amount:
                        return  # Retrying the same target balance does not duplicate adjustments.
                    state["cash"] = str(amount)
                    engine.db.execute("UPDATE state SET body=? WHERE id=1", (dumps(state),))
                    engine.db.execute(
                        "INSERT INTO audit(at,event,body) VALUES(?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), "PAPER_BALANCE_ADJUSTED",
                         dumps({"before": before, "after": amount, "adjustment": amount - before,
                                "reason": reason})),
                    )
            finally:
                engine.db.close()

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

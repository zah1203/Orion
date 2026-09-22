"""Deterministic long-option paper engine. No broker order submission in this module."""

from __future__ import annotations
import calendar
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def dec(x):
    value = Decimal(str(x))
    if not value.is_finite():
        raise ValueError("Non-finite number")
    return value


def stamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt


def dumps(value):
    return json.dumps(value, default=str, sort_keys=True)


NUM = r"(\d+(?:\.\d+)?)"
PRODUCT = r"(BANKNIFTY|NIFTY|GOLDM|GOLD|SILVERM|SILVER|CRUDEOILM|CRUDEOIL)"
CONTRACT = re.compile(
    r"\b" + PRODUCT + r"\s+(?:(\d{1,2})\s+([A-Z]{3})(?:\s+(20\d{2}))?\s+)?" + NUM + r"\s*(CE|PE|CALL|PUT)\b"
)


def contract_identity(text, source_time):
    """Return one explicit option identity without inferring a missing contract."""
    matches = list(CONTRACT.finditer(text.upper().replace(",", "")))
    if len(matches) != 1:
        raise ValueError("Expected one explicit option contract")
    product, day, month, year, strike, kind = matches[0].groups()
    expiry = None
    if day:
        month_num = {v.upper(): i for i, v in enumerate(calendar.month_abbr) if v}.get(month)
        if not month_num:
            raise ValueError("Invalid expiry month")
        expiry = (
            datetime(int(year or stamp(source_time).astimezone(IST).year), month_num, int(day))
            .date()
            .isoformat()
        )
    return {
        "product": product,
        "strike": str(dec(strike)),
        "option_type": {"CALL": "CE", "PUT": "PE"}.get(kind, kind),
        "expiry": expiry,
    }


def parse_exit(text, source_time):
    """Recognize explicit provider exits while requiring the full option identity."""
    t = text.upper().replace(",", "")
    if not re.search(r"\b(?:CLOSE THIS POSITION|STOP\s*LOSS HIT|STOPLOSS HIT|SL HIT|EXIT NOW)\b", t):
        raise ValueError("Not an explicit exit")
    return contract_identity(t, source_time)


def parse_signal(text, source_time):
    """Only complete BUY signals; no implicit link to an earlier contract."""
    t = text.upper().replace(",", "")
    if re.search(r"\bWATCHLIST\b", t):
        raise ValueError("Watchlist, not a signal")
    identity = contract_identity(t, source_time)
    if re.search(r"\bACTION\s*:\s*SELL\b|\bSELL\s+\d", t):
        raise ValueError("Option selling not supported")
    entry = re.search(r"ENTRY PRICE RANGE\s*:\s*" + NUM + r"\s*[-–]\s*" + NUM, t)
    mode = "range"
    if entry:
        if not re.search(r"\bACTION\s*:\s*BUY\b", t):
            raise ValueError("Explicit BUY required")
        low, high = map(dec, entry.groups())
    else:
        entry = re.search(r"\bBUY\s+(ABOVE\s+)?" + NUM + r"(?:\s*[-–]\s*" + NUM + r")?", t)
        if not entry:
            raise ValueError("Missing entry")
        above, a, b = entry.groups()
        low, high = dec(a), dec(b or a)
        mode = "above" if above else ("range" if b else "cross")
    stop = re.search(r"\b(?:SL|STOP LOSS)\s*:?\s*" + NUM, t)
    compact = re.search(r"\bTGT\s*:?\s*" + NUM + r"\s*/\s*" + NUM + r"\s*/\s*" + NUM, t)
    if compact:
        targets = list(map(dec, compact.groups()))
    else:
        targets = []
        for i in (1, 2, 3):
            target = re.search(r"\bTARGET\s*" + str(i) + r"\s*:\s*" + NUM, t)
            if not target:
                raise ValueError("Three targets required")
            targets.append(dec(target.group(1)))
    if not stop:
        raise ValueError("Stop required")
    sl = dec(stop.group(1))
    if not (0 < sl < low <= high < targets[0] < targets[1] < targets[2]):
        raise ValueError("Invalid price ordering")
    return dict(
        **identity,
        entry_low=str(low),
        entry_high=str(high),
        entry_mode=mode,
        stop=str(sl),
        targets=list(map(str, targets)),
        overnight="BTST" in t,
    )


def resolve(signal, master, now, allow_synthetic=False):
    if master.get("synthetic") and not allow_synthetic:
        raise ValueError("Synthetic instrument file forbidden with live data")
    if master["as_of"] != now.astimezone(IST).date().isoformat():
        raise ValueError("Instrument master must be verified for this IST date")
    found = []
    for c in master["contracts"]:
        if (
            c["product"] != signal["product"]
            or dec(c["strike"]) != dec(signal["strike"])
            or c["option_type"] != signal["option_type"]
        ):
            continue
        expected_segment = "nse_fo" if c["product"] in ("NIFTY", "BANKNIFTY") else "mcx_fo"
        if c["segment"] != expected_segment:
            continue
        if stamp(c["expiry_at"]) <= now or (signal["expiry"] and c["expiry"] != signal["expiry"]):
            continue
        if c["expiry"] != stamp(c["expiry_at"]).astimezone(IST).date().isoformat():
            raise ValueError("Inconsistent expiry metadata")
        if (
            int(c["order_quantity_per_lot"]) <= 0
            or dec(c["premium_multiplier"]) <= 0
            or dec(c["tick_size"]) <= 0
        ):
            raise ValueError("Invalid sizing metadata")
        found.append(c)
    if not found:
        raise ValueError("No exact contract")
    nearest = min(c["expiry"] for c in found)
    found = [c for c in found if c["expiry"] == nearest]
    if len(found) != 1:
        raise ValueError("Ambiguous contract")
    return found[0].copy()


def allocations(n):
    if n <= 0:
        raise ValueError("Lots must be positive")
    if n == 1:
        return [0, 0, 1]
    if n == 2:
        return [1, 0, 1]
    a = n // 3
    b = (n - a) // 2
    return [a, b, n - a - b]


class Engine:
    """One process, transactional SQLite state, synthetic full fills at bid/ask.

    Paper results exclude real liquidity/queue effects; this is NOT a live broker.
    """

    def __init__(self, db, config, master, allow_synthetic=False):
        if config["mode"] != "paper":
            raise ValueError("This release has no live order execution")
        self.cfg, self.master, self.allow_synthetic = config, master, allow_synthetic
        self.db = sqlite3.connect(db)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, at TEXT, event TEXT, body TEXT);
        """)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO state VALUES(1,?)",
                (
                    dumps(
                        {
                            "cash": config["paper_cash"],
                            "positions": {},
                            "fingerprints": [],
                            "days": {},
                            "last_quotes": {},
                        }
                    ),
                ),
            )

    def state(self):
        return json.loads(self.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])

    def process(self, event, now=None):
        now = now or datetime.now(timezone.utc)
        out = []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            identity = event["event_id"]
            if self.db.execute("SELECT 1 FROM seen WHERE id=?", (identity,)).fetchone():
                return []
            s = self.state()

            def emit(kind, **body):
                out.append({"event": kind, **body})
                self.db.execute(
                    "INSERT INTO audit(at,event,body) VALUES(?,?,?)", (now.isoformat(), kind, dumps(body))
                )

            if event["type"] == "message":
                self._message(s, event, now, emit)
            elif event["type"] == "quote":
                self._quote(s, event, now, emit)
            elif event["type"] == "heartbeat":
                for key, p in s["positions"].items():
                    if p["status"] in ("PENDING", "OPEN"):
                        emit("NEEDS_QUOTE", signal_id=key, contract=p["contract"]["symbol"])
            else:
                raise ValueError("Unsupported event")
            self.db.execute("INSERT INTO seen VALUES(?)", (identity,))
            self.db.execute("UPDATE state SET body=? WHERE id=1", (dumps(s),))
        return out

    def _message(self, s, e, now, emit):
        channel = self.cfg["channels"].get(str(e["channel_id"]))
        if not channel:
            return emit("IGNORED_CHANNEL")
        key = f"{e['channel_id']}:{e['message_id']}"
        # Any edit invalidates a pending signal; never silently changes an open trade.
        if e.get("edited"):
            p = s["positions"].get(key)
            if p and p["status"] == "PENDING":
                p["status"] = "CANCELLED"
            return emit("EDIT_REQUIRES_REVIEW", signal_id=key)
        if key in s["positions"]:
            return emit("DUPLICATE_MESSAGE", signal_id=key)
        try:
            closing = parse_exit(e["text"], e["source_time"])
        except ValueError:
            closing = None
        if closing:
            matches = []
            for signal_id, position in s["positions"].items():
                if signal_id.split(":")[0] != str(e["channel_id"]):
                    continue
                if position["status"] not in ("PENDING", "OPEN"):
                    continue
                if any(
                    position.get(field) != closing[field]
                    for field in ("product", "strike", "option_type")
                ):
                    continue
                if closing["expiry"] and position["contract"]["expiry"] != closing["expiry"]:
                    continue
                matches.append((signal_id, position))
            if len(matches) != 1:
                return emit("EXIT_REQUIRES_REVIEW", signal_id=key, matches=len(matches))
            signal_id, position = matches[0]
            if position["status"] == "PENDING":
                position["status"] = "CANCELLED"
                return emit("PENDING_CANCELLED_BY_PROVIDER", signal_id=signal_id)
            position["exit_requested"] = True
            return emit("PROVIDER_EXIT_PENDING_QUOTE", signal_id=signal_id)
        if not self.cfg.get("new_entries_enabled", True):
            return emit("ENTRIES_PAUSED", signal_id=key)
        age = (now - stamp(e["source_time"])).total_seconds()
        if age < -5 or age > self.cfg["signal_max_age_seconds"]:
            return emit("STALE_SIGNAL", signal_id=key)
        try:
            signal = parse_signal(e["text"], e["source_time"])
            if signal["product"] not in channel["products"]:
                raise ValueError("Product not allowed for this channel")
            if signal["overnight"] and not channel.get("allow_overnight", False):
                raise ValueError("BTST is disabled for this channel")
            c = resolve(signal, self.master, now, self.allow_synthetic)
        except (ValueError, KeyError) as exc:
            return emit("REVIEW_OR_COMMENTARY", signal_id=key, reason=str(exc))
        fingerprint = hashlib.sha256(
            dumps([str(e["channel_id"]), now.astimezone(IST).date().isoformat(), signal]).encode()
        ).hexdigest()
        if fingerprint in s["fingerprints"]:
            return emit("DUPLICATE_SIGNAL", signal_id=key)
        s["fingerprints"].append(fingerprint)
        signal.update(
            contract=c,
            status="PENDING",
            source_time=e["source_time"],
            last_ltp=None,
            lots=0,
            remaining=0,
            stage=0,
            entry_fill=None,
            entry_time=None,
            entry_date_ist=None,
            exit_requested=False,
            allocations=[],
            pnl="0",
        )
        s["positions"][key] = signal
        emit("SIGNAL_PENDING", signal_id=key, contract=c["symbol"], expiry=c["expiry"])

    def _quote(self, s, e, now, emit):
        qtime = stamp(e["source_time"])
        age = (now - qtime).total_seconds()
        if age < -2 or age > self.cfg["quote_max_age_seconds"]:
            return emit("STALE_QUOTE", token=e["token"])
        token_key = e["segment"] + ":" + str(e["token"])
        if token_key in s["last_quotes"] and qtime <= stamp(s["last_quotes"][token_key]):
            return emit("OUT_OF_ORDER_QUOTE", token=e["token"])
        bid, ask, ltp = map(dec, (e["bid"], e["ask"], e["ltp"]))
        if not (0 < bid <= ask and ltp > 0):
            return emit("INVALID_QUOTE", token=e["token"])
        s["last_quotes"][token_key] = e["source_time"]
        s.setdefault("marks", {})[token_key] = {
            "bid": str(bid),
            "source_time": e["source_time"],
            "market_open": e.get("market_open", False),
        }
        today = now.astimezone(IST).date().isoformat()
        day = s["days"].setdefault(today, {"pnl": "0", "entries": 0})
        for key, p in s["positions"].items():
            c = p["contract"]
            if c["segment"] != e["segment"] or str(c["token"]) != str(e["token"]):
                continue
            if p["status"] not in ("PENDING", "OPEN"):
                continue
            expiry_cutoff = (stamp(c["expiry_at"]) - now).total_seconds() <= self.cfg[
                "expiry_exit_buffer_seconds"
            ]
            local_time = now.astimezone(IST).strftime("%H:%M")
            cutoff = local_time >= self.cfg["channels"][key.split(":")[0]]["exit_time_ist"]
            if p["status"] == "PENDING":
                if not self.cfg.get("new_entries_enabled", True):
                    p["status"] = "CANCELLED"
                    emit("ENTRIES_PAUSED", signal_id=key)
                    continue
                signal_date = stamp(p["source_time"]).astimezone(IST).date().isoformat()
                pending_expired = (
                    (today != signal_date or cutoff)
                    if p.get("overnight")
                    else (now - stamp(p["source_time"])).total_seconds()
                    > self.cfg["signal_max_age_seconds"]
                )
                if expiry_cutoff or pending_expired:
                    p["status"] = "CANCELLED"
                    emit("SIGNAL_EXPIRED", signal_id=key)
                    continue
                if qtime < stamp(p["source_time"]):
                    continue
                if not e.get("market_open", False):
                    continue
                threshold = dec(p["entry_low"]) + (
                    dec(self.cfg["above_buffer"]) if p["entry_mode"] == "above" else 0
                )
                if p["entry_mode"] == "range":
                    trigger = dec(p["entry_low"]) <= ask <= dec(p["entry_high"])
                    cap = dec(p["entry_high"])
                else:
                    prior = p["last_ltp"]
                    p["last_ltp"] = str(ltp)
                    if prior is None and ltp >= threshold:
                        p["status"] = "MISSED"
                        emit("ALREADY_TRIGGERED", signal_id=key)
                        continue
                    trigger = prior is not None and dec(prior) < threshold <= ltp
                    cap = threshold + dec(self.cfg["entry_slippage_cap"])
                if not trigger:
                    continue
                if ask > cap or ask >= dec(p["targets"][0]):
                    p["status"] = "MISSED"
                    emit("ENTRY_PRICE_REJECTED", signal_id=key)
                    continue
                spread_ok = (ask - bid) / ask <= dec(self.cfg["max_spread_fraction"])
                active = sum(v["status"] == "OPEN" for v in s["positions"].values())
                if (
                    not spread_ok
                    or active >= self.cfg["max_open_positions"]
                    or dec(day["pnl"]) <= -dec(self.cfg["daily_loss_limit"])
                    or day["entries"] >= self.cfg["max_entries_per_day"]
                ):
                    p["status"] = "REJECTED"
                    emit("ENTRY_LIMIT_REJECTED", signal_id=key)
                    continue
                multiplier = dec(c["premium_multiplier"])
                fee = dec(self.cfg["paper_fee_per_order"])
                risk_lot = (ask - dec(p["stop"])) * multiplier
                existing_risk = sum(
                    max(Decimal(0), dec(v["entry_fill"]) - dec(v["stop"]))
                    * dec(v["contract"]["premium_multiplier"])
                    * v["remaining"]
                    for v in s["positions"].values()
                    if v["status"] == "OPEN"
                )
                risk_budget = min(
                    dec(self.cfg["risk_per_trade"]),
                    max(Decimal(0), dec(self.cfg["max_open_risk"]) - existing_risk),
                )
                lots = min(
                    self.cfg["max_lots"],
                    int(max(Decimal(0), risk_budget - 4 * fee) / risk_lot),
                    int(max(Decimal(0), dec(s["cash"]) - fee) / (ask * multiplier)),
                )
                if lots < 1:
                    p["status"] = "REJECTED"
                    emit("INSUFFICIENT_BUDGET", signal_id=key)
                    continue
                p.update(
                    status="OPEN",
                    lots=lots,
                    remaining=lots,
                    entry_fill=str(ask),
                    entry_time=qtime.isoformat(),
                    entry_date_ist=qtime.astimezone(IST).date().isoformat(),
                    allocations=allocations(lots),
                    pnl=str(-fee),
                )
                s["cash"] = str(dec(s["cash"]) - ask * multiplier * lots - fee)
                day["pnl"] = str(dec(day["pnl"]) - fee)
                day["entries"] += 1
                emit("PAPER_BUY", signal_id=key, lots=lots, price=str(ask), stop=p["stop"])
                continue
            if not e.get("market_open", False):
                emit("MARKET_CLOSED_POSITION", signal_id=key)
                continue
            overnight_cutoff = p.get("overnight") and today > p.get("entry_date_ist", today) and cutoff
            regular_cutoff = not p.get("overnight") and cutoff
            if p.get("exit_requested") or expiry_cutoff or overnight_cutoff or regular_cutoff or bid <= dec(p["stop"]):
                self._sell(s, p, p["remaining"], bid, day)
                emit(
                    "PAPER_EXIT",
                    signal_id=key,
                    reason=(
                        "PROVIDER"
                        if p.get("exit_requested")
                        else "CUTOFF"
                        if expiry_cutoff or overnight_cutoff or regular_cutoff
                        else "STOP"
                    ),
                    price=str(bid),
                    pnl=p["pnl"],
                )
                continue
            for i, target in enumerate(p["targets"]):
                if i < p["stage"] or bid < dec(target):
                    continue
                amount = p["allocations"][i]
                if amount:
                    self._sell(s, p, amount, bid, day)
                p["stage"] = i + 1
                if p["remaining"]:
                    new_stop = dec(p["entry_fill"]) if i == 0 else dec(p["targets"][i - 1])
                    tick = dec(c["tick_size"])
                    rounded = (new_stop / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
                    p["stop"] = str(max(dec(p["stop"]), rounded))
                emit(
                    "TARGET_REACHED",
                    signal_id=key,
                    target=i + 1,
                    sold_lots=amount,
                    remaining=p["remaining"],
                    stop=p["stop"],
                    price=str(bid),
                )

    def _sell(self, s, p, lots, price, day):
        if not 0 < lots <= p["remaining"]:
            raise ValueError("Invalid exit quantity")
        mult = dec(p["contract"]["premium_multiplier"])
        fee = dec(self.cfg["paper_fee_per_order"])
        pnl = (price - dec(p["entry_fill"])) * mult * lots - fee
        p["pnl"] = str(dec(p["pnl"]) + pnl)
        day["pnl"] = str(dec(day["pnl"]) + pnl)
        s["cash"] = str(dec(s["cash"]) + price * mult * lots - fee)
        p["remaining"] -= lots
        if p["remaining"] == 0:
            p["status"] = "CLOSED"

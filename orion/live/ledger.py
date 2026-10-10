"""Durable order intents and conservative risk reservations; no network/SDK imports.

This is a development foundation, not an executable trading strategy. A future
adapter must reconcile full broker state and protective exits before activation.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


class Refused(ValueError):
    """Safe, non-secret validation failure."""


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Refused("Invalid amount") from None
    if not result.is_finite() or result < 0:
        raise Refused("Invalid amount")
    return result


def positive_int(value):
    if type(value) is not int or value < 1:
        raise Refused("Positive integer required")
    return value


@dataclass(frozen=True)
class Limits:
    max_lots: int
    max_entries: int
    max_premium: Decimal
    daily_loss: Decimal
    max_age_seconds: int = 30

    def __post_init__(self):
        for v in (self.max_lots, self.max_entries, self.max_age_seconds):
            positive_int(v)
        for name in ("max_premium", "daily_loss"):
            v = amount(getattr(self, name))
            if not v:
                raise Refused("Limit must be positive")
            object.__setattr__(self, name, v)


class Ledger:
    def __init__(self, path, account):
        if not re.fullmatch(r"[0-9a-f]{32}", account):
            raise Refused("Invalid account")
        path = Path(path)
        if path.name != "live.db" or path.is_symlink():
            raise Refused("Separate live.db required")
        if any(p.is_symlink() for p in path.parents):
            raise Refused("Symlink parent forbidden")
        # Never turn an isolated recovery tree into an execution ledger.
        if any((p / "RECOVERY_ONLY").exists() for p in path.parents):
            raise Refused("Recovery destination forbidden")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() and path.stat().st_mode & 0o077:
            raise Refused("Private ledger required")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.account = account
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(account TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS intents(
                tag TEXT PRIMARY KEY, event TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL, broker_id TEXT UNIQUE, filled INTEGER NOT NULL DEFAULT 0,
                average TEXT NOT NULL DEFAULT '0');
            INSERT OR IGNORE INTO control VALUES(1,1);
        ''')
        with self.transaction():
            row = self.db.execute("SELECT account FROM metadata").fetchone()
            if row and row[0] != account:
                raise Refused("Account mismatch")
            if not row:
                self.db.execute("INSERT INTO metadata VALUES(?)", (account,))

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise

    def pause(self):
        # Only closes the entry gate. No liquidation or order cancellations.
        self.db.execute("UPDATE control SET paused=1 WHERE id=1")

    def development_resume(self):
        """Offline test harness only; no API route or production worker calls this."""
        with self.transaction():
            if self.db.execute("SELECT 1 FROM intents WHERE status IN ('DISPATCHING','UNKNOWN')").fetchone():
                raise Refused("Reconciliation required")
            self.db.execute("UPDATE control SET paused=0 WHERE id=1")

    def reserve(self, *, event, symbol, segment, lots, lot_size, limit_price, tick_size,
                signal_time, quote_time, now, limits, realized_loss):
        """Reserve full entry premium, not merely intended stop-loss risk.

        Loss is an injected development snapshot, not broker-verified live data.
        A production adapter must supply complete/fresh reconciliation atomically.
        """
        if not isinstance(event, str) or not 1 <= len(event) <= 128:
            raise Refused("Invalid event")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9_.-]{1,80}", symbol):
            raise Refused("Invalid symbol")
        if segment not in ("nse_fo", "mcx_fo"):
            raise Refused("Unsupported segment")
        positive_int(lots); positive_int(lot_size)
        price, tick = amount(limit_price), amount(tick_size)
        if not price or not tick or price % tick:
            raise Refused("Invalid limit price or tick")
        for stamp in (signal_time, quote_time, now):
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise Refused("Timezone required")
        if any(not 0 <= (now - t).total_seconds() <= limits.max_age_seconds
               for t in (signal_time, quote_time)):
            raise Refused("Stale or future input")
        if lots > limits.max_lots or amount(realized_loss) >= limits.daily_loss:
            raise Refused("Risk limit reached")
        body = dict(symbol=symbol, segment=segment, quantity=lots * lot_size,
                    limit_price=str(price), premium=str(price * lots * lot_size),
                    signal_time=signal_time.isoformat(), side="BUY", mode="live")
        encoded = json.dumps(body, sort_keys=True)
        tag = "orion" + hashlib.sha256((self.account + ':' + event).encode()).hexdigest()[:24]
        with self.transaction():
            prior = self.db.execute("SELECT * FROM intents WHERE event=?", (event,)).fetchone()
            if prior:
                if prior["body"] != encoded:
                    raise Refused("Duplicate event changed")
                return dict(prior)
            if self.db.execute("SELECT paused FROM control WHERE id=1").fetchone()[0]:
                raise Refused("Entries paused")
            if self.db.execute("SELECT 1 FROM intents WHERE status IN ('DISPATCHING','UNKNOWN')").fetchone():
                raise Refused("Reconciliation required")
            rows = self.db.execute("SELECT body,status,filled FROM intents").fetchall()
            # Conservative lifetime cap until accounting/position closure is implemented.
            if len(rows) >= limits.max_entries:
                raise Refused("Entry limit reached")
            used = sum((amount(json.loads(r['body'])['premium']) for r in rows
                        if r['status'] not in ('REJECTED','CANCELLED') or r['filled']), Decimal(0))
            if used + price * lots * lot_size > limits.max_premium:
                raise Refused("Premium budget exceeded")
            self.db.execute("INSERT INTO intents(tag,event,body,status) VALUES(?,?,?,'PREPARED')",
                            (tag, event, encoded))
        return self.get(tag)

    def get(self, tag):
        row = self.db.execute("SELECT * FROM intents WHERE tag=?", (tag,)).fetchone()
        if row is None:
            raise Refused("Unknown intent")
        return dict(row)

    def mark_dispatching(self, tag):
        """Durably record intent before a FUTURE transport sends an order.

        A crash/timeout must never cause automatic resubmission. This method
        itself sends nothing; restart leaves DISPATCHING blocking new entries.
        """
        with self.transaction():
            if self.db.execute("SELECT paused FROM control WHERE id=1").fetchone()[0]:
                raise Refused("Entries paused")
            if self.db.execute("SELECT 1 FROM intents WHERE status IN ('DISPATCHING','UNKNOWN')").fetchone():
                raise Refused("Reconciliation required")
            changed = self.db.execute("UPDATE intents SET status='DISPATCHING' WHERE tag=? AND status='PREPARED'", (tag,))
            if changed.rowcount != 1:
                raise Refused("Intent cannot be dispatched twice")
        return self.get(tag)

    def mark_unknown(self, tag):
        with self.transaction():
            changed = self.db.execute("UPDATE intents SET status='UNKNOWN' WHERE tag=? AND status='DISPATCHING'", (tag,))
            if changed.rowcount != 1:
                raise Refused("Invalid transition")
            self.db.execute("UPDATE control SET paused=1 WHERE id=1")

    def reconcile(self, tag, *, account, broker_id, symbol, quantity, status, filled, average):
        """Apply a normalized broker observation supplied by offline tests only.

        An empty order book is not proof of rejection. Only explicit matching
        observations resolve uncertainty. No raw broker-response parsing yet.
        """
        if account != self.account or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", broker_id):
            raise Refused("Broker identity mismatch")
        if status not in ("OPEN", "PARTIAL", "FILLED", "CANCELLED", "REJECTED"):
            raise Refused("Unknown broker status")
        positive_int(quantity)
        avg = amount(average)
        if type(filled) is not int or not 0 <= filled <= quantity:
            raise Refused("Invalid filled quantity")
        if ((status == "FILLED" and filled != quantity) or
                (status == "PARTIAL" and not 0 < filled < quantity) or
                (status in ("OPEN", "REJECTED") and filled != 0) or
                (filled > 0 and avg <= 0)):
            raise Refused("Inconsistent fill")
        with self.transaction():
            row = self.get(tag)
            body = json.loads(row['body'])
            if (row['status'] == 'PREPARED' or body['symbol'] != symbol or
                    body['quantity'] != quantity or filled < row['filled'] or
                    (filled and avg > amount(body['limit_price'])) or
                    (row['broker_id'] and row['broker_id'] != broker_id)):
                raise Refused("Order observation mismatch")
            if row['status'] in ('FILLED','CANCELLED','REJECTED') and (
                    status != row['status'] or filled != row['filled'] or avg != amount(row['average'])):
                raise Refused("Terminal order changed")
            self.db.execute("UPDATE intents SET status=?,broker_id=?,filled=?,average=? WHERE tag=?",
                            (status, broker_id, filled, str(avg), tag))
        return self.get(tag)

"""Offline protective-exit protocol. No broker transport or production callers."""
import hashlib
import json
import re

from .ledger import Refused, amount, positive_int


class Protection:
    """Serialize sell capacity for one terminal entry; uncertainty locks entries.

    An exit command is recorded once before a hypothetical send. Cancellation
    requests are never treated as cancellation confirmations. This harness does
    not provide price selection, an OCO implementation or live execution.
    """

    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS protective_exits(
                tag TEXT PRIMARY KEY, entry_tag TEXT NOT NULL,
                quantity INTEGER NOT NULL, trigger TEXT NOT NULL,
                status TEXT NOT NULL, broker_id TEXT UNIQUE,
                filled INTEGER NOT NULL DEFAULT 0);
        ''')

    def incident(self, code):
        # Persist outside the rejected transaction, including after restart.
        with self.ledger.transaction():
            self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES(?)", (code,))
            self.db.execute("UPDATE control SET paused=1 WHERE id=1")

    def _check_conflicts(self):
        if self.db.execute("SELECT 1 FROM live_incidents WHERE code IN ('protective-observation-conflict','protective-outcome-unknown')").fetchone():
            raise Refused("Protective reconciliation requires review")

    def get(self, tag):
        row = self.db.execute("SELECT * FROM protective_exits WHERE tag=?", (tag,)).fetchone()
        if row is None:
            raise Refused("Unknown protective exit")
        return dict(row)

    def _remaining(self, entry_tag):
        entry = self.ledger.get(entry_tag)
        if entry['status'] not in ('FILLED', 'CANCELLED') or not entry['filled']:
            raise Refused("Terminal filled entry required")
        sold = self.db.execute(
            "SELECT COALESCE(SUM(filled),0) FROM protective_exits WHERE entry_tag=?",
            (entry_tag,)).fetchone()[0]
        remaining = entry['filled'] - sold
        if remaining < 0:
            raise Refused("Exit fills exceed entry")
        return remaining

    def prepare(self, entry_tag, *, trigger_price, tick_size, lot_size):
        """Reserve all remaining sell capacity; no parallel stop/target orders."""
        trigger, tick = amount(trigger_price), amount(tick_size)
        positive_int(lot_size)
        if not trigger or not tick or trigger % tick:
            raise Refused("Invalid protective price")
        with self.ledger.transaction():
            self._check_conflicts()
            remaining = self._remaining(entry_tag)
            if not remaining or remaining % lot_size:
                raise Refused("Nonzero whole-lot exposure required")
            rows = self.db.execute("SELECT * FROM protective_exits WHERE entry_tag=?", (entry_tag,)).fetchall()
            if any(r['status'] not in ('FILLED', 'CANCELLED', 'REJECTED') for r in rows):
                raise Refused("Previous exit unresolved")
            tag = 'exit' + hashlib.sha256(f'{entry_tag}:{len(rows)}'.encode()).hexdigest()[:24]
            self.db.execute("INSERT INTO protective_exits(tag,entry_tag,quantity,trigger,status) VALUES(?,?,?,?,'PREPARED')",
                            (tag, entry_tag, remaining, str(trigger)))
            # Exposure is not protected merely because an intent exists.
            self.db.execute("UPDATE control SET paused=1 WHERE id=1")
        return self.get(tag)

    def dispatch(self, tag):
        with self.ledger.transaction():
            self._check_conflicts()
            row = self.get(tag)
            if self._remaining(row['entry_tag']) != row['quantity']:
                raise Refused("Exposure changed")
            result = self.db.execute("UPDATE protective_exits SET status='DISPATCHING' WHERE tag=? AND status='PREPARED'", (tag,))
            if result.rowcount != 1:
                raise Refused("Exit cannot be dispatched twice")
        return self.get(tag)

    def request_cancel(self, tag):
        with self.ledger.transaction():
            result = self.db.execute("UPDATE protective_exits SET status='CANCEL_PENDING' WHERE tag=? AND status IN ('OPEN','PARTIAL')", (tag,))
            if result.rowcount != 1:
                raise Refused("Exit cannot be cancelled in this state")
        return self.get(tag)

    def uncertain(self, tag):
        with self.ledger.transaction():
            result = self.db.execute("UPDATE protective_exits SET status='UNKNOWN' WHERE tag=? AND status IN ('DISPATCHING','CANCEL_PENDING')", (tag,))
            if result.rowcount != 1:
                raise Refused("Invalid uncertainty transition")
        self.incident('protective-outcome-unknown')

    def observe(self, tag, *, account, broker_id, symbol, segment, side, quantity, status, filled):
        """Apply synthetic normalized observations, never raw SDK responses.

        A contradictory terminal observation latches an incident, rather than
        guessing whether a delayed fill permits another sell order.
        """
        try:
            with self.ledger.transaction():
                row = self.get(tag)
                body = json.loads(self.ledger.get(row['entry_tag'])['body'])
                if (account != self.ledger.account or side != 'SELL' or
                        symbol != body['symbol'] or segment != body['segment'] or
                        type(quantity) is not int or quantity != row['quantity'] or
                        not isinstance(broker_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', broker_id) or
                        (row['broker_id'] and row['broker_id'] != broker_id)):
                    raise Refused("Protective identity mismatch")
                if (status not in ('OPEN', 'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED') or
                        type(filled) is not int or not row['filled'] <= filled <= quantity or
                        (status == 'FILLED' and filled != quantity) or
                        (status == 'PARTIAL' and not 0 < filled < quantity) or
                        (status in ('OPEN', 'REJECTED') and filled != 0)):
                    raise Refused("Invalid protective fill")
                if row['status'] == 'PREPARED':
                    raise Refused("Exit not dispatched")
                if row['status'] in ('FILLED', 'CANCELLED', 'REJECTED') and (status != row['status'] or filled != row['filled']):
                    raise Refused("Terminal protective order changed")
                duplicate = self.db.execute("SELECT 1 FROM protective_exits WHERE broker_id=? AND tag!=?", (broker_id, tag)).fetchone()
                if duplicate or self.db.execute("SELECT 1 FROM intents WHERE broker_id=?", (broker_id,)).fetchone():
                    raise Refused("Broker order already bound")
                # Open updates during a cancel race do not confirm cancellation.
                effective = 'CANCEL_PENDING' if row['status'] == 'CANCEL_PENDING' and status in ('OPEN', 'PARTIAL') else status
                self.db.execute("UPDATE protective_exits SET status=?,broker_id=?,filled=? WHERE tag=?",
                                (effective, broker_id, filled, tag))
                remaining = self._remaining(row['entry_tag'])
        except Refused:
            self.incident('protective-observation-conflict')
            raise
        if status in ('REJECTED', 'CANCELLED') and remaining:
            self.incident('unprotected-exposure')
        return self.get(tag)

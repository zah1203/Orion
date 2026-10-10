"""Offline full-book comparison of Kotak-shaped fixtures; no network calls.

This audits already-bound orders. It cannot resolve an ambiguous placement by
matching a tag, import external trades, or repair a ledger from a broker book.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from .ledger import Refused, amount
from .readonly import ProbeFailure, integer, rows


def fingerprint(ledger):
    state = []
    for table in ('intents', 'protective_exits', 'reconciliation_instruments', 'fill_history', 'fill_contracts', 'fee_corrections', 'execution_terms', 'execution_attempts', 'broker_commands', 'protection_policy'):
        if ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            state.append((table, [dict(r) for r in ledger.db.execute(f'SELECT * FROM {table} ORDER BY 1')]))
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def require_current(ledger):
    """Called inside the entry-gate transaction; never resumes entries itself."""
    row = ledger.db.execute('SELECT * FROM reconciliation_state WHERE id=1').fetchone()
    if not row or row['session'] != ledger.session:
        raise Refused('Fresh reconciliation required after open')
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['checked_at'])).total_seconds()
    if not 0 <= age <= 30 or row['digest'] != fingerprint(ledger):
        raise Refused('Reconciliation stale or ledger changed')


def text_field(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value):
        raise Refused('Invalid broker identity field')
    return value


def instrument(row):
    return tuple(text_field(row.get(k)) for k in ('exSeg', 'prod', 'tok', 'trdSym'))


def normalize_orders(report, ucc):
    result = {}
    for row in rows(report):
        if row.get('actId') != ucc:
            raise Refused('Broker account mismatch')
        oid = text_field(row.get('nOrdNo'))
        if oid in result:
            raise Refused('Duplicate broker order')
        quantity, filled = integer(row.get('qty')), integer(row.get('fldQty'))
        if quantity <= 0 or filled > quantity:
            raise Refused('Invalid order quantity')
        raw = row.get('ordSt')
        if raw in ('open', 'trigger pending'):
            if filled == quantity:
                raise Refused('Inconsistent working order')
            status = 'PARTIAL' if filled else 'OPEN'
        else:
            status = {'complete': 'FILLED', 'cancelled': 'CANCELLED', 'rejected': 'REJECTED'}.get(raw)
        if (not status or (status == 'FILLED' and filled != quantity) or
                (status == 'REJECTED' and filled)):
            raise Refused('Unsupported order state')
        if row.get('trnsTp') not in ('B', 'S'):
            raise Refused('Invalid order side')
        avg = amount(row.get('avgPrc'))
        if (filled and not avg) or (not filled and avg):
            raise Refused('Invalid fill average')
        result[oid] = dict(instrument=instrument(row), quantity=quantity, filled=filled,
                           status=status, side=row['trnsTp'], average=avg,
                           price=amount(row.get('prc')), trigger=amount(row.get('trgPrc')),
                           order_type=text_field(row.get('prcTp')))
    return result


def normalize_positions(report, ucc):
    result = {}
    for row in rows(report):
        if row.get('actId') != ucc:
            raise Refused('Broker account mismatch')
        key = instrument(row)
        if key in result:
            raise Refused('Duplicate broker position')
        result[key] = (integer(row.get('cfBuyQty')) + integer(row.get('flBuyQty')) -
                       integer(row.get('cfSellQty')) - integer(row.get('flSellQty')))
    return {k: v for k, v in result.items() if v}


class Reconciliation:
    def __init__(self, ledger, ucc):
        self.ledger = ledger
        self.db = ledger.db
        self.ucc = text_field(ucc)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS reconciliation_account(ucc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reconciliation_instruments(
                entry_tag TEXT PRIMARY KEY, product TEXT NOT NULL, token TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reconciliation_state(
                id INTEGER PRIMARY KEY CHECK(id=1), session TEXT NOT NULL,
                checked_at TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reconciliation_audit(
                seq INTEGER PRIMARY KEY, at TEXT NOT NULL, result TEXT NOT NULL);
        ''')
        with ledger.transaction():
            row = self.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
            if row and row[0] != self.ucc:
                raise Refused('Reconciliation account binding mismatch')
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='fill_account'").fetchone():
                bound = self.db.execute('SELECT ucc FROM fill_account').fetchone()
                if bound and bound[0] != self.ucc:
                    raise Refused('Reconciliation account binding mismatch')
            if not row:
                self.db.execute('INSERT INTO reconciliation_account VALUES(?)', (self.ucc,))

    def bind_instrument(self, entry_tag, *, product, token):
        """Offline fixture metadata. Future transport must bind before dispatch."""
        product, token = text_field(product), text_field(token)
        self.ledger.get(entry_tag)
        with self.ledger.transaction():
            prior = self.db.execute('SELECT product,token FROM reconciliation_instruments WHERE entry_tag=?', (entry_tag,)).fetchone()
            if prior and tuple(prior) != (product, token):
                raise Refused('Instrument binding cannot change')
            self.db.execute('INSERT OR IGNORE INTO reconciliation_instruments VALUES(?,?,?)', (entry_tag, product, token))

    def compare(self, orders, positions, *, started_at, completed_at, complete):
        """Require explicit completeness provenance from a future read adapter.

        Caller timestamps/complete are assertions, not proof of atomic broker
        data. Production collection and pagination guarantees remain unverified.
        A successful comparison NEVER updates fills, clears incidents or resumes.
        """
        now = datetime.now(timezone.utc)
        try:
            if complete is not True:
                raise Refused('Complete books required')
            for stamp in (started_at, completed_at):
                if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise Refused('Aware snapshot timestamps required')
            if not (0 <= (completed_at-started_at).total_seconds() <= 20 and
                    0 <= (now-completed_at).total_seconds() <= 30):
                raise Refused('Snapshot stale or invalid')
            broker_orders = normalize_orders(orders, self.ucc)
            broker_positions = normalize_positions(positions, self.ucc)
            with self.ledger.transaction():
                self._compare(broker_orders, broker_positions)
                self.db.execute('INSERT OR REPLACE INTO reconciliation_state VALUES(1,?,?,?)',
                                (self.ledger.session, completed_at.isoformat(), fingerprint(self.ledger)))
                self.db.execute('INSERT INTO reconciliation_audit(at,result) VALUES(?,?)', (now.isoformat(), 'matched'))
        except (Refused, ProbeFailure, TypeError, KeyError, OverflowError):
            with self.ledger.transaction():
                self.db.execute('DELETE FROM reconciliation_state')
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('broker-snapshot-mismatch')")
                self.db.execute('UPDATE control SET paused=1 WHERE id=1')
                self.db.execute('INSERT INTO reconciliation_audit(at,result) VALUES(?,?)', (now.isoformat(), 'blocked'))
            raise Refused('Broker snapshot requires review') from None
        return dict(matched=True, order_count=len(broker_orders), position_count=len(broker_positions),
                    live_available=False, order_submission_available=False)

    def _compare(self, orders, positions):
        expected_orders, expected_positions = {}, {}
        for row in self.db.execute('SELECT * FROM intents'):
            body = json.loads(row['body'])
            binding = self.db.execute('SELECT product,token FROM reconciliation_instruments WHERE entry_tag=?', (row['tag'],)).fetchone()
            if not binding:
                raise Refused('Instrument binding required')
            key = (body['segment'], binding['product'], binding['token'], body['symbol'])
            expected_positions[key] = expected_positions.get(key, 0) + row['filled']
            self._order(expected_orders, row, key, 'B', orders, body)
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='protective_exits'").fetchone():
                for exit_row in self.db.execute('SELECT * FROM protective_exits WHERE entry_tag=?', (row['tag'],)):
                    expected_positions[key] -= exit_row['filled']
                    self._order(expected_orders, exit_row, key, 'S', orders, dict(exit_row))
        if set(expected_orders) != set(orders):
            raise Refused('External or missing broker order')
        if any(v < 0 for v in expected_positions.values()):
            raise Refused('Unexpected short exposure')
        if {k: v for k, v in expected_positions.items() if v} != positions:
            raise Refused('Broker position mismatch')

    @staticmethod
    def _order(expected, row, key, side, orders, body):
        if row['status'] == 'PREPARED':
            if row['broker_id'] or row['filled']:
                raise Refused('Invalid prepared order')
            return
        oid = row['broker_id']
        if not oid or oid in expected or oid not in orders or row['status'] in ('DISPATCHING', 'UNKNOWN', 'CANCEL_PENDING'):
            raise Refused('Unresolved broker order')
        expected[oid] = True
        observed = orders[oid]
        quantity = body['quantity']
        if any(observed[k] != v for k, v in dict(instrument=key, side=side, quantity=quantity,
                                                  status=row['status'], filled=row['filled']).items()):
            raise Refused('Order observation mismatch')
        if side == 'B':
            if (observed['average'] != amount(row['average']) or observed['order_type'] != 'L' or
                    observed['price'] != amount(body['limit_price'])):
                raise Refused('Entry price mismatch')
        elif observed['order_type'] not in ('SL', 'SL-M') or observed['trigger'] != amount(body['trigger']):
            raise Refused('Protective order mismatch')

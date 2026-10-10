"""Durable handoff of already-dispatched intents to the bounded SDK process.

Not an entry gate: callers must first authorize/reserve/mark dispatching under
the account lease. No current portal, runtime or supervisor calls this module.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json

from .kotak import OrderRequest
from .ledger import Refused, amount


class Commands:
    def __init__(self, ledger, session):
        self.ledger, self.session = ledger, session
        self.db = ledger.db
        row = self.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
        if not row or row[0] != session.ucc:
            raise Refused('Execution session account mismatch')
        self.db.execute('''CREATE TABLE IF NOT EXISTS broker_commands(
            kind TEXT NOT NULL, tag TEXT NOT NULL, operation TEXT NOT NULL,
            request TEXT NOT NULL, status TEXT NOT NULL, broker_id TEXT,
            PRIMARY KEY(kind,tag,operation))''')

    def _row(self, kind, tag):
        if kind not in ('ENTRY', 'STOP', 'EXIT'):
            raise Refused('Unsupported command kind')
        table = 'intents' if kind == 'ENTRY' else 'protective_exits'
        row = self.db.execute(f'SELECT * FROM {table} WHERE tag=?', (tag,)).fetchone()
        if not row:
            raise Refused('Unknown command intent')
        if kind != 'ENTRY' and (kind == 'EXIT') != (amount(row['trigger']) == 0):
            raise Refused('Exit command purpose mismatch')
        return table, dict(row)

    def place(self, kind, tag, *, stop_limit=None):
        """No PREPARED -> dispatch transition here; acceptance is not a fill."""
        self._require_committed_boundary()
        with self.ledger.transaction():
            table, row = self._row(kind, tag)
            if row['status'] != 'DISPATCHING' or row['broker_id']:
                raise Refused('Committed unbound dispatch required')
            entry_tag = tag if kind == 'ENTRY' else row['entry_tag']
            body = json.loads(self.ledger.get(entry_tag)['body'])
            terms = self.db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry_tag,)).fetchone()
            binding = self.db.execute('SELECT product FROM reconciliation_instruments WHERE entry_tag=?', (entry_tag,)).fetchone()
            if not terms or not binding or body['segment'] != 'nse_fo' or binding[0] != 'NRML':
                raise Refused('NSE NRML execution terms required')
            if kind == 'ENTRY' and not self.db.execute('SELECT 1 FROM execution_attempts WHERE tag=?', (tag,)).fetchone():
                raise Refused('Atomic risk/dispatch record required')
            if kind == 'ENTRY':
                if self.db.execute('SELECT paused FROM control WHERE id=1').fetchone()[0] or self.db.execute('SELECT 1 FROM live_incidents').fetchone():
                    raise Refused('Entry paused after risk decision')
                attempt = self.db.execute('SELECT at FROM execution_attempts WHERE tag=?', (tag,)).fetchone()
                now = datetime.now(timezone.utc)
                for stamp in (attempt[0], body['signal_time']):
                    if not 0 <= (now-datetime.fromisoformat(stamp)).total_seconds() <= 30:
                        raise Refused('Committed entry expired before transport')
            else:
                from .protection import check_conflicts
                check_conflicts(self.db)
                if kind == 'EXIT' and self.db.execute('SELECT 1 FROM live_incidents').fetchone():
                    raise Refused('Target blocked by unresolved incident')
            exit_price = stop_limit
            if kind == 'EXIT':
                bound = self.db.execute('SELECT limit_price FROM exit_terms WHERE tag=?', (tag,)).fetchone()
                if not bound:
                    raise Refused('Bound target price required')
                exit_price = bound[0]
            request = OrderRequest(tag=tag, symbol=body['symbol'],
                quantity=body['quantity'] if kind == 'ENTRY' else row['quantity'],
                lot_size=terms['lot_size'], tick=terms['tick'], kind=kind,
                price=body['limit_price'] if kind == 'ENTRY' else exit_price,
                trigger='0' if kind == 'ENTRY' else row['trigger'])
            request.parameters()
            command = dict(request=asdict(request))
            self._record(kind, tag, 'place', command)
        return self._send(kind, tag, 'place', command, table)

    def cancel(self, kind, tag):
        self._require_committed_boundary()
        with self.ledger.transaction():
            table, row = self._row(kind, tag)
            if row['status'] not in ('OPEN', 'PARTIAL') or not row['broker_id']:
                raise Refused('Observed working order required')
            command = dict(broker_id=row['broker_id'])
            self._record(kind, tag, 'cancel', command)
            # Do not mutate observed order status/filled. The journal separately
            # blocks entry until a terminal broker observation confirms outcome.
            self.ledger.pause()
        return self._send(kind, tag, 'cancel', command, table)

    def _require_committed_boundary(self):
        # Releasing a nested savepoint is not a durable commit. Never let an
        # enclosing caller roll back the journal after a broker sees the order.
        if self.db.in_transaction:
            raise Refused('Broker handoff requires a committed transaction boundary')

    def _record(self, kind, tag, operation, request):
        row = self.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
        if not row or row[0] != self.session.ucc:
            raise Refused('Execution session account changed')
        if self.db.execute("SELECT 1 FROM broker_commands WHERE status IN ('SENDING','UNKNOWN')").fetchone():
            raise Refused('Uncertain broker command requires review')
        if self.db.execute("SELECT 1 FROM live_incidents WHERE code IN ('broker-command-unknown','broker-observation-conflict')").fetchone():
            raise Refused('Broker command incident requires review')
        if self.db.execute('SELECT 1 FROM broker_commands WHERE kind=? AND tag=? AND operation=?', (kind, tag, operation)).fetchone():
            raise Refused('Broker command cannot be sent twice')
        self.db.execute('INSERT INTO broker_commands VALUES(?,?,?,?,?,NULL)',
                        (kind, tag, operation, json.dumps(request, sort_keys=True), 'SENDING'))

    def confirm(self, kind, tag, operation):
        """After validated broker ingestion, never from a command ACK itself.

        Unknown placement identity cannot be reconstructed from a client tag.
        This never clears an uncertainty incident or resumes entries.
        """
        with self.ledger.transaction():
            _, row = self._row(kind, tag)
            command = self.db.execute('SELECT * FROM broker_commands WHERE kind=? AND tag=? AND operation=?', (kind, tag, operation)).fetchone()
            if not command or not command['broker_id'] or command['broker_id'] != row['broker_id']:
                raise Refused('Observed command binding required')
            allowed = ('FILLED', 'CANCELLED', 'REJECTED') if operation == 'cancel' else ('OPEN', 'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED')
            if row['status'] not in allowed:
                raise Refused('Broker observation required')
            self.db.execute("UPDATE broker_commands SET status='CONFIRMED' WHERE kind=? AND tag=? AND operation=?", (kind, tag, operation))

    def _send(self, kind, tag, operation, command, table):
        self._require_committed_boundary()
        try:
            result = self.session.request(operation, **command)
            from .kotak import acknowledgement
            oid = acknowledgement(dict(stat='Ok', stCode=200, nOrdNo=result.get('broker_id')),
                                  expected=command.get('broker_id'))
            with self.ledger.transaction():
                if operation == 'place':
                    for other in ('intents', 'protective_exits'):
                        if self.db.execute(f'SELECT 1 FROM {other} WHERE broker_id=?', (oid,)).fetchone():
                            raise Refused('Broker order already bound')
                    self.db.execute(f'UPDATE {table} SET broker_id=? WHERE tag=?', (oid, tag))
                self.db.execute("UPDATE broker_commands SET status='ACKNOWLEDGED',broker_id=? WHERE kind=? AND tag=? AND operation=?",
                                (oid, kind, tag, operation))
            return oid
        except BaseException as exc:
            with self.ledger.transaction():
                self.db.execute("UPDATE broker_commands SET status='UNKNOWN' WHERE kind=? AND tag=? AND operation=?", (kind, tag, operation))
                if operation == 'place':
                    self.db.execute(f"UPDATE {table} SET status='UNKNOWN' WHERE tag=?", (tag,))
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('broker-command-unknown')")
                self.ledger.pause()
            if not isinstance(exc, Exception):
                raise
            raise Refused('Broker command uncertain; reconcile before any further command') from None

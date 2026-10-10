"""Atomic current-book ingestion for already-bound orders; no network calls.

An order ID must have been durably bound by the placement acknowledgement. Tags
are never used to guess an unknown order's identity. Missing history and external
orders block reconciliation rather than being silently imported or discarded.
"""
from datetime import datetime, timezone
import json

from .ledger import Refused, amount
from .protection import Protection
from .reconciliation import Reconciliation, normalize_orders


class Observations:
    def __init__(self, ledger, ucc, commands):
        if commands.ledger is not ledger or commands.session.ucc != ucc:
            raise Refused('Observation account mismatch')
        self.ledger, self.commands = ledger, commands
        self.db = ledger.db
        self.reconciliation = Reconciliation(ledger, ucc)
        self.protection = Protection(ledger)
        self.ucc = ucc

    def ingest_evidence(self, snapshot):
        """Commit matching order, position and fill evidence as one transaction.

        Incomplete trades roll back order transitions and command confirmations
        too. Collectors cannot leave a partly reconciled, apparently fresh book.
        Fee/cash flags remain unverified and never grant entry permission.
        """
        from .trades import TradeJournal
        journal = TradeJournal(self.ledger, self.ucc)
        try:
            with self.ledger.transaction():
                self.ingest_snapshot(snapshot)
                inserted = journal.ingest(snapshot)
                # Fill evidence participates in the reconciliation fingerprint.
                result = self.ingest_snapshot(snapshot)
            return dict(result, new_fills=inserted, fees_verified=False,
                        available_cash_verified=False)
        except Exception:
            with self.ledger.transaction():
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('fill-history-conflict')")
                self.db.execute('DELETE FROM reconciliation_state')
                self.ledger.pause()
            raise Refused('Combined broker evidence requires review') from None

    def ingest_snapshot(self, snapshot):
        """Accept the process adapter's sanitized representation, validate again."""
        try:
            if snapshot['ucc'] != self.ucc:
                raise ValueError
            orders, positions = [], []
            states = dict(OPEN='open', PARTIAL='open', FILLED='complete', CANCELLED='cancelled', REJECTED='rejected')
            for row in snapshot['orders']:
                segment, product, token, symbol = row['instrument']
                orders.append(dict(actId=self.ucc, nOrdNo=row['broker_id'], exSeg=segment,
                    prod=product, tok=token, trdSym=symbol, qty=row['quantity'], fldQty=row['filled'],
                    ordSt=states[row['status']], trnsTp=row['side'], avgPrc=row['average'],
                    prc=row['price'], trgPrc=row['trigger'], prcTp=row['order_type']))
            for row in snapshot['positions']:
                segment, product, token, symbol = row['instrument']
                qty = row['quantity']
                if type(qty) is not int:
                    raise ValueError
                positions.append(dict(actId=self.ucc, exSeg=segment, prod=product, tok=token,
                    trdSym=symbol, cfBuyQty=0, cfSellQty=0, flBuyQty=max(0, qty), flSellQty=max(0, -qty)))
            started, completed = (datetime.fromisoformat(snapshot[k]) for k in ('started_at', 'completed_at'))
        except Exception:
            with self.ledger.transaction():
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('broker-observation-conflict')")
                self.ledger.pause()
            raise Refused('Malformed broker snapshot') from None
        return self.ingest(dict(stat='Ok', stCode=200, data=orders),
                           dict(stat='Ok', stCode=200, data=positions), started_at=started, completed_at=completed)

    def ingest(self, orders, positions, *, started_at, completed_at):
        try:
            now = datetime.now(timezone.utc)
            if (started_at.tzinfo is None or completed_at.tzinfo is None or
                    not 0 <= (completed_at-started_at).total_seconds() <= 20 or
                    not 0 <= (now-completed_at).total_seconds() <= 30):
                raise Refused('Fresh broker books required')
            from .history import expand
            with self.ledger.transaction():
                orders = expand(self.ledger, orders, completed_at)
                normalized = normalize_orders(orders, self.ucc)
                seen = set()
                for entry in self.db.execute('SELECT * FROM intents').fetchall():
                    body = json.loads(entry['body'])
                    binding = self.db.execute('SELECT product,token FROM reconciliation_instruments WHERE entry_tag=?', (entry['tag'],)).fetchone()
                    if not binding:
                        raise Refused('Bound instrument required')
                    key = (body['segment'], binding['product'], binding['token'], body['symbol'])
                    if entry['status'] != 'PREPARED':
                        observed = self._match(entry, normalized, seen, key, body['quantity'], 'B')
                        if observed['order_type'] != 'L' or observed['price'] != amount(body['limit_price']) or observed['trigger']:
                            raise Refused('Entry order terms changed')
                        self.ledger.reconcile(entry['tag'], account=self.ledger.account,
                            broker_id=entry['broker_id'], symbol=body['symbol'], quantity=body['quantity'],
                            status=observed['status'], filled=observed['filled'], average=observed['average'])
                        self._confirm('ENTRY', entry['tag'])
                    for exit_row in self.db.execute('SELECT * FROM protective_exits WHERE entry_tag=?', (entry['tag'],)).fetchall():
                        if exit_row['status'] == 'PREPARED':
                            continue
                        observed = self._match(exit_row, normalized, seen, key, exit_row['quantity'], 'S')
                        command = self.db.execute("SELECT request FROM broker_commands WHERE kind='STOP' AND tag=? AND operation='place'", (exit_row['tag'],)).fetchone()
                        if not command:
                            raise Refused('Protective command terms required')
                        requested = json.loads(command['request'])['request']
                        if (observed['order_type'] != 'SL' or observed['trigger'] != amount(exit_row['trigger']) or
                                observed['price'] != amount(requested['price'])):
                            raise Refused('Protective order terms changed')
                        self.protection.observe(exit_row['tag'], account=self.ledger.account,
                            broker_id=exit_row['broker_id'], symbol=body['symbol'], segment=body['segment'],
                            side='SELL', quantity=exit_row['quantity'], status=observed['status'], filled=observed['filled'])
                        self._confirm('STOP', exit_row['tag'])
                if seen != set(normalized):
                    raise Refused('External broker orders require review')
                result = self.reconciliation.compare(orders, positions, started_at=started_at,
                                                     completed_at=completed_at, complete=True)
            return result
        except Exception:
            # Nested validators may roll their incident back along with the bad
            # batch. Persist the outer incident after that rollback, never lose it.
            with self.ledger.transaction():
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('broker-observation-conflict')")
                self.db.execute('DELETE FROM reconciliation_state')
                self.ledger.pause()
            raise Refused('Broker observations require review') from None

    @staticmethod
    def _match(row, orders, seen, key, quantity, side):
        oid = row['broker_id']
        if not oid or oid not in orders or oid in seen:
            raise Refused('Missing or ambiguous broker order')
        seen.add(oid)
        value = orders[oid]
        if value['instrument'] != key or value['quantity'] != quantity or value['side'] != side:
            raise Refused('Broker order identity mismatch')
        return value

    def _confirm(self, kind, tag):
        for command in self.db.execute('SELECT operation FROM broker_commands WHERE kind=? AND tag=?', (kind, tag)).fetchall():
            if command['operation'] == 'cancel':
                _, row = self.commands._row(kind, tag)
                if row['status'] not in ('FILLED', 'CANCELLED', 'REJECTED'):
                    continue
            self.commands.confirm(kind, tag, command['operation'])

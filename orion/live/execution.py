"""Integrated offline execution harness. Deliberately has no broker SDK transport.

All simulated orders pass the same ledger/protection/accounting components. The
production portal/worker cannot construct this harness with a real transport.
"""
from datetime import datetime, timezone
import json

from .accounting import Accounting
from .ledger import Refused, amount, Limits, positive_int
from .pilot import validate_limits
from .protection import Protection
from .reconciliation import Reconciliation


class SimulatedBroker:
    """Deterministic test double, not a brokerage connection."""
    def __init__(self):
        self.orders = []
        self.timeout_after_accept = False

    def submit(self, body):
        oid = 'sim' + str(len(self.orders) + 1)
        self.orders.append(dict(body, broker_id=oid))
        if self.timeout_after_accept:
            raise TimeoutError('simulated ambiguous outcome')
        return oid


class ExecutionHarness:
    def __init__(self, ledger, ucc, limits, broker, *, enrolled, reviewed):
        if type(broker) is not SimulatedBroker:
            raise Refused('Only the built-in simulated broker is supported')
        if enrolled is not True or reviewed is not True:
            raise Refused('Reviewed pilot enrollment required')
        self.ledger = ledger
        self.limits = validate_limits(limits)
        self.broker = broker
        self.reconciliation = Reconciliation(ledger, ucc)
        self.accounting = Accounting(ledger, ucc)
        self.protection = Protection(ledger)
        self.ucc = ucc
        ledger.db.executescript('''
            CREATE TABLE IF NOT EXISTS execution_terms(
                tag TEXT PRIMARY KEY, stop TEXT NOT NULL, tick TEXT NOT NULL,
                lot_size INTEGER NOT NULL, fee_reserve TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_attempts(
                tag TEXT PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL);
        ''')

    def check_book(self, orders, positions, now):
        return self.reconciliation.compare(orders, positions, started_at=now,
                                           completed_at=now, complete=True)

    def prepare_entry(self, *, event, symbol, segment, product, token, lots,
                      lot_size, limit_price, stop, tick, signal_time, quote_time, now):
        # Pilot harness supports only instruments where a unit of premium is a
        # rupee per order unit. Commodity conversion requires its own adapter.
        if segment != 'nse_fo' or product != 'NRML':
            raise Refused('Harness supports NSE NRML only')
        positive_int(lots); positive_int(lot_size)
        price, stop_value, tick_value = amount(limit_price), amount(stop), amount(tick)
        if not 0 < stop_value < price or not tick_value or stop_value % tick_value:
            raise Refused('Valid tick-aligned stop required')
        with self.ledger.transaction():
            snapshot = self.accounting.summary(now=now, marks={})
            if amount(snapshot['loss_used']) >= amount(self.limits['daily_loss']):
                raise Refused('Daily loss reached')
            if price*lots*lot_size + amount(self.limits['fee_reserve']) > amount(self.limits['max_order_premium']):
                raise Refused('Per-order premium budget exceeded')
            row = self.ledger.reserve(event=event, symbol=symbol, segment=segment,
                lots=lots, lot_size=lot_size, limit_price=price, tick_size=tick,
                signal_time=signal_time, quote_time=quote_time, now=now,
                limits=Limits(self.limits['max_lots'], self.limits['max_entries'],
                              self.limits['max_open_premium'], self.limits['daily_loss']),
                realized_loss=snapshot['loss_used'])
            self.reconciliation.bind_instrument(row['tag'], product=product, token=token)
            self.accounting.bind_multiplier(row['tag'], 1)
            values = (row['tag'], str(stop_value), str(tick_value), lot_size, self.limits['fee_reserve'])
            old = self.ledger.db.execute('SELECT * FROM execution_terms WHERE tag=?', (row['tag'],)).fetchone()
            if old and tuple(old) != values:
                raise Refused('Duplicate execution terms changed')
            self.ledger.db.execute('INSERT OR IGNORE INTO execution_terms VALUES(?,?,?,?,?)', values)
        return row['tag']

    def dispatch_entry(self, tag, *, now, quote_time, available_cash):
        """Commit risk decision and one-shot intent together before simulated I/O."""
        from .accounting import IST, timestamp, bounded_amount
        now, quote_time = timestamp(now), timestamp(quote_time)
        if not 0 <= (now-quote_time).total_seconds() <= 30:
            raise Refused('Fresh dispatch quote required')
        day = now.astimezone(IST).date().isoformat()
        with self.ledger.transaction():
            self.ledger._check_incidents()
            row = self.ledger.get(tag)
            body = json.loads(row['body'])
            if not 0 <= (now-datetime.fromisoformat(body['signal_time'])).total_seconds() <= 30:
                raise Refused('Signal expired before dispatch')
            terms = self.ledger.db.execute('SELECT * FROM execution_terms WHERE tag=?', (tag,)).fetchone()
            if not terms:
                raise Refused('Execution terms required')
            snapshot = self.accounting.summary(now=now, marks={})
            used = self.ledger.db.execute('SELECT COUNT(*) FROM execution_attempts WHERE day=?', (day,)).fetchone()[0]
            if used >= self.limits['max_entries']:
                raise Refused('Daily dispatch count reached')
            # Full reserved premium + per-entry round-trip fee reserve, including
            # unresolved and prepared intents. Never rely on a stop to cap debit.
            reserved = amount(0)
            reserved_risk = amount(0)
            for candidate in self.ledger.db.execute("SELECT i.body,i.status,i.filled,t.fee_reserve,t.stop FROM intents i LEFT JOIN execution_terms t ON t.tag=i.tag"):
                if candidate['status'] in ('REJECTED','CANCELLED') and not candidate['filled']:
                    continue
                if candidate['fee_reserve'] is None:
                    raise Refused('Unaccounted reservation')
                candidate_body = json.loads(candidate['body'])
                reserved += amount(candidate_body['premium']) + amount(candidate['fee_reserve'])
                reserved_risk += (amount(candidate_body['limit_price'])-amount(candidate['stop'])) * candidate_body['quantity'] + amount(candidate['fee_reserve'])
            if reserved > min(amount(self.limits['capital']), amount(self.limits['max_open_premium']), bounded_amount(available_cash)):
                raise Refused('Reserved premium exceeds available budget')
            trade_risk = (amount(body['limit_price'])-amount(terms['stop'])) * body['quantity'] + amount(terms['fee_reserve'])
            if trade_risk > amount(self.limits['max_trade_loss']) or reserved_risk > amount(self.limits['max_open_risk']):
                raise Refused('Projected stop risk exceeds policy')
            if amount(snapshot['loss_used']) + reserved_risk >= amount(self.limits['daily_loss']):
                raise Refused('Daily fee/loss budget exceeded')
            self.ledger.mark_dispatching(tag)
            self.ledger.db.execute('INSERT INTO execution_attempts VALUES(?,?,?)', (tag, now.isoformat(), day))
        try:
            oid = self.broker.submit(dict(body, tag=tag, side='BUY'))
        except Exception:
            self.ledger.mark_unknown(tag)
            raise Refused('Simulated placement uncertain; do not retry') from None
        # An acknowledgement binds identity but is not evidence of an open/fill.
        with self.ledger.transaction():
            self.ledger.db.execute('UPDATE intents SET broker_id=? WHERE tag=?', (oid, tag))
        return oid

    def entry_observation(self, tag, *, status, filled, average):
        row = self.ledger.get(tag)
        body = json.loads(row['body'])
        return self.ledger.reconcile(tag, account=self.ledger.account, broker_id=row['broker_id'],
            symbol=body['symbol'], quantity=body['quantity'], status=status, filled=filled, average=average)

    def protective_exit(self, entry_tag):
        """Protect terminal confirmed entry exposure; never implies a target/OCO."""
        terms = self.ledger.db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry_tag,)).fetchone()
        if not terms:
            raise Refused('Execution terms required')
        exit_row = self.protection.prepare(entry_tag, trigger_price=terms['stop'],
                                           tick_size=terms['tick'], lot_size=terms['lot_size'])
        self.protection.dispatch(exit_row['tag'])
        try:
            oid = self.broker.submit(dict(exit_row, side='SELL', kind='PROTECTIVE'))
        except Exception:
            self.protection.uncertain(exit_row['tag'])
            raise Refused('Simulated protection uncertain; review required') from None
        with self.ledger.transaction():
            self.ledger.db.execute('UPDATE protective_exits SET broker_id=? WHERE tag=?', (oid, exit_row['tag']))
        return exit_row['tag'], oid

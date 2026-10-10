"""Offline immutable fill history and conservative daily risk evaluation.

Normalized fixtures only: no broker API, order submission or activation path.
Fees, contract multipliers and marks require future trusted broker adapters.
"""
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
from zoneinfo import ZoneInfo

from .ledger import Refused, amount, positive_int
from .reconciliation import text_field

IST = ZoneInfo('Asia/Kolkata')


def timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise Refused('Aware timestamp required')
    return value.astimezone(timezone.utc)


def bounded_amount(value):
    result = amount(value)
    if isinstance(value, bool) or result > Decimal('1e15') or result.as_tuple().exponent < -8:
        raise Refused('Invalid accounting amount')
    return result


class Accounting:
    def __init__(self, ledger, ucc):
        self.ledger, self.db = ledger, ledger.db
        self.ucc = text_field(ucc)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS fill_account(ucc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fill_contracts(
                entry_tag TEXT PRIMARY KEY, multiplier TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fill_history(
                segment TEXT NOT NULL, trade_day TEXT NOT NULL, trade_id TEXT NOT NULL,
                broker_id TEXT NOT NULL, entry_tag TEXT NOT NULL, side TEXT NOT NULL,
                quantity INTEGER NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
                executed_at TEXT NOT NULL,
                PRIMARY KEY(segment,trade_day,trade_id));
            CREATE TABLE IF NOT EXISTS fee_corrections(
                source_ref TEXT NOT NULL, segment TEXT NOT NULL, trade_day TEXT NOT NULL,
                trade_id TEXT NOT NULL, total_fee TEXT NOT NULL, reported_at TEXT NOT NULL,
                PRIMARY KEY(source_ref,segment,trade_day,trade_id));
        ''')
        with ledger.transaction():
            for table in ('fill_account', 'reconciliation_account'):
                if self.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                    row = self.db.execute(f'SELECT ucc FROM {table}').fetchone()
                    if row and row[0] != self.ucc:
                        raise Refused('Accounting broker account mismatch')
            if not self.db.execute('SELECT 1 FROM fill_account').fetchone():
                self.db.execute('INSERT INTO fill_account VALUES(?)', (self.ucc,))

    def bind_multiplier(self, entry_tag, multiplier):
        """Units-to-currency factor; explicitly supplied, never assumed to be 1."""
        self.ledger.get(entry_tag)
        value = bounded_amount(multiplier)
        if not value:
            raise Refused('Positive contract multiplier required')
        with self.ledger.transaction():
            old = self.db.execute('SELECT multiplier FROM fill_contracts WHERE entry_tag=?', (entry_tag,)).fetchone()
            if old and amount(old[0]) != value:
                raise Refused('Contract multiplier cannot change')
            self.db.execute('INSERT OR IGNORE INTO fill_contracts VALUES(?,?)', (entry_tag, str(value)))

    def _orders(self):
        result = {}
        for entry in self.db.execute('SELECT * FROM intents'):
            if entry['broker_id']:
                result[entry['broker_id']] = (dict(entry), entry['tag'], 'BUY')
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='protective_exits'").fetchone():
            for exit_row in self.db.execute('SELECT * FROM protective_exits WHERE broker_id IS NOT NULL'):
                if exit_row['broker_id'] in result:
                    raise Refused('Duplicate broker order binding')
                result[exit_row['broker_id']] = (dict(exit_row), exit_row['entry_tag'], 'SELL')
        return result

    def record(self, *, account, ucc, trade_id, broker_id, segment, symbol, side,
               quantity, price, fee, executed_at):
        """Deduplicate exchange/day trade IDs. Corrections require review.

        Cumulative order observations must be ingested first. This records fill
        evidence without modifying orders or importing an external position.
        """
        try:
            if account != self.ledger.account or ucc != self.ucc:
                raise Refused('Fill account mismatch')
            text_field(trade_id); text_field(broker_id)
            positive_int(quantity)
            if quantity > 1000000000:
                raise Refused('Invalid fill quantity')
            price, fee = bounded_amount(price), bounded_amount(fee)
            at = timestamp(executed_at)
            if not price or at > datetime.now(timezone.utc):
                raise Refused('Invalid fill price or time')
            with self.ledger.transaction():
                orders = self._orders()
                if broker_id not in orders:
                    raise Refused('Unknown fill order')
                order, entry_tag, expected_side = orders[broker_id]
                body = json.loads(self.ledger.get(entry_tag)['body'])
                if (side != expected_side or segment != body['segment'] or symbol != body['symbol'] or
                        order['status'] in ('PREPARED', 'DISPATCHING', 'UNKNOWN', 'REJECTED')):
                    raise Refused('Fill order mismatch')
                if side == 'BUY' and price > amount(body['limit_price']):
                    raise Refused('Fill exceeds entry limit')
                values = dict(segment=segment, trade_day=at.astimezone(IST).date().isoformat(),
                    trade_id=trade_id, broker_id=broker_id, entry_tag=entry_tag, side=side,
                    quantity=quantity, price=str(price.normalize()), fee=str(fee.normalize()),
                    executed_at=at.isoformat())
                old = self.db.execute('SELECT * FROM fill_history WHERE segment=? AND trade_day=? AND trade_id=?',
                                      (segment, values['trade_day'], trade_id)).fetchone()
                if old:
                    if dict(old) != values:
                        raise Refused('Conflicting fill identity')
                    return False
                total = self.db.execute('SELECT COALESCE(SUM(quantity),0) FROM fill_history WHERE broker_id=?', (broker_id,)).fetchone()[0]
                if total + quantity > order['filled']:
                    raise Refused('Fill exceeds observed cumulative quantity')
                self.db.execute('INSERT INTO fill_history VALUES(:segment,:trade_day,:trade_id,:broker_id,:entry_tag,:side,:quantity,:price,:fee,:executed_at)', values)
            return True
        except Refused:
            with self.ledger.transaction():
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('fill-history-conflict')")
                self.db.execute('UPDATE control SET paused=1 WHERE id=1')
            raise

    def correct_fee(self, *, account, ucc, source_ref, segment, trade_day, trade_id,
                    total_fee, reported_at):
        """Append a reviewed fee total without rewriting immutable trade evidence.

        Input remains an offline assertion until a statement adapter is verified.
        Source references are opaque non-secret IDs, not documents or credentials.
        Reports must advance in time; replaying a changed report is a conflict.
        """
        try:
            if account != self.ledger.account or ucc != self.ucc:
                raise Refused('Fee correction account mismatch')
            text_field(source_ref); text_field(trade_id)
            fee = bounded_amount(total_fee)
            at = timestamp(reported_at)
            if at > datetime.now(timezone.utc):
                raise Refused('Future fee report')
            values = (source_ref, segment, trade_day, trade_id, str(fee.normalize()), at.isoformat())
            with self.ledger.transaction():
                fill = self.db.execute('SELECT executed_at FROM fill_history WHERE segment=? AND trade_day=? AND trade_id=?', (segment, trade_day, trade_id)).fetchone()
                if not fill or at < datetime.fromisoformat(fill[0]):
                    raise Refused('Known fill preceding fee report required')
                previous = self.db.execute('SELECT * FROM fee_corrections WHERE source_ref=? AND segment=? AND trade_day=? AND trade_id=?', values[:4]).fetchone()
                if previous:
                    if tuple(previous) != values:
                        raise Refused('Fee report changed')
                    return False
                latest = self.db.execute('SELECT MAX(reported_at) FROM fee_corrections WHERE segment=? AND trade_day=? AND trade_id=?', (segment, trade_day, trade_id)).fetchone()[0]
                if latest and at <= datetime.fromisoformat(latest):
                    raise Refused('Fee report is not newer')
                self.db.execute('INSERT INTO fee_corrections VALUES(?,?,?,?,?,?)', values)
            return True
        except Refused:
            with self.ledger.transaction():
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('fill-history-conflict')")
                self.ledger.pause()
            raise

    def summary(self, *, now, marks):
        """Per-entry FIFO attribution; conservative loss versus acquisition cost.

        Marks map entry tags to (price, aware timestamp). Daily realized P&L is
        attributed to the sell execution's IST date; fees to their fill date.
        Carried unrealized losses count fully, gains never offset daily losses.
        This is not broker settlement MTM, available cash or an execution permit.
        """
        now = timestamp(now)
        day = now.astimezone(IST).date().isoformat()
        with self.ledger.transaction(), localcontext() as context:
            context.prec = 80
            fills = [dict(r) for r in self.db.execute('SELECT * FROM fill_history ORDER BY executed_at,trade_id')]
            orders = self._orders()
            if self.db.execute("SELECT 1 FROM intents WHERE status IN ('DISPATCHING','UNKNOWN')").fetchone():
                raise Refused('Unresolved entry requires reconciliation')
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='protective_exits'").fetchone():
                if self.db.execute("SELECT 1 FROM protective_exits WHERE status IN ('DISPATCHING','UNKNOWN','CANCEL_PENDING')").fetchone():
                    raise Refused('Unresolved exit requires reconciliation')
            for oid, (order, _, side) in orders.items():
                matches = [f for f in fills if f['broker_id'] == oid]
                if sum(f['quantity'] for f in matches) != order['filled']:
                    raise Refused('Incomplete fill history')
                if side == 'BUY' and order['filled']:
                    # Require exact agreement; rounded broker averages need a reviewed adapter.
                    total = sum((amount(f['price'])*f['quantity'] for f in matches), Decimal(0))
                    if total != amount(order['average'])*order['filled']:
                        raise Refused('Fill average mismatch')
            lots, first_entry_days = {}, {}
            realized, fees, unrealized_loss, open_premium = (Decimal(0) for _ in range(4))
            for fill in fills:
                if datetime.fromisoformat(fill['executed_at']) > now:
                    raise Refused('Fill is after evaluation time')
                tag = fill['entry_tag']
                row = self.db.execute('SELECT multiplier FROM fill_contracts WHERE entry_tag=?', (tag,)).fetchone()
                if not row:
                    raise Refused('Contract multiplier required')
                multiplier = amount(row[0])
                queue = lots.setdefault(tag, deque())
                if fill['trade_day'] == day:
                    correction = self.db.execute('''SELECT total_fee FROM fee_corrections
                        WHERE segment=? AND trade_day=? AND trade_id=? AND reported_at<=?
                        ORDER BY reported_at DESC LIMIT 1''',
                        (fill['segment'], fill['trade_day'], fill['trade_id'], now.isoformat())).fetchone()
                    fees += amount(correction[0] if correction else fill['fee'])
                if fill['side'] == 'BUY':
                    first_entry_days.setdefault(tag, fill['trade_day'])
                    queue.append([fill['quantity'], amount(fill['price']), multiplier])
                else:
                    remaining = fill['quantity']
                    while remaining:
                        if not queue:
                            raise Refused('Sell precedes matching buy exposure')
                        qty = min(queue[0][0], remaining)
                        if fill['trade_day'] == day:
                            realized += qty * (amount(fill['price'])-queue[0][1]) * multiplier
                        remaining -= qty
                        queue[0][0] -= qty
                        if not queue[0][0]:
                            queue.popleft()
            open_quantities = {}
            for tag, queue in lots.items():
                if not queue:
                    continue
                if tag not in marks or not isinstance(marks[tag], tuple) or len(marks[tag]) != 2:
                    raise Refused('Fresh mark required')
                mark, marked_at = marks[tag]
                mark = bounded_amount(mark)
                if not 0 <= (now-timestamp(marked_at)).total_seconds() <= 30:
                    raise Refused('Stale or future mark')
                open_quantities[tag] = sum(q[0] for q in queue)
                for qty, cost, multiplier in queue:
                    open_premium += qty*cost*multiplier
                    unrealized_loss += max(Decimal(0), cost-mark)*qty*multiplier
            return dict(day=day, realized=str(realized), fees=str(fees),
                unrealized_loss=str(unrealized_loss),
                loss_used=str(max(Decimal(0), -realized)+fees+unrealized_loss),
                open_premium=str(open_premium), open_quantities=open_quantities,
                filled_entries=sum(d == day for d in first_entry_days.values()),
                live_available=False)

    def check_limits(self, *, now, marks, max_daily_loss, max_filled_entries, max_open_premium):
        """Offline evaluation only; does not authorize or dispatch an entry.

        Does not include pending-order reservations, candidate order costs or
        rejected attempts. Those must be combined at a future execution gate.
        """
        positive_int(max_filled_entries)
        loss_limit, premium_limit = bounded_amount(max_daily_loss), bounded_amount(max_open_premium)
        if not loss_limit or not premium_limit:
            raise Refused('Positive risk limits required')
        if self.db.execute("SELECT 1 FROM live_incidents").fetchone():
            raise Refused('Accounting incident requires review')
        result = self.summary(now=now, marks=marks)
        if (amount(result['loss_used']) >= loss_limit or result['filled_entries'] >= max_filled_entries or
                amount(result['open_premium']) >= premium_limit):
            with self.ledger.transaction():
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('daily-accounting-limit')")
                self.db.execute('UPDATE control SET paused=1 WHERE id=1')
            raise Refused('Daily accounting limit reached')
        return result

"""Reviewed daily cash allocations. Never a broker balance or dispatch permit.

No credit for sale proceeds: purchases consume the day's allocation even after
exit. Pending purchases reserve their full quantity. Only an unsent abandoned
intent releases its fee allowance. No same-day reset or top-up is supported.
"""
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json

from .accounting import IST, bounded_amount, timestamp
from .ledger import Refused
from .reconciliation import fingerprint

CONFIRMATION = 'DEPOSITED CASH ONLY; NO EXTERNAL TRADES OR WITHDRAWALS; FEES ARE ESTIMATES'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class CashAllocation:
    def __init__(self, accounting):
        self.accounting, self.ledger, self.db = accounting, accounting.ledger, accounting.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS daily_cash_allocations(
            day TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL, account TEXT NOT NULL,
            ucc TEXT NOT NULL, policy_digest TEXT NOT NULL, opening_cash TEXT NOT NULL,
            baseline INTEGER NOT NULL, baseline_digest TEXT NOT NULL)''')

    def _history(self, baseline):
        entries = [dict(r) for r in self.db.execute('SELECT * FROM intents WHERE rowid<=? ORDER BY rowid', (baseline,))]
        tags = {r['tag'] for r in entries}
        state = [entries]
        for table, field in (('protective_exits','entry_tag'), ('fill_history','entry_tag'), ('execution_terms','tag')):
            state.append([dict(r) for r in self.db.execute(f'SELECT * FROM {table} ORDER BY rowid') if r[field] in tags])
        state.append([dict(r) for r in self.db.execute('''SELECT c.* FROM fee_corrections c JOIN fill_history f
            USING(segment,trade_day,trade_id) JOIN intents i ON i.tag=f.entry_tag WHERE i.rowid<=?
            ORDER BY c.rowid''', (baseline,))])
        return digest(state)

    def review(self, *, actor, policy, opening_cash, confirmation):
        if actor != self.ledger.account or confirmation != CONFIRMATION:
            raise Refused('Own-account cash and fee-model review required')
        cash = bounded_amount(opening_cash)
        if not cash or cash.as_tuple().exponent < -2 or cash > bounded_amount(policy['limits']['capital']):
            raise Refused('Positive opening allocation within reviewed capital required')
        now = datetime.now(timezone.utc)
        day = now.astimezone(IST).date().isoformat()
        with self.ledger.transaction():
            old = self.db.execute('SELECT * FROM daily_cash_allocations WHERE day=?', (day,)).fetchone()
            if old:
                if (old['policy_digest'], old['opening_cash'], old['ucc']) != (digest(policy), str(cash), self.accounting.ucc):
                    raise Refused('Daily allocation cannot be replaced or increased')
                return dict(day=day, duplicate=True, order_submission_available=False)
            # The portal can consume a fresh worker snapshot without assuming the
            # worker's session identity or starting another broker connection.
            checked = self.db.execute('SELECT * FROM reconciliation_state WHERE id=1').fetchone()
            if (not checked or not 0 <= (now-timestamp(datetime.fromisoformat(checked['checked_at']))).total_seconds() <= 30
                    or checked['digest'] != fingerprint(self.ledger)):
                raise Refused('Fresh complete broker reconciliation required')
            if self.db.execute('SELECT 1 FROM live_incidents').fetchone():
                raise Refused('Account incident requires review')
            if self.db.execute("SELECT 1 FROM intents WHERE status NOT IN ('FILLED','CANCELLED','REJECTED') AND tag NOT IN (SELECT tag FROM abandoned_intents)").fetchone():
                raise Refused('Opening allocation requires no pending entries')
            if self.db.execute("SELECT 1 FROM protective_exits WHERE status NOT IN ('FILLED','CANCELLED','REJECTED')").fetchone():
                raise Refused('Opening allocation requires no pending exits')
            if self.db.execute("SELECT 1 FROM broker_commands WHERE status!='CONFIRMED'").fetchone():
                raise Refused('Broker commands require reconciliation')
            summary = self.accounting.summary(now=now, marks={})
            if summary['open_quantities']:
                raise Refused('Opening allocation requires a flat account')
            # All today's activity must occur after this immutable opening point.
            for row in self.db.execute('SELECT body FROM intents'):
                if timestamp(datetime.fromisoformat(json.loads(row[0])['signal_time'])).astimezone(IST).date().isoformat() >= day:
                    raise Refused('Review opening allocation before the first daily intent')
            baseline = self.db.execute('SELECT COALESCE(MAX(rowid),0) FROM intents').fetchone()[0]
            self.db.execute('INSERT INTO daily_cash_allocations VALUES(?,?,?,?,?,?,?,?)',
                (day,now.isoformat(),actor,self.accounting.ucc,digest(policy),str(cash),baseline,self._history(baseline)))
            self.ledger.pause()
            return dict(day=day, duplicate=False, opening_cash=str(cash), order_submission_available=False)

    def balance(self, *, policy, now):
        if not self.db.in_transaction:
            raise Refused('Transactional cash snapshot required')
        now = timestamp(now)
        day = now.astimezone(IST).date().isoformat()
        row = self.db.execute('SELECT * FROM daily_cash_allocations WHERE day=?', (day,)).fetchone()
        if (not row or row['account'] != self.ledger.account or row['ucc'] != self.accounting.ucc
                or row['policy_digest'] != digest(policy) or timestamp(datetime.fromisoformat(row['reviewed_at'])) > now
                or row['baseline_digest'] != self._history(row['baseline'])):
            raise Refused('Current unchanged daily cash review required')
        from .statements import unresolved
        if any(r['trade_day'] != day for r in unresolved(self.db, now=now)):
            raise Refused('Prior-day charges require reconciliation')
        with localcontext() as ctx:
            ctx.prec = 80
            used, fees = Decimal(0), Decimal(0)
            for entry in self.db.execute('SELECT * FROM intents WHERE rowid>?', (row['baseline'],)):
                body = json.loads(entry['body'])
                if timestamp(datetime.fromisoformat(body['signal_time'])).astimezone(IST).date().isoformat() != day:
                    raise Refused('Intent outside cash allocation day')
                abandoned = self.db.execute('SELECT 1 FROM abandoned_intents WHERE tag=?', (entry['tag'],)).fetchone()
                if abandoned:
                    if entry['broker_id'] or entry['filled'] or self.db.execute('SELECT 1 FROM execution_attempts WHERE tag=?', (entry['tag'],)).fetchone():
                        raise Refused('Abandoned cash reservation was dispatched')
                    continue
                terms = self.db.execute('SELECT fee_reserve FROM execution_terms WHERE tag=?', (entry['tag'],)).fetchone()
                if not terms:
                    raise Refused('Bound cash reservation terms required')
                terminal = entry['status'] in ('FILLED','CANCELLED','REJECTED')
                units = entry['filled'] if terminal else body['quantity']
                used += bounded_amount(body['limit_price'])*units
                recorded = Decimal(0)
                for fill in self.db.execute('SELECT * FROM fill_history WHERE entry_tag=?', (entry['tag'],)):
                    latest = self.db.execute('''SELECT total_fee FROM fee_corrections WHERE segment=? AND trade_day=?
                        AND trade_id=? AND reported_at<=? ORDER BY reported_at DESC LIMIT 1''',
                        (fill['segment'],fill['trade_day'],fill['trade_id'],now.isoformat())).fetchone()
                    recorded += bounded_amount(latest[0] if latest else fill['fee'])
                    if fill['side'] == 'BUY' and bounded_amount(fill['price']) > bounded_amount(body['limit_price']):
                        raise Refused('Fill exceeds reserved entry limit')
                fees += max(recorded, bounded_amount(terms[0]), bounded_amount(policy['limits']['fee_reserve']))
            available = bounded_amount(row['opening_cash'])-used-fees
            if available < 0:
                raise Refused('Daily cash allocation exhausted')
            return dict(day=day, opening_cash=row['opening_cash'], purchase_debits=str(used),
                fee_hold=str(fees), remaining=str(available), basis='reviewed-opening-allocation',
                available_cash_verified=False, fees_verified=False, order_submission_available=False)

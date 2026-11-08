"""Reviewed daily cash allocations. Never a broker balance or dispatch permit.

Legacy reviews retain opening-only spending. Explicit new reviews may recycle
reconciled same-day NSE option principal with fresh position marks. Pending purchases reserve their full quantity. Only an unsent abandoned
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

REUSE_CONFIRMATION = CONFIRMATION + '; REUSE RECONCILED NSE OPTION SALE PROCEEDS'
REUSE_MODEL = 'nse-option-reconciled-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class CashAllocation:
    def __init__(self, accounting):
        self.accounting, self.ledger, self.db = accounting, accounting.ledger, accounting.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS daily_cash_allocations(
            day TEXT PRIMARY KEY, reviewed_at TEXT NOT NULL, account TEXT NOT NULL,
            ucc TEXT NOT NULL, policy_digest TEXT NOT NULL, opening_cash TEXT NOT NULL,
            baseline INTEGER NOT NULL, baseline_digest TEXT NOT NULL)''')
        if 'model' not in {r[1] for r in self.db.execute('PRAGMA table_info(daily_cash_allocations)')}:
            self.db.execute("ALTER TABLE daily_cash_allocations ADD COLUMN model TEXT NOT NULL DEFAULT 'opening-only'")

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
        if actor != self.ledger.account or confirmation not in (CONFIRMATION, REUSE_CONFIRMATION):
            raise Refused('Own-account cash and fee-model review required')
        model = REUSE_MODEL if confirmation == REUSE_CONFIRMATION else 'opening-only'
        cash = bounded_amount(opening_cash)
        if not cash or cash.as_tuple().exponent < -2 or cash > bounded_amount(policy['limits']['capital']):
            raise Refused('Positive opening allocation within reviewed capital required')
        now = datetime.now(timezone.utc)
        day = now.astimezone(IST).date().isoformat()
        with self.ledger.transaction():
            old = self.db.execute('SELECT * FROM daily_cash_allocations WHERE day=?', (day,)).fetchone()
            if old:
                if (old['policy_digest'], old['opening_cash'], old['ucc'], old['model']) != (digest(policy), str(cash), self.accounting.ucc, model):
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
            self.db.execute('INSERT INTO daily_cash_allocations(day,reviewed_at,account,ucc,policy_digest,opening_cash,baseline,baseline_digest,model) VALUES(?,?,?,?,?,?,?,?,?)',
                (day,now.isoformat(),actor,self.accounting.ucc,digest(policy),str(cash),baseline,self._history(baseline),model))
            self.ledger.pause()
            return dict(day=day, duplicate=False, opening_cash=str(cash), order_submission_available=False)

    def _sale_credit(self, baseline, day, now, fee_reserve, marks):
        """Only reconciled NSE long-option principal, never a broker cash claim.

        Existing positions need fresh marks. Profits do not expand
        capital: each position credits at most its reserved acquisition cost.
        """
        # The portal may consume the worker's unchanged snapshot under the
        # account lock. Entry admission separately checks worker-session identity.
        checked = self.db.execute('SELECT * FROM reconciliation_state WHERE id=1').fetchone()
        if (not checked or checked['digest'] != fingerprint(self.ledger) or
                not 0 <= (now-timestamp(datetime.fromisoformat(checked['checked_at']))).total_seconds() <= 30):
            raise Refused('Fresh unchanged reconciliation required for cash reuse')
        if self.db.execute('SELECT 1 FROM live_incidents').fetchone():
            raise Refused('Cash reuse requires incident review')
        if self.db.execute("SELECT 1 FROM broker_commands WHERE status!='CONFIRMED'").fetchone():
            raise Refused('Confirmed broker commands required for cash reuse')
        self.accounting.estimate_budget(now=now, marks=marks, fee_reserve=fee_reserve)
        credit = Decimal(0)
        for entry in self.db.execute('SELECT * FROM intents WHERE rowid>?', (baseline,)):
            body = json.loads(entry['body'])
            sold = self.db.execute('SELECT COALESCE(SUM(filled),0) FROM protective_exits WHERE entry_tag=?', (entry['tag'],)).fetchone()[0]
            if not sold:
                continue
            instrument = self.db.execute('SELECT product FROM reconciliation_instruments WHERE entry_tag=?', (entry['tag'],)).fetchone()
            multiplier = self.db.execute('SELECT multiplier FROM fill_contracts WHERE entry_tag=?', (entry['tag'],)).fetchone()
            if (body['segment'] != 'nse_fo' or body['side'] != 'BUY' or
                    not body['symbol'].endswith(('CE', 'PE')) or not instrument or instrument[0] != 'NRML' or
                    not multiplier or bounded_amount(multiplier[0]) != 1):
                raise Refused('Cash reuse requires bound NSE long options')
            fills = self.db.execute('SELECT * FROM fill_history WHERE entry_tag=? ORDER BY executed_at,trade_id', (entry['tag'],)).fetchall()
            bought = sold_units = 0
            proceeds = Decimal(0)
            for fill in fills:
                if fill['segment'] != 'nse_fo' or fill['trade_day'] != day or timestamp(datetime.fromisoformat(fill['executed_at'])) > now:
                    raise Refused('Same-day same-segment cash evidence required')
                if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='broker_fill_evidence'").fetchone() or not self.db.execute(
                        'SELECT 1 FROM broker_fill_evidence WHERE segment=? AND trade_day=? AND trade_id=?',
                        (fill['segment'], day, fill['trade_id'])).fetchone():
                    raise Refused('Broker fill provenance required for cash reuse')
                if fill['side'] == 'BUY':
                    bought += fill['quantity']
                elif fill['side'] == 'SELL':
                    sold_units += fill['quantity']
                    if sold_units > bought:
                        raise Refused('Sale exceeds acquired units')
                    proceeds += bounded_amount(fill['price'])*fill['quantity']
                else:
                    raise Refused('Invalid cash fill side')
            if sold_units != sold or bought != entry['filled']:
                raise Refused('Complete matched cash fill history required')
            credit += min(proceeds, bounded_amount(body['limit_price'])*sold)
        return credit

    def balance(self, *, policy, now, marks=None):
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
                from .entry_plans import bound_quantity_multiplier
                multiplier = bound_quantity_multiplier(self.ledger, entry['tag'])
                used += bounded_amount(body['limit_price'])*units*multiplier
                recorded = Decimal(0)
                for fill in self.db.execute('SELECT * FROM fill_history WHERE entry_tag=?', (entry['tag'],)):
                    latest = self.db.execute('''SELECT total_fee FROM fee_corrections WHERE segment=? AND trade_day=?
                        AND trade_id=? AND reported_at<=? ORDER BY reported_at DESC LIMIT 1''',
                        (fill['segment'],fill['trade_day'],fill['trade_id'],now.isoformat())).fetchone()
                    recorded += bounded_amount(latest[0] if latest else fill['fee'])
                    if fill['side'] == 'BUY' and bounded_amount(fill['price']) > bounded_amount(body['limit_price']):
                        raise Refused('Fill exceeds reserved entry limit')
                fees += max(recorded, bounded_amount(terms[0]), bounded_amount(policy['limits']['fee_reserve']))
            if row['model'] not in ('opening-only', REUSE_MODEL):
                raise Refused('Unknown daily cash model')
            credit = self._sale_credit(row['baseline'], day, now, policy['limits']['fee_reserve'], marks or {}) if row['model'] == REUSE_MODEL else Decimal(0)
            available = bounded_amount(row['opening_cash'])-used-fees+credit
            if available < 0:
                raise Refused('Daily cash allocation exhausted')
            return dict(day=day, opening_cash=row['opening_cash'], purchase_debits=str(used),
                fee_hold=str(fees), remaining=str(available), sale_credit=str(credit), model=row['model'],
                basis='reviewed-opening-allocation',
                available_cash_verified=False, fees_verified=False, order_submission_available=False)

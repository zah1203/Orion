"""Read-only candidate risk assessment. Never an execution permit.

Caller holds the account lock and ledger transaction. Cash, market status and
fee provenance must be established by reviewed broker adapters before a future
atomic dispatch gate can use these calculations.
"""
from datetime import datetime
import json

from .accounting import IST, bounded_amount, timestamp
from .ledger import Refused, amount, positive_int
from .pilot import validate_limits
from .reconciliation import require_current


def assess(ledger, accounting, limits, candidate, *, now, marks):
    if not ledger.db.in_transaction:
        raise Refused('Transactional risk snapshot required')
    if accounting.ledger is not ledger or candidate['account'] != ledger.account:
        raise Refused('Risk account mismatch')
    binding = ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
    if not binding or accounting.ucc != binding[0]:
        raise Refused('Risk broker account mismatch')
    for table in ('execution_attempts', 'execution_terms', 'reconciliation_state'):
        if not ledger.db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
            raise Refused('Initialized execution risk ledger required')
    limits = validate_limits(limits)
    now = timestamp(now)
    require_current(ledger)
    # Existing exposure still requires protection review. A calculated budget
    # must not silently weaken the ledger's conservative admission gate.
    ledger._check_incidents()
    for _, marked_at in marks.values():
        if not 0 <= (now-timestamp(marked_at)).total_seconds() <= 5:
            raise Refused('Fresh risk marks required')
    summary = accounting.summary(now=now, marks=marks)
    day = now.astimezone(IST).date().isoformat()
    counted = {r['tag'] for r in ledger.db.execute(
        'SELECT tag FROM execution_attempts WHERE day=?', (day,))}
    premium, risk = amount(0), amount(0)
    for row in ledger.db.execute('SELECT * FROM intents'):
        body = json.loads(row['body'])
        if row['event'] == candidate['source']:
            raise Refused('Candidate already reserved')
        if (row['status'] == 'PREPARED' or
                datetime.fromisoformat(body['signal_time']).astimezone(IST).date().isoformat() == day):
            counted.add(row['tag'])
        units = ledger.reserved_units(row)
        if not units:
            continue
        terms = ledger.db.execute('SELECT * FROM execution_terms WHERE tag=?', (row['tag'],)).fetchone()
        if not terms:
            raise Refused('Unaccounted reservation')
        price, stop = bounded_amount(body['limit_price']), bounded_amount(terms['stop'])
        if not 0 < stop < price:
            raise Refused('Invalid reserved stop risk')
        # Policy increases cannot leave older prepared orders under-reserved.
        fee = max(bounded_amount(terms['fee_reserve']), amount(limits['fee_reserve']))
        premium += price*units + fee
        risk += (price-stop)*units + fee
    if len(counted) >= limits['max_entries']:
        raise Refused('Daily entry capacity exhausted')
    price, stop = bounded_amount(candidate['limit_price']), bounded_amount(candidate['stop'])
    if not 0 < stop < price:
        raise Refused('Valid candidate stop required')
    lot = positive_int(candidate['lot_size'])
    maximum = min(positive_int(candidate['lots']), limits['max_lots'])
    fee, loss = amount(limits['fee_reserve']), amount(summary['loss_used'])
    for lots in range(maximum, 0, -1):
        debit = price*lot*lots + fee
        projected_risk = (price-stop)*lot*lots + fee
        if (debit <= amount(limits['max_order_premium']) and
                premium+debit <= min(amount(limits['capital']), amount(limits['max_open_premium'])) and
                projected_risk <= amount(limits['max_trade_loss']) and
                risk+projected_risk <= amount(limits['max_open_risk']) and
                loss+risk+projected_risk < amount(limits['daily_loss'])):
            return dict(lots=lots, quantity=lots*lot, reserved_premium=str(premium),
                reserved_stop_risk=str(risk), candidate_premium=str(debit),
                candidate_stop_risk=str(projected_risk), loss_used=str(loss),
                counted_entries=len(counted), entries_paused=bool(ledger.db.execute(
                    'SELECT paused FROM control WHERE id=1').fetchone()[0]),
                available_cash_verified=False, fees_verified=False,
                order_submission_available=False)
    raise Refused('No whole lot fits remaining ledger risk budget')

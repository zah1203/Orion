"""Read-only candidate risk assessment. Never an execution permit.

Caller holds the account lock and ledger transaction. Cash, market status and
fee provenance must be established by reviewed broker adapters before a future
atomic dispatch gate can use these calculations.
"""
from datetime import datetime
from decimal import localcontext
import json

from .accounting import IST, bounded_amount, timestamp
from .ledger import Refused, amount, positive_int
from .pilot import validate_limits
from .reconciliation import require_current


def candidate_quantity_multiplier(ledger, candidate, now):
    """Validate the admitted MCX source before using quantity-unit economics."""
    source = None
    if ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='live_signals'").fetchone():
        source = ledger.db.execute('SELECT * FROM live_signals WHERE source=?', (candidate['source'],)).fetchone()
    source_segment = json.loads(source['body'])['contract']['segment'] if source else None
    if 'segment' not in candidate and any(key in candidate for key in (
            'commodity_evidence', 'quantity_multiplier', 'premium_multiplier')):
        raise Refused('Explicit candidate segment required for contract economics')
    segment = candidate.get('segment', 'nse_fo')  # Legacy standalone NSE risk fixtures.
    if source_segment is not None and source_segment != segment:
        raise Refused('Candidate source segment mismatch')
    if segment == 'nse_fo':
        return amount(1)
    if segment != 'mcx_fo':
        raise Refused('Unsupported candidate segment')
    if not source or source['state'] != 'PENDING':
        raise Refused('Admitted MCX source required')
    from .commodity import validate_saved_contract
    try:
        normalized = json.loads(source['body'])
        contract, signal = normalized['contract'], normalized['signal']
        economics = validate_saved_contract(contract, now)
        factor = economics.rupees_per_quantity_point
        if (candidate['source_digest'] != source['digest'] or
                candidate['policy_version'] != source['policy_version'] or
                candidate['commodity_evidence'] != contract['commodity_evidence'] or
                candidate['segment'] != contract['segment'] or candidate['symbol'] != contract['symbol'] or
                candidate['token'] != contract['token'] or
                candidate['lot_size'] != economics.order_quantity_per_lot or
                bounded_amount(candidate['tick']) != economics.tick_size or
                bounded_amount(candidate['premium_multiplier']) != economics.premium_rupees_per_lot_point or
                bounded_amount(candidate['quantity_multiplier']) != factor or
                bounded_amount(candidate['stop']) != bounded_amount(signal['stop']) or
                candidate['targets'] != signal['targets'] or
                candidate['quantity'] != economics.broker_quantity(candidate['lots']) or
                not bounded_amount(signal['entry_low']) <= bounded_amount(candidate['limit_price']) <= bounded_amount(signal['entry_high'])):
            raise Refused('MCX candidate economics mismatch')
        economics.aligned_price(candidate['limit_price'])
        return factor
    except (KeyError, TypeError, ValueError):
        raise Refused('MCX candidate economics mismatch') from None


def assess(ledger, accounting, limits, candidate, *, now, marks, cash=None, policy=None):
    # Preserve exact monetary arithmetic independently of caller Decimal context.
    with localcontext() as ctx:
        ctx.prec = 80
        return _assess(ledger, accounting, limits, candidate, now=now,
                       marks=marks, cash=cash, policy=policy)


def _assess(ledger, accounting, limits, candidate, *, now, marks, cash=None, policy=None):
    if not ledger.db.in_transaction:
        raise Refused('Transactional risk snapshot required')
    if accounting.ledger is not ledger or candidate['account'] != ledger.account:
        raise Refused('Risk account mismatch')
    if cash is not None and (cash.ledger is not ledger or cash.accounting is not accounting):
        raise Refused('Cash risk account mismatch')
    binding = ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
    if not binding or accounting.ucc != binding[0]:
        raise Refused('Risk broker account mismatch')
    for table in ('execution_attempts', 'execution_terms', 'reconciliation_state'):
        if not ledger.db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
            raise Refused('Initialized execution risk ledger required')
    limits = validate_limits(limits)
    now = timestamp(now)
    multiplier = candidate_quantity_multiplier(ledger, candidate, now)
    require_current(ledger)
    # Additional exposure requires complete observed stop coverage and stable
    # per-position exit plans, not merely a calculated account budget.
    ledger._check_incidents(allow_protected=True)
    for _, marked_at in marks.values():
        if not 0 <= (now-timestamp(marked_at)).total_seconds() <= 5:
            raise Refused('Fresh risk marks required')
    cash_snapshot = cash.balance(policy=policy, now=now, marks=marks) if cash is not None else None
    summary = (accounting.estimate_budget(now=now, marks=marks, fee_reserve=limits['fee_reserve'])
               if cash is not None else accounting.summary(now=now, marks=marks))
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
        from .entry_plans import bound_quantity_multiplier
        reserved_multiplier = bound_quantity_multiplier(ledger, row['tag'])
        premium += price*units*reserved_multiplier + fee
        risk += (price-stop)*units*reserved_multiplier + fee
    if len(counted) >= limits['max_entries']:
        raise Refused('Daily entry capacity exhausted')
    price, stop = bounded_amount(candidate['limit_price']), bounded_amount(candidate['stop'])
    if not 0 < stop < price:
        raise Refused('Valid candidate stop required')
    lot = positive_int(candidate['lot_size'])
    maximum = min(positive_int(candidate['lots']), limits['max_lots'])
    fee, loss = amount(limits['fee_reserve']), amount(summary['loss_used'])
    if cash_snapshot is not None:
        # Include retained allowances for attempted/cancelled orders without a
        # fill. A trade journal alone cannot account for these possible charges.
        loss += max(amount(0), amount(cash_snapshot['fee_hold'])-amount(summary['fees']))
    for lots in range(maximum, 0, -1):
        debit = price*lot*lots*multiplier + fee
        projected_risk = (price-stop)*lot*lots*multiplier + fee
        if ((cash_snapshot is None or debit <= amount(cash_snapshot['remaining'])) and
                debit <= amount(limits['max_order_premium']) and
                premium+debit <= min(amount(limits['capital']), amount(limits['max_open_premium'])) and
                projected_risk <= amount(limits['max_trade_loss']) and
                risk+projected_risk <= amount(limits['max_open_risk']) and
                loss+risk+projected_risk < amount(limits['daily_loss'])):
            return dict(lots=lots, quantity=lots*lot, reserved_premium=str(premium),
                reserved_stop_risk=str(risk), candidate_premium=str(debit),
                candidate_stop_risk=str(projected_risk), loss_used=str(loss),
                counted_entries=len(counted), entries_paused=bool(ledger.db.execute(
                    'SELECT paused FROM control WHERE id=1').fetchone()[0]),
                cash_allocation=cash_snapshot, available_cash_verified=False, fees_verified=False,
                order_submission_available=False)
    raise Refused('No whole lot fits remaining ledger risk budget')

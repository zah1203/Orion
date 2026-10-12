"""Read-only reconciliation of Paper cash, capital adjustments and retained P&L.

The configured initial balance is an assumption, not an independently audited
deposit. Never repair state or declare broker funds verified from this report.
"""
from decimal import Decimal, InvalidOperation, localcontext


def money(value):
    if isinstance(value, bool):
        raise ValueError('Invalid paper amount')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid paper amount') from None
    if not result.is_finite() or abs(result) > Decimal('1e18') or not -18 <= result.as_tuple().exponent <= 18:
        raise ValueError('Invalid paper amount')
    return result


def reconcile(state, records, starting_cash):
    with localcontext() as context:
        context.prec = 80
        return _reconcile(state, records, starting_cash)


def _reconcile(state, records, starting_cash):
    initial, cash = money(starting_cash), money(state['cash'])
    if initial <= 0 or cash < 0:
        raise ValueError('Invalid paper balance')
    problems, adjustments = set(), []
    realized = cost = Decimal(0)
    positions = {}
    for sid, position in state.get('positions', {}).items():
        if not position.get('entry_fill') or not position.get('lots'):
            continue
        lots, remaining = position['lots'], position['remaining']
        if type(lots) is not int or type(remaining) is not int or not 0 <= remaining <= lots:
            raise ValueError('Invalid paper quantity')
        entry, multiplier = money(position['entry_fill']), money(position['contract']['premium_multiplier'])
        if entry <= 0 or multiplier <= 0:
            raise ValueError('Invalid paper contract economics')
        if position['status'] != ('OPEN' if remaining else 'CLOSED'):
            problems.add('position-status-mismatch')
        pnl = money(position['pnl'])
        realized += pnl
        cost += entry * multiplier * remaining
        positions[sid] = dict(lots=lots, remaining=remaining, entry=entry, multiplier=multiplier, pnl=pnl)
    daily = sum((money(day['pnl']) for day in state.get('days', {}).values()), Decimal(0))
    if daily != realized:
        problems.add('daily-position-pnl-mismatch')
    replay, running, adjustment_total = {}, initial, Decimal(0)
    complete = True
    previous_seq = -1
    for record in records:
        seq, event, body = record['seq'], record['event'], record['body']
        if type(seq) is not int or seq <= previous_seq:
            raise ValueError('Ordered paper audit required')
        previous_seq = seq
        if event == 'PAPER_BALANCE_ADJUSTED':
            before, after, delta = (money(body[k]) for k in ('before', 'after', 'adjustment'))
            if before < 0 or after < 0 or after - before != delta:
                problems.add('invalid-balance-adjustment')
            if complete and before != running:
                problems.add('adjustment-before-mismatch')
            if any(p['remaining'] for p in replay.values()):
                problems.add('adjustment-with-open-position')
            adjustment_total += delta
            # Retain the arithmetic discrepancy; never reset replay to 'after'.
            running += delta
            adjustments.append(dict(seq=seq, at=record['at'], before=str(before),
                                    after=str(after), adjustment=str(delta)))
            continue
        if event not in ('PAPER_BUY', 'PAPER_EXIT', 'TARGET_REACHED'):
            continue
        sid = body.get('signal_id')
        if sid not in positions:
            problems.add('audit-position-missing')
            complete = False
            continue
        position = positions[sid]
        prior = replay.get(sid)
        if (event == 'PAPER_BUY') == (prior is not None):
            problems.add('audit-trade-sequence')
            complete = False
            continue
        # Some old BUY records lack fee/P&L checkpoints. Keep diagnostics but
        # never label their cash replay complete or infer a historical fee.
        if 'pnl' not in body:
            complete = False
            problems.add('legacy-audit-incomplete')
            if event == 'PAPER_BUY':
                replay[sid] = dict(remaining=position['lots'], pnl=Decimal(0))
            continue
        before_pnl = prior['pnl'] if prior else Decimal(0)
        after_pnl = money(body['pnl'])
        delta = after_pnl - before_pnl
        if 'realized_delta' in body and money(body['realized_delta']) != delta:
            problems.add('audit-pnl-delta-mismatch')
        if event == 'PAPER_BUY':
            if type(body.get('lots')) is not int or body['lots'] != position['lots'] or money(body['price']) != position['entry']:
                problems.add('audit-entry-mismatch')
            remaining = position['lots']
            running += delta - position['entry'] * position['multiplier'] * remaining
            if 'fee' in body and delta != -money(body['fee']):
                problems.add('audit-fee-mismatch')
        else:
            sold = prior['remaining'] if event == 'PAPER_EXIT' else body.get('sold_lots')
            if type(sold) is not int or not 0 <= sold <= prior['remaining']:
                raise ValueError('Invalid paper audit quantity')
            remaining = prior['remaining'] - sold
            if 'remaining' in body and body['remaining'] != remaining:
                problems.add('audit-remaining-mismatch')
            running += delta + position['entry'] * position['multiplier'] * sold
            if 'fee' in body and delta != (money(body['price']) - position['entry']) * position['multiplier'] * sold - money(body['fee']):
                problems.add('audit-fee-mismatch')
        replay[sid] = dict(remaining=remaining, pnl=after_pnl)
    for sid, position in positions.items():
        if sid not in replay:
            complete = False
            problems.add('trade-audit-missing')
        elif replay[sid] != {k: position[k] for k in ('remaining', 'pnl')}:
            problems.add('audit-position-totals-mismatch')
    expected = initial + adjustment_total + realized - cost
    unexplained = cash - expected
    if unexplained:
        problems.add('cash-equation-mismatch')
    if complete and running != cash:
        problems.add('audit-cash-mismatch')
    return dict(status='review-required' if problems else 'consistent',
        starting_cash=str(initial), starting_cash_basis='configured-initial-balance',
        net_adjustments=str(adjustment_total), net_contributed_capital=str(initial+adjustment_total),
        realized_pnl=str(realized), daily_realized_pnl=str(daily),
        open_cost_basis=str(cost), expected_cash=str(expected), actual_cash=str(cash),
        unexplained_difference=str(unexplained), audit_complete=complete,
        audit_expected_cash=str(running) if complete else None,
        adjustments=adjustments, issues=sorted(problems),
        order_submission_available=False,
        note='Balance adjustments set current cash and preserve historical P&L. '
             'Cash = configured starting cash + net adjustments + realized P&L '
             '− remaining position cost. Consistency is not independent proof '
             'of starting capital, broker cash or Live readiness.')

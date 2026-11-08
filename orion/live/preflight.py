"""Read-only signal/risk/funding integration; never a BUY authorization.

The caller owns the account's worker lease and authenticated private session.
No cash formula is inferred from RMS balances or a successful margin estimate.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import time

from .accounting import bounded_amount, timestamp
from .funding import FIELDS, SIGNED
from .kotak import OrderRequest
from .ledger import Refused
from .readonly import number

MARGIN_FIELDS = ('avlCash', 'totMrgnUsd', 'mrgnUsd', 'ordMrgn', 'reqdMrgn',
                 'avlMrgn', 'insufFund')


def funding_preflight(router, session, source, *, accounting, marks, cash=None, **quote):
    """Recheck current policy and ledger after the one-shot funding read.

    Do not hold an account/SQLite lock across network I/O. Re-evaluate the
    original quote afterward; changes to sizing, consent, source or channel
    invalidate the result rather than silently applying evidence to a new order.
    Output is transient diagnostic data, not a reusable dispatch permit.
    """
    if router.ledger.db.in_transaction:
        raise Refused('Committed preflight boundary required')
    if session.ucc != router.ucc:
        raise Refused('Funding session account mismatch')
    candidate = router.quote(source, accounting=accounting, marks=marks, cash=cash, **quote)
    if candidate['segment'] != 'nse_fo':
        raise Refused('MCX funding preflight unavailable pending verified cash and margin semantics')
    request = OrderRequest(
        tag='probe'+hashlib.sha256(source.encode()).hexdigest()[:32],
        symbol=candidate['symbol'], segment=candidate['segment'], quantity=candidate['quantity'],
        lot_size=candidate['lot_size'], tick=candidate['tick'],
        price=candidate['limit_price'], kind='ENTRY')
    parameters = request.parameters()
    started, clock = datetime.now(timezone.utc), time.monotonic()
    try:
        evidence = session.request('funding', request=asdict(request), token=candidate['token'])
        if session.ucc != router.ucc:
            raise ValueError
        # Only this read-only operation is ever sent to the session. Do not
        # propagate an SDK exception or unrecognized response fields.
        now = datetime.now(timezone.utc)
        begin, end = timestamp(datetime.fromisoformat(evidence['started_at'])), timestamp(datetime.fromisoformat(evidence['completed_at']))
        before, after = timestamp(datetime.fromisoformat(evidence['rms_before_at'])), timestamp(datetime.fromisoformat(evidence['rms_after_at']))
        if (time.monotonic()-clock > 5 or not started <= begin <= end <= now or
                not 0 <= (now-before).total_seconds() <= 5 or not before <= after <= end or
                evidence['ucc'] != router.ucc or evidence['token'] != candidate['token'] or
                evidence.get('segment', 'nse_fo') != candidate['segment'] or
                evidence['symbol'] != candidate['symbol'] or
                type(evidence['quantity']) is not int or evidence['quantity'] != request.quantity or
                bounded_amount(evidence['price']) != bounded_amount(parameters['price'])):
            raise ValueError
        for flag in ('available_cash_verified', 'fees_verified', 'order_submission_available'):
            if evidence.get(flag) is not False:
                raise ValueError
        limits = {k: number(evidence['reported_limits'][k]) for k in FIELDS}
        if any(v.as_tuple().exponent < -18 or (k not in SIGNED and v < 0) for k,v in limits.items()):
            raise ValueError
        margin = {k: bounded_amount(evidence['reported_margin'][k]) for k in MARGIN_FIELDS}
        shortfall = margin['insufFund'] > 0
        if evidence.get('broker_reports_shortfall') is not shortfall:
            raise ValueError
    except Exception:
        raise Refused('Candidate funding evidence rejected; no order submitted') from None
    current = router.quote(source, accounting=accounting, marks=marks, cash=cash, **quote)
    # Includes reservation/loss/paused changes, not just quantity and price.
    if current != candidate:
        raise Refused('Candidate or ledger changed during funding check')
    if time.monotonic()-clock > 5 or (datetime.now(timezone.utc)-before).total_seconds() > 5:
        raise Refused('Funding expired during policy recheck')
    blockers = list(current['blockers'])
    if shortfall:
        blockers.append('broker-reported-funding-shortfall')
    return dict(candidate=current, blockers=blockers,
                reported_limits={k:str(v) for k,v in limits.items()},
                reported_margin={k:str(v) for k,v in margin.items()},
                checked_at=end.isoformat(), broker_reports_shortfall=shortfall,
                available_cash_verified=False, fees_verified=False,
                order_submission_available=False)

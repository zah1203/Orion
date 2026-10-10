"""Strict read-only Kotak probe. No order-placement methods or token persistence."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import time
from urllib.parse import urlsplit

CODES = ('authentication', 'account-mismatch', 'broker-response', 'schema', 'numeric',
         'routing', 'timeout', 'probe-failed', 'snapshot-too-slow')


class ProbeFailure(ValueError):
    def __init__(self, code):
        self.code = code if code in CODES else 'probe-failed'
        super().__init__(self.code)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ProbeFailure('numeric')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ProbeFailure('numeric') from None
    if not result.is_finite() or abs(result) > Decimal('1e15'):
        raise ProbeFailure('numeric')
    return result


def integer(value):
    n = number(value)
    if n != n.to_integral_value() or n < 0:
        raise ProbeFailure('numeric')
    return int(n)


def envelope(value):
    if not isinstance(value, dict) or any(not isinstance(k, str) or k.lower() in ('error', 'errors', 'error message') for k in value):
        raise ProbeFailure('broker-response')
    if str(value.get('stat', '')).lower() != 'ok' or value.get('stCode') != 200:
        raise ProbeFailure('broker-response')
    return value


def rows(value):
    data = envelope(value).get('data')
    if not isinstance(data, list) or len(data) > 10000 or any(not isinstance(r, dict) for r in data):
        raise ProbeFailure('schema')
    return data


def auth(value):
    if not isinstance(value, dict) or any(not isinstance(k, str) or k.lower() in ('error', 'errors', 'error message') for k in value):
        raise ProbeFailure('authentication')
    data = value.get('data')
    if not isinstance(data, dict) or data.get('status') != 'success':
        raise ProbeFailure('authentication')
    if any(not isinstance(data.get(k), str) or not 1 <= len(data[k]) <= 16384 for k in ('token','sid')):
        raise ProbeFailure('authentication')
    return data


def account_match(value, expected):
    if not isinstance(value, str) or value != expected:
        raise ProbeFailure('account-mismatch')


def summarize(orders, positions, limits, expected):
    orders, positions, limits = rows(orders), rows(positions), envelope(limits)
    ids = set()
    working = 0
    for order in orders:
        account_match(order.get('actId'), expected)
        oid = order.get('nOrdNo')
        if not isinstance(oid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', oid) or oid in ids:
            raise ProbeFailure('schema')
        ids.add(oid)
        quantity, filled = integer(order.get('qty')), integer(order.get('fldQty'))
        if not quantity or filled > quantity:
            raise ProbeFailure('schema')
        state = order.get('ordSt')
        if state not in ('open', 'trigger pending', 'complete', 'cancelled', 'rejected'):
            raise ProbeFailure('schema')
        if (state == 'complete' and filled != quantity) or (state == 'rejected' and filled):
            raise ProbeFailure('schema')
        working += state in ('open', 'trigger pending')
    nonzero = 0
    for position in positions:
        account_match(position.get('actId'), expected)
        net = (integer(position.get('cfBuyQty')) + integer(position.get('flBuyQty')) -
               integer(position.get('cfSellQty')) - integer(position.get('flSellQty')))
        nonzero += net != 0
    # RMS Net can be negative; it is not cash or approved trading capital.
    net = number(limits.get('Net'))
    entity = limits.get('EntityId')
    if entity not in (None, ''):
        account_match(entity, expected)
    return dict(order_count=len(orders), working_order_count=working,
                position_count=len(positions), nonzero_position_count=nonzero,
                rms_net=str(net), identity_matched=True,
                live_available=False, order_submission_available=False)


def probe(client, creds, totp):
    """Client is a fresh SDK instance, never an existing worker's feed client."""
    expected = creds['kotak_ucc']
    if not isinstance(expected, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', expected):
        raise ProbeFailure('account-mismatch')
    first = auth(client.totp_login(mobile_number=creds['kotak_mobile'], ucc=expected, totp=totp))
    account_match(first.get('ucc'), expected)
    second = auth(client.totp_validate(mpin=creds['kotak_mpin']))
    if second.get('kType') != 'Trade':
        raise ProbeFailure('authentication')
    account_match(getattr(client.configuration, 'ucc', None), expected)
    if second.get('ucc') is not None:
        account_match(second['ucc'], expected)
    url = urlsplit(second.get('baseUrl', ''))
    if (url.scheme != 'https' or not url.hostname or
            not url.hostname.endswith('.kotaksecurities.com') or url.username or url.password or
            url.port not in (None,443) or url.query or url.fragment):
        raise ProbeFailure('routing')
    started = time.monotonic()
    report = summarize(client.order_report(), client.positions(), client.limits(), expected)
    if time.monotonic() - started > 20:
        raise ProbeFailure('snapshot-too-slow')
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    return report

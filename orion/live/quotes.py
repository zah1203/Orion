"""Strict Kotak REST depth evidence. Fresh prices do not prove market-open state."""
from datetime import datetime, timezone
import re

from .accounting import bounded_amount
from .ledger import Refused
from .readonly import integer
from .reconciliation import text_field


class QuoteUnavailable(Refused):
    """Correct instrument, but no current price; book reads may still work."""


def instruments(values):
    """Bind identity by segment and token, never by a token alone.

    Legacy two-field requests are confined to NSE. MCX requires an explicit
    segment; there is no product-name inference or alternate-exchange retry.
    """
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise Refused('One to fifty bound instruments required')
    result = {}
    for row in values:
        if not isinstance(row, dict) or set(row) not in ({'token','symbol'}, {'segment','token','symbol'}):
            raise Refused('Explicit instrument identity required')
        segment = row.get('segment', 'nse_fo')
        if segment not in ('nse_fo', 'mcx_fo'):
            raise Refused('Explicit supported derivatives segment required')
        token, symbol = text_field(row['token']), text_field(row['symbol'])
        key = (segment, token)
        if not re.fullmatch(r'[0-9]{1,12}', token) or key in result:
            raise Refused('Unique segment-bound instrument tokens required')
        result[key] = symbol
    return result


def normalize_quotes(response, expected, *, now):
    if not isinstance(response, list) or len(response) != len(expected):
        raise Refused('Complete quote response required')
    result, seen = [], set()
    for row in response:
        if not isinstance(row, dict):
            raise Refused('Invalid quote response')
        token, segment = row.get('exchange_token'), row.get('exchange')
        if not isinstance(token, str) or not isinstance(segment, str):
            raise Refused('Quote identity mismatch')
        key = (segment, token)
        if key not in expected or key in seen or row.get('display_symbol') != expected[key]:
            raise Refused('Quote identity mismatch')
        seen.add(key)
        at = datetime.fromtimestamp(integer(row.get('lstup_time')), timezone.utc)
        if not 0 <= (now-at).total_seconds() <= 5:
            raise QuoteUnavailable('Stale broker quote')
        sides = []
        for name, reverse in (('buy', True), ('sell', False)):
            depth = row.get('depth', {}).get(name)
            if not isinstance(depth, list) or not 1 <= len(depth) <= 5:
                raise Refused('Nonempty bounded market depth required')
            prices = []
            for level in depth:
                price = bounded_amount(level.get('price'))
                if not price or not integer(level.get('quantity')):
                    raise Refused('Positive quoted depth required')
                prices.append(price)
            if prices != sorted(prices, reverse=reverse) or len(set(prices)) != len(prices):
                raise Refused('Unordered broker depth')
            sides.append(prices[0])
        bid, ask = sides
        if bid > ask:
            raise Refused('Crossed broker quote')
        result.append(dict(segment=segment,token=token,symbol=expected[key],bid=str(bid),ask=str(ask),
                           quoted_at=at.isoformat(),observed_at=now.isoformat(),market_open_verified=False))
    return result

"""Strict Kotak REST depth evidence. Fresh prices do not prove market-open state."""
from datetime import datetime, timezone
import re

from .accounting import bounded_amount
from .ledger import Refused
from .readonly import integer
from .reconciliation import text_field


def instruments(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise Refused('One to fifty bound instruments required')
    result = {}
    for row in values:
        if not isinstance(row, dict) or set(row) != {'token','symbol'}:
            raise Refused('Explicit instrument identity required')
        token, symbol = text_field(row['token']), text_field(row['symbol'])
        if not re.fullmatch(r'[0-9]{1,12}', token) or token in result:
            raise Refused('Unique NSE instrument tokens required')
        result[token] = symbol
    return result


def normalize_quotes(response, expected, *, now):
    if not isinstance(response, list) or len(response) != len(expected):
        raise Refused('Complete quote response required')
    result, seen = [], set()
    for row in response:
        if not isinstance(row, dict):
            raise Refused('Invalid quote response')
        token = row.get('exchange_token')
        if token not in expected or token in seen or row.get('exchange') != 'nse_fo' or row.get('display_symbol') != expected[token]:
            raise Refused('Quote identity mismatch')
        seen.add(token)
        at = datetime.fromtimestamp(integer(row.get('lstup_time')), timezone.utc)
        if not 0 <= (now-at).total_seconds() <= 5:
            raise Refused('Stale broker quote')
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
        result.append(dict(segment='nse_fo',token=token,symbol=expected[token],bid=str(bid),ask=str(ask),
                           quoted_at=at.isoformat(),observed_at=now.isoformat(),market_open_verified=False))
    return result

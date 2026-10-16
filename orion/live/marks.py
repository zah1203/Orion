"""Account-bound broker bid marks for existing exposure, never entry authority."""
from datetime import datetime, timezone
import json

from .accounting import bounded_amount, timestamp
from .ledger import Refused, amount
from .quotes import instruments


def collect_marks(ledger, session):
    """Read the existing ledger's exact contracts through its leased session.

    No symbol lookup, alternate account, cached price or last-trade substitution.
    The strategy must check freshness again after collecting broker books.
    """
    binding = ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
    if not binding or binding[0] != session.ucc:
        raise Refused('Quote session account mismatch')
    expected, entries = {}, []
    for entry in ledger.db.execute('SELECT * FROM intents').fetchall():
        sold = ledger.db.execute('SELECT COALESCE(SUM(filled),0) FROM protective_exits WHERE entry_tag=?', (entry['tag'],)).fetchone()[0]
        if entry['filled'] <= sold:
            continue
        body = json.loads(entry['body'])
        instrument = ledger.db.execute('SELECT * FROM reconciliation_instruments WHERE entry_tag=?', (entry['tag'],)).fetchone()
        terms = ledger.db.execute('SELECT tick FROM execution_terms WHERE tag=?', (entry['tag'],)).fetchone()
        if not instrument or not terms or body['segment'] not in ('nse_fo', 'mcx_fo') or instrument['product'] != 'NRML':
            raise Refused('Bound derivatives exposure required')
        if body['segment'] == 'mcx_fo':
            from .entry_plans import bound_quantity_multiplier
            bound_quantity_multiplier(ledger, entry['tag'])
        token, symbol = instrument['token'], body['symbol']
        key = (body['segment'], token)
        if key in expected and expected[key] != symbol:
            raise Refused('Conflicting instrument identity')
        expected[key] = symbol
        entries.append((entry['tag'], key, amount(terms['tick'])))
    if not entries:
        return {}
    requested = [dict(token=token, symbol=symbol, **({'segment': segment} if segment != 'nse_fo' else {}))
                 for (segment, token), symbol in expected.items()]
    instruments(requested)
    response = session.request('quotes', instruments=requested)
    now = datetime.now(timezone.utc)
    if not isinstance(response, dict) or response.get('ucc') != binding[0]:
        raise Refused('Quote response account mismatch')
    quotes = response.get('quotes')
    if not isinstance(quotes, list) or len(quotes) != len(expected):
        raise Refused('Complete bound quotes required')
    prices = {}
    for row in quotes:
        if not isinstance(row, dict):
            raise Refused('Invalid bound quote')
        key = (row.get('segment'), row.get('token'))
        if key not in expected or key in prices or row.get('symbol') != expected[key]:
            raise Refused('Quote contract mismatch')
        at = timestamp(datetime.fromisoformat(row['quoted_at']))
        observed = timestamp(datetime.fromisoformat(row['observed_at']))
        if not 0 <= (now-at).total_seconds() <= 5 or not at <= observed <= now:
            raise Refused('Fresh broker bid required')
        bid, ask = bounded_amount(row.get('bid')), bounded_amount(row.get('ask'))
        if not 0 < bid <= ask:
            raise Refused('Valid broker depth required')
        prices[key] = (bid, ask, at)
    result = {}
    for tag, key, tick in entries:
        bid, ask, at = prices[key]
        if tick <= 0 or bid % tick or ask % tick:
            raise Refused('Tick-aligned broker depth required')
        result[tag] = (str(bid), at)
    return result

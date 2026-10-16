"""Dated MCX catalogue attestation for offline admission, never trade authority.

The digest detects changed saved data; it is not a signature or proof that an
operator's economics are true. Broker acceptance/cash/fees remain separate gates.
"""
from datetime import date, datetime
import hashlib
import json
import re

from .accounting import IST, timestamp
from .contract_economics import ContractEconomics
from .ledger import Refused

PRODUCTS = frozenset(('CRUDEOIL', 'CRUDEOILM', 'GOLD', 'GOLDM', 'SILVER', 'SILVERM'))
FIELDS = ('product', 'strike', 'option_type', 'segment', 'expiry', 'expiry_at',
          'symbol', 'token', 'order_quantity_per_lot', 'premium_multiplier', 'tick_size')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _identity(contract):
    try:
        return {k: contract[k] for k in FIELDS}
    except (TypeError, KeyError):
        raise Refused('Complete commodity contract identity required') from None


def _contract(contract, now):
    now = timestamp(now)
    identity = _identity(contract)
    if (identity['segment'] != 'mcx_fo' or identity['product'] not in PRODUCTS or
            identity['option_type'] not in ('CE', 'PE')):
        raise Refused('Supported exact MCX option identity required')
    try:
        expiry = timestamp(datetime.fromisoformat(identity['expiry_at']))
        day = date.fromisoformat(identity['expiry'])
    except (ValueError, TypeError):
        raise Refused('Aware commodity option expiry required') from None
    if day != expiry.astimezone(IST).date() or day <= now.astimezone(IST).date():
        raise Refused('MCX expiry-day admission is disabled pending devolvement policy')
    return ContractEconomics.from_contract(contract)


def review_contract(contract, master, now):
    """Require a fresh explicitly attested catalogue; no inferred conversions."""
    economics = _contract(contract, now)
    now = timestamp(now)
    today = now.astimezone(IST).date().isoformat()
    try:
        profile = master['economics']
        rule = profile['products'][contract['product']]
        verified_at = timestamp(datetime.fromisoformat(master['verified_at']))
        source_digest = hashlib.sha256(json.dumps(master['sources'], sort_keys=True).encode()).hexdigest()
        hashes = (master['source_sha256'], master['sources']['mcx_fo'])
        if (master.get('synthetic') is not False or master['as_of'] != today or
                master['source_sha256'] != source_digest or
                profile['verified_on'] != today or not isinstance(profile['source'], str) or
                not profile['source'].strip() or master.get('economics_reused') is not False or
                verified_at > now or verified_at.astimezone(IST).date().isoformat() != today or
                any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in hashes) or
                contract['order_quantity_per_lot'] not in rule['verified_lot_sizes'] or
                not isinstance(rule['verified_lot_sizes'], list) or
                any(type(v) is not int or v <= 0 for v in rule['verified_lot_sizes']) or
                not isinstance(rule['price_units'], str) or not rule['price_units'].strip() or
                not isinstance(rule['delivery_units'], str) or not rule['delivery_units'].strip()):
            raise ValueError
        from .accounting import bounded_amount
        if bounded_amount(rule['premium_rupees_per_quantity_point']) != economics.rupees_per_quantity_point:
            raise ValueError
        expiry = datetime.fromisoformat(contract['expiry_at']).astimezone(IST)
        if expiry.time().isoformat() != rule['expiry_time_ist']:
            raise ValueError
        evidence = dict(version=1, verified_on=today, source_sha256=hashes[0],
                        mcx_source_sha256=hashes[1], profile_sha256=_hash(profile),
                        contract_sha256=_hash(_identity(contract)))
        return evidence
    except (KeyError, TypeError, ValueError):
        raise Refused('Fresh explicitly reviewed MCX economics and source provenance required') from None


def validate_saved_contract(contract, now):
    """Check persisted identity/economics/date before reuse; never refresh it."""
    economics = _contract(contract, now)
    today = timestamp(now).astimezone(IST).date().isoformat()
    try:
        evidence = contract['commodity_evidence']
        if (type(evidence['version']) is not int or evidence['version'] != 1 or
                evidence['verified_on'] != today or evidence['contract_sha256'] != _hash(_identity(contract)) or
                any(not isinstance(evidence[k], str) or not re.fullmatch('[0-9a-f]{64}', evidence[k])
                    for k in ('source_sha256', 'mcx_source_sha256', 'profile_sha256'))):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise Refused('Unchanged dated MCX contract evidence required') from None
    return economics

"""Sanitized RMS observations, not a spendable-cash calculation.

Field names follow the official SDK limits example. Neither RMS Net nor the
margin endpoint's avlCash is promoted to verified cash or final charges.
"""
from datetime import datetime, timezone
import re

from .ledger import Refused
from .readonly import envelope, number

FIELDS = ('CollateralValue', 'Collateral', 'RmsCollateral', 'AdhocMargin',
          'NotionalCash', 'MarginUsed', 'RmsPayInAmt', 'RmsPayOutAmt', 'Net',
          'BrokeragePrsnt', 'FoPremiumNrmlPrsnt', 'FoPremiumMisPrsnt',
          'UnrealizedMtomPrsnt', 'RealizedMtomPrsnt')
SIGNED = {'Net', 'UnrealizedMtomPrsnt', 'RealizedMtomPrsnt'}


def limits_observation(response, ucc, *, now):
    data = envelope(response)
    if type(data.get('stCode')) is not int or data.get('errMsg') not in (None, ''):
        raise Refused('RMS response rejected')
    # Blank is the documented example. A nonempty unexpected identity is not
    # silently accepted; actual account schema compatibility remains to be tested.
    if data.get('EntityId') not in ('', ucc):
        raise Refused('RMS identity requires review')
    raw_stamp = data.get('TimeStamp')
    if not isinstance(raw_stamp, str) or not re.fullmatch(r'[0-9]{13}', raw_stamp):
        raise Refused('Broker RMS timestamp required')
    at = datetime.fromtimestamp(int(raw_stamp)/1000, timezone.utc)
    if not 0 <= (now-at).total_seconds() <= 5:
        raise Refused('Fresh broker RMS observation required')
    values = {}
    for key in FIELDS:
        value = number(data.get(key))
        if value.as_tuple().exponent < -18 or (key not in SIGNED and value < 0):
            raise Refused('RMS numeric field requires review')
        values[key] = value
    return values, at

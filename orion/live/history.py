"""Immutable terminal-order evidence for books that roll over each day.

History is sealed only from reconciled books and complete local fill evidence.
It never turns a missing working/unknown order into a cancellation. No network,
broker archive download or production scheduled collector is provided here.
"""
from datetime import datetime
from decimal import Decimal, localcontext
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

from .ledger import Refused, amount
from .readonly import rows

IST = ZoneInfo('Asia/Kolkata')


def evidence(ledger, oid):
    """Bind a sealed observation to immutable local order/fill/instrument facts."""
    db = ledger.db
    entry = db.execute('SELECT * FROM intents WHERE broker_id=?', (oid,)).fetchone()
    exit_row = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='protective_exits'").fetchone():
        exit_row = db.execute('SELECT * FROM protective_exits WHERE broker_id=?', (oid,)).fetchone()
    if bool(entry) == bool(exit_row):
        raise Refused('Exactly one local order binding required')
    order = entry if entry else exit_row
    if order['status'] not in ('FILLED','CANCELLED','REJECTED'):
        raise Refused('Only terminal orders can be archived')
    tag = entry['tag'] if entry else exit_row['entry_tag']
    binding = db.execute('SELECT * FROM reconciliation_instruments WHERE entry_tag=?', (tag,)).fetchone()
    contract = db.execute('SELECT * FROM fill_contracts WHERE entry_tag=?', (tag,)).fetchone()
    if not binding or not contract:
        raise Refused('Historical instrument binding required')
    fills = [dict(r) for r in db.execute('SELECT * FROM fill_history WHERE broker_id=? ORDER BY segment,trade_day,trade_id', (oid,))]
    if sum(r['quantity'] for r in fills) != order['filled']:
        raise Refused('Complete historical fills required')
    canonical = dict(order)
    if 'average' in canonical:
        canonical['average'] = format(amount(canonical['average']).normalize(), 'f')
    value = dict(account=ledger.account, order=canonical, binding=dict(binding),
                 contract=dict(contract), fills=fills)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(), fills


def expand(ledger, report, as_of):
    """Use earlier-day terminal evidence only; validate it again on every read."""
    current = rows(report)
    present = {r.get('nOrdNo') for r in current}
    day = as_of.astimezone(IST).date().isoformat()
    result = list(current)
    try:
        for row in ledger.db.execute('SELECT * FROM terminal_history ORDER BY broker_id'):
            if row['broker_id'] in present or row['observed_day'] >= day:
                continue
            observed = datetime.fromisoformat(row['observed_at'])
            if observed.tzinfo is None or observed > as_of or observed.astimezone(IST).date().isoformat() != row['observed_day']:
                raise Refused('Historical date invalid')
            digest, _ = evidence(ledger, row['broker_id'])
            if hashlib.sha256((digest+row['body']).encode()).hexdigest() != row['evidence_digest']:
                raise Refused('Historical order or fill evidence changed')
            result.append(json.loads(row['body']))
    except (sqlite3.Error, ValueError, TypeError):
        raise Refused('Historical evidence invalid') from None
    return dict(stat='Ok', stCode=200, data=result)


def seal(ledger, ucc, orders, positions, *, started_at, completed_at, marks):
    """Seal terminal rows after fresh full-book and fill-journal validation.

    marks are required for any carried exposure, using the accounting freshness
    rules. Fees may still be estimates; sealing does not certify broker charges.
    """
    from .accounting import Accounting
    from .reconciliation import Reconciliation, normalize_orders
    accounting = Accounting(ledger, ucc)
    reconciliation = Reconciliation(ledger, ucc)
    db = ledger.db
    try:
        with ledger.transaction(), localcontext() as context:
            context.prec = 80
            if db.execute('SELECT 1 FROM live_incidents').fetchone():
                raise Refused('Resolve incidents before sealing history')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='broker_commands'").fetchone():
                if db.execute("SELECT 1 FROM broker_commands WHERE status!='CONFIRMED'").fetchone():
                    raise Refused('Confirm all commands before sealing history')
            reconciliation.compare(orders, positions, started_at=started_at, completed_at=completed_at, complete=True)
            accounting.summary(now=completed_at, marks=marks)
            count = 0
            for oid, order in normalize_orders(orders, ucc).items():
                if order['status'] not in ('FILLED','CANCELLED','REJECTED'):
                    continue
                digest, fills = evidence(ledger, oid)
                entry = db.execute('SELECT body FROM intents WHERE broker_id=?', (oid,)).fetchone()
                if entry and completed_at < datetime.fromisoformat(json.loads(entry[0])['signal_time']):
                    raise Refused('Historical observation predates entry')
                total = sum((amount(f['price'])*f['quantity'] for f in fills), Decimal(0))
                if total != order['average']*order['filled']:
                    raise Refused('Historical fill average mismatch')
                segment, product, token, symbol = order['instrument']
                raw = dict(actId=ucc, nOrdNo=oid, exSeg=segment, prod=product, tok=token,
                    trdSym=symbol, qty=order['quantity'], fldQty=order['filled'],
                    ordSt={'FILLED':'complete','CANCELLED':'cancelled','REJECTED':'rejected'}[order['status']],
                    trnsTp=order['side'], avgPrc=format(order['average'].normalize(), 'f'), prc=format(order['price'].normalize(), 'f'),
                    trgPrc=format(order['trigger'].normalize(), 'f'), prcTp=order['order_type'])
                encoded = json.dumps(raw, sort_keys=True)
                digest = hashlib.sha256((digest+encoded).encode()).hexdigest()
                old = db.execute('SELECT body,evidence_digest FROM terminal_history WHERE broker_id=?', (oid,)).fetchone()
                if old:
                    if tuple(old) != (encoded, digest):
                        raise Refused('Sealed order cannot change')
                    continue
                db.execute('INSERT INTO terminal_history VALUES(?,?,?,?,?)',
                    (oid, completed_at.astimezone(IST).date().isoformat(), completed_at.isoformat(), encoded, digest))
                count += 1
            # Archive changes are included in the fingerprint before recording a
            # current reconciliation result. No I/O occurs inside this transaction.
            reconciliation.compare(orders, positions, started_at=started_at, completed_at=completed_at, complete=True)
        return count
    except Exception:
        with ledger.transaction():
            db.execute("INSERT OR IGNORE INTO live_incidents VALUES('history-evidence-conflict')")
            db.execute('DELETE FROM reconciliation_state')
            ledger.pause()
        raise Refused('Historical evidence requires review') from None

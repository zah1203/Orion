"""Strict current-day trade evidence; no cash or fee certification.

Kotak's trade report can omit actId and tok. Identity comes from the checked
session and an exact order-ID join to the account-bound order report. A nonempty
conflicting identity is rejected, never replaced. SDK fields not needed by the
ledger are discarded before crossing the private session process boundary.
"""
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from .accounting import Accounting, IST, bounded_amount, timestamp
from .ledger import Refused
from .readonly import integer, rows
from .reconciliation import text_field


def normalize_trades(report, ucc, orders, *, completed_at):
    completed_at = timestamp(completed_at)
    result, seen = [], set()
    for row in rows(report):
        oid = text_field(row.get('nOrdNo'))
        order = orders.get(oid)
        if not order or row.get('actId') not in ('', ucc):
            raise Refused('Trade account/order mismatch')
        segment, product, token, symbol = order['instrument']
        if (segment != 'nse_fo' or product != 'NRML' or
                (row.get('exSeg'), row.get('prod'), row.get('trdSym')) != (segment, product, symbol) or
                row.get('tok') not in ('', token) or row.get('trnsTp') != order['side'] or
                row.get('rptTp') != 'fill'):
            raise Refused('Trade instrument or side mismatch')
        # Only the NSE unit-premium contract supported by this pilot is allowed.
        if any(bounded_amount(row.get(k)) != 1 for k in ('multiplier', 'genDen', 'genNum', 'prcNum', 'prcDen')):
            raise Refused('Unsupported trade price conversion')
        quantity = integer(row.get('fldQty'))
        lot_size = integer(row.get('lotSz'))
        price = bounded_amount(row.get('avgPrc'))
        if not 0 < quantity <= order['filled'] or not lot_size or not price:
            raise Refused('Invalid trade quantity or price')
        # Partial fills may be odd lots; preserve exact units, never round them.
        if row.get('prc') not in ('', None) and bounded_amount(row['prc']) != price:
            raise Refused('Ambiguous trade price')
        try:
            value = row['flDt'] + ' ' + row['flTm']
            at = datetime.strptime(value, '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
        except (ValueError, TypeError, KeyError):
            raise Refused('Invalid broker fill time') from None
        if at > completed_at or at.date() != completed_at.astimezone(IST).date():
            raise Refused('Trade outside current broker day')
        trade_id = text_field(row.get('flId'))
        identity = (segment, at.date().isoformat(), trade_id)
        if identity in seen:
            raise Refused('Duplicate trade in broker report')
        seen.add(identity)
        result.append(dict(broker_id=oid, trade_id=trade_id, segment=segment, symbol=symbol,
                           side='BUY' if order['side'] == 'B' else 'SELL', quantity=quantity,
                           price=str(price), executed_at=at.astimezone(timezone.utc).isoformat(),
                           lot_size=lot_size))
    totals = {}
    with localcontext() as ctx:
        ctx.prec = 80
        for trade in result:
            qty, value = totals.get(trade['broker_id'], (0, Decimal(0)))
            totals[trade['broker_id']] = (qty+trade['quantity'], value+trade['quantity']*Decimal(trade['price']))
        for oid, (quantity, value) in totals.items():
            if quantity > orders[oid]['filled']:
                raise Refused('Trades exceed observed fills')
            if quantity == orders[oid]['filled'] and value != quantity*orders[oid]['average']:
                raise Refused('Trade prices disagree with order average')
    return result


class TradeJournal:
    """Atomically ingest sanitized broker trades after matching order observations.

    The trade endpoint contains no verified charges. Zero is a placeholder in
    fill_history, explicitly marked unverified; Accounting.summary refuses to
    turn these records into a spendable risk budget. No broker fee is assumed zero.
    """
    def __init__(self, ledger, ucc):
        self.ledger, self.db, self.ucc = ledger, ledger.db, ucc
        self.accounting = Accounting(ledger, ucc)
        self.db.execute('''CREATE TABLE IF NOT EXISTS broker_fill_evidence(
            segment TEXT NOT NULL, trade_day TEXT NOT NULL, trade_id TEXT NOT NULL,
            observed_at TEXT NOT NULL, fee_status TEXT NOT NULL CHECK(fee_status='unverified'),
            PRIMARY KEY(segment,trade_day,trade_id))''')

    def ingest(self, snapshot):
        try:
            if snapshot['ucc'] != self.ucc or snapshot.get('fees_verified') is not False:
                raise Refused('Broker trade provenance mismatch')
            completed = timestamp(datetime.fromisoformat(snapshot['completed_at']))
            now = datetime.now(timezone.utc)
            if not 0 <= (now-completed).total_seconds() <= 30:
                raise Refused('Stale trade evidence')
            trades = snapshot['trades']
            if not isinstance(trades, list) or len(trades) > 10000:
                raise Refused('Invalid trade evidence')
            seen, inserted = set(), 0
            with self.ledger.transaction():
                for trade in trades:
                    at = timestamp(datetime.fromisoformat(trade['executed_at']))
                    if at > completed or at.astimezone(IST).date() != completed.astimezone(IST).date():
                        raise Refused('Invalid trade evidence day')
                    key = (trade['segment'], at.astimezone(IST).date().isoformat(), trade['trade_id'])
                    if key in seen:
                        raise Refused('Duplicate trade evidence')
                    seen.add(key)
                    orders = self.accounting._orders()
                    if trade['broker_id'] not in orders:
                        raise Refused('Unbound trade')
                    _, tag, _ = orders[trade['broker_id']]
                    terms = self.db.execute('SELECT lot_size FROM execution_terms WHERE tag=?', (tag,)).fetchone()
                    if not terms or type(trade['lot_size']) is not int or trade['lot_size'] != terms[0]:
                        raise Refused('Trade lot size mismatch')
                    existing = self.db.execute('SELECT 1 FROM fill_history WHERE segment=? AND trade_day=? AND trade_id=?', key).fetchone()
                    evidence = self.db.execute('SELECT 1 FROM broker_fill_evidence WHERE segment=? AND trade_day=? AND trade_id=?', key).fetchone()
                    if bool(existing) != bool(evidence):
                        raise Refused('Trade provenance changed')
                    inserted += self.accounting.record(account=self.ledger.account, ucc=self.ucc,
                        **{k:trade[k] for k in ('trade_id','broker_id','segment','symbol','side','quantity','price')},
                        fee='0', executed_at=at)
                    self.db.execute('INSERT OR IGNORE INTO broker_fill_evidence VALUES(?,?,?,?,?)',
                                    (*key, completed.isoformat(), 'unverified'))
                # Full current-day report: loss of previously recorded evidence
                # is not interpreted as a correction or an empty trading day.
                day = completed.astimezone(IST).date().isoformat()
                prior = {tuple(r) for r in self.db.execute('SELECT segment,trade_day,trade_id FROM broker_fill_evidence WHERE trade_day=?', (day,))}
                if prior != seen:
                    raise Refused('Previously recorded trade disappeared')
                for oid, (order, _, _) in self.accounting._orders().items():
                    total = self.db.execute('SELECT COALESCE(SUM(quantity),0) FROM fill_history WHERE broker_id=?', (oid,)).fetchone()[0]
                    if total != order['filled']:
                        raise Refused('Incomplete broker fill report')
            return inserted
        except Exception:
            with self.ledger.transaction():
                self.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('fill-history-conflict')")
                self.ledger.pause()
            raise Refused('Broker trade evidence requires review') from None

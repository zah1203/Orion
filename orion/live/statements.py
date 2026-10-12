"""Human-reviewed fee statements matched to immutable broker fill evidence.

This is a normalized statement importer, not a Kotak PDF parser. The caller must
review the actual private statement and its transcription. Nothing here guesses
charges, changes fills, authenticates to a broker or enables order submission.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json

from .accounting import IST, bounded_amount, timestamp
from .ledger import Refused
from .reconciliation import text_field

CHARGES = ('brokerage', 'exchange', 'sebi', 'stt', 'stamp', 'gst', 'other')


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS reviewed_statements(
        review_id TEXT PRIMARY KEY, trade_day TEXT NOT NULL, reviewed_at TEXT NOT NULL,
        proof_digest TEXT NOT NULL, normalized_digest TEXT NOT NULL,
        total_fee TEXT NOT NULL, supersedes TEXT, actor TEXT NOT NULL, normalized TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS reviewed_statement_lines(
        review_id TEXT NOT NULL, segment TEXT NOT NULL, trade_day TEXT NOT NULL,
        trade_id TEXT NOT NULL, total_fee TEXT NOT NULL,
        PRIMARY KEY(review_id,segment,trade_day,trade_id))''')


def unresolved(db, *, now=None):
    """A correction alone never certifies fees; require its exact reviewed source."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='broker_fill_evidence'").fetchone():
        return []
    rows = db.execute("SELECT * FROM broker_fill_evidence WHERE fee_status='unverified'").fetchall()
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='reviewed_statements'").fetchone():
        return rows
    at = timestamp(now or datetime.now(timezone.utc)).isoformat()
    pending = []
    for row in rows:
        key = (row['segment'], row['trade_day'], row['trade_id'])
        latest = db.execute('''SELECT * FROM reviewed_statements WHERE trade_day=? AND reviewed_at<=?
            ORDER BY reviewed_at DESC, rowid DESC LIMIT 1''', (row['trade_day'], at)).fetchone()
        line = db.execute('''SELECT total_fee FROM reviewed_statement_lines
            WHERE review_id=? AND segment=? AND trade_day=? AND trade_id=?''',
            (latest['review_id'], *key)).fetchone() if latest else None
        correction = db.execute('''SELECT source_ref,total_fee FROM fee_corrections
            WHERE segment=? AND trade_day=? AND trade_id=? AND reported_at<=?
            ORDER BY reported_at DESC LIMIT 1''', (*key, at)).fetchone()
        if (not line or not correction or correction['source_ref'] != latest['review_id'] or
                bounded_amount(correction['total_fee']) != bounded_amount(line[0])):
            pending.append(row)
    return pending


def review(accounting, *, actor, proof, statement, confirmation):
    """Atomically record a complete reviewed day and append per-fill corrections.

    proof is the private original document's bytes; only its SHA-256 is retained.
    No assertion that an uploaded document is authentic replaces human review.
    Full fill identities/quantities/prices and allocated total charges must agree.
    A later correction invalidates review until a complete superseding review.
    """
    ledger, db = accounting.ledger, accounting.db
    if actor != ledger.account or confirmation != 'I REVIEWED THE BROKER STATEMENT':
        raise Refused('Account-owner statement review required')
    if not isinstance(proof, bytes) or not 1 <= len(proof) <= 10*1024*1024:
        raise Refused('Private original statement bytes required')
    required = {'account','ucc','trade_day','charges','fills','supersedes'}
    if not isinstance(statement, dict) or set(statement) != required:
        raise Refused('Complete normalized statement required')
    if (statement['account'], statement['ucc']) != (ledger.account, accounting.ucc):
        raise Refused('Statement account mismatch')
    try:
        day = date.fromisoformat(statement['trade_day'])
        if day.isoformat() != statement['trade_day'] or day >= datetime.now(IST).date():
            raise ValueError
        charges = statement['charges']
        if not isinstance(charges, dict) or set(charges) != set(CHARGES):
            raise ValueError
        charges = {k: bounded_amount(v) for k,v in charges.items()}
        if any(v.as_tuple().exponent < -2 for v in charges.values()):
            raise ValueError
        fills = statement['fills']
        if not isinstance(fills, list) or not 1 <= len(fills) <= 10000:
            raise ValueError
        canonical = json.dumps(statement, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        raise Refused('Closed trading day and valid statement amounts required') from None
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    proof_digest = hashlib.sha256(proof).hexdigest()
    review_id = hashlib.sha256((ledger.account+proof_digest+digest).encode()).hexdigest()
    install(db)
    with ledger.transaction(), localcontext() as context:
        context.prec = 80
        prior = db.execute('SELECT * FROM reviewed_statements WHERE review_id=?', (review_id,)).fetchone()
        if prior:
            return dict(review_id=review_id, duplicate=True, fee_basis='reviewed-statement',
                        order_submission_available=False)
        latest = db.execute('SELECT review_id FROM reviewed_statements WHERE trade_day=? ORDER BY reviewed_at DESC,rowid DESC LIMIT 1', (day.isoformat(),)).fetchone()
        if statement['supersedes'] != (latest[0] if latest else None):
            raise Refused('Explicit latest statement revision required')
        known = { (r['segment'],r['trade_id']):dict(r) for r in db.execute(
            'SELECT * FROM fill_history WHERE trade_day=?', (day.isoformat(),)) }
        if not known:
            raise Refused('Known broker fills required')
        seen, allocations = set(), []
        for value in fills:
            if not isinstance(value, dict) or set(value) != {'segment','trade_id','broker_id','side','quantity','price','total_fee'}:
                raise Refused('Exact statement fill fields required')
            key = (text_field(value['segment']),text_field(value['trade_id']))
            if key not in known or key in seen:
                raise Refused('Missing, duplicate or external statement fill')
            seen.add(key)
            fill = known[key]
            if (value['broker_id'] != fill['broker_id'] or value['side'] != fill['side'] or
                    type(value['quantity']) is not int or value['quantity'] != fill['quantity'] or
                    bounded_amount(value['price']) != bounded_amount(fill['price'])):
                raise Refused('Statement fill disagrees with broker evidence')
            if not db.execute('SELECT 1 FROM broker_fill_evidence WHERE segment=? AND trade_day=? AND trade_id=?',
                              (key[0],day.isoformat(),key[1])).fetchone():
                raise Refused('Original broker fill evidence required')
            fee = bounded_amount(value['total_fee'])
            if fee.as_tuple().exponent < -2:
                raise Refused('Statement fees must be allocated to paise')
            allocations.append((*key, fee))
        total = sum(charges.values(), Decimal(0))
        if seen != set(known) or sum((r[2] for r in allocations), Decimal(0)) != total:
            raise Refused('Complete fills and exactly allocated statement total required')
        at = datetime.now(timezone.utc)
        db.execute('INSERT INTO reviewed_statements VALUES(?,?,?,?,?,?,?,?,?)',
            (review_id,day.isoformat(),at.isoformat(),proof_digest,digest,str(total),statement['supersedes'],actor,canonical))
        for segment, trade_id, fee in allocations:
            accounting.correct_fee(account=ledger.account, ucc=accounting.ucc, source_ref=review_id,
                segment=segment, trade_day=day.isoformat(), trade_id=trade_id, total_fee=fee, reported_at=at)
            db.execute('INSERT INTO reviewed_statement_lines VALUES(?,?,?,?,?)',
                (review_id,segment,day.isoformat(),trade_id,str(fee)))
        ledger.pause()
        return dict(review_id=review_id, duplicate=False, total_fee=str(total),
                    fee_basis='reviewed-statement', order_submission_available=False)

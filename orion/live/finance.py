"""Own-account finance review helpers; never login, start workers or send orders."""
from contextlib import closing
from datetime import date, datetime
import sqlite3

from .accounting import Accounting, IST
from .cash import CashAllocation
from .controls import _existing, _path
from .ledger import Ledger, Refused
from .routing import reviewed_policy
from .statements import CHARGES, review as review_statement


def statement_template(store, uid, day):
    try:
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day or parsed >= datetime.now(IST).date():
            raise ValueError
    except (TypeError, ValueError):
        raise Refused('Closed statement day required') from None
    path = _path(store, uid)
    if not path.is_file():
        raise Refused('Existing Live fill history required')
    try:
        with closing(sqlite3.connect(path.absolute().as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('SELECT account FROM metadata').fetchone()[0] != uid:
                raise Refused('Statement account mismatch')
            ucc = db.execute('SELECT ucc FROM reconciliation_account').fetchone()[0]
            fills = [dict(r) | dict(total_fee='') for r in db.execute('''SELECT segment,trade_id,broker_id,side,quantity,price
                FROM fill_history WHERE trade_day=? ORDER BY executed_at,trade_id LIMIT 10001''', (day,))]
            if not 1 <= len(fills) <= 10000:
                raise Refused('Known bounded statement fills required')
            latest = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='reviewed_statements'").fetchone():
                row = db.execute('SELECT review_id FROM reviewed_statements WHERE trade_day=? ORDER BY reviewed_at DESC,rowid DESC LIMIT 1', (day,)).fetchone()
                latest = row[0] if row else None
            return dict(account=uid, ucc=ucc, trade_day=day, charges={k:'' for k in CHARGES}, fills=fills, supersedes=latest)
    except sqlite3.Error:
        raise Refused('Statement history unavailable') from None


def snapshot(store, uid):
    path = _path(store, uid)
    empty = dict(latest_cash_review=None, order_submission_available=False)
    if not path.is_file():
        return empty
    try:
        with closing(sqlite3.connect(path.absolute().as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('SELECT account FROM metadata').fetchone()[0] != uid:
                raise Refused('Finance account mismatch')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_cash_allocations'").fetchone():
                return empty
            has_model = 'model' in {r[1] for r in db.execute('PRAGMA table_info(daily_cash_allocations)')}
            row = db.execute('SELECT day,reviewed_at,opening_cash,' + ('model' if has_model else "'opening-only' AS model") + ' FROM daily_cash_allocations ORDER BY day DESC LIMIT 1').fetchone()
            return empty | dict(latest_cash_review=dict(row) if row else None)
    except sqlite3.Error:
        raise Refused('Finance snapshot unavailable') from None


def review_cash(store, uid, *, opening_cash, confirmation):
    with _existing(store, uid) as ledger:
        row = ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
        if not row:
            raise Refused('Bound broker account required')
        policy = reviewed_policy(store, uid, row[0])
        accounting = Accounting(ledger, row[0])
        return CashAllocation(accounting).review(actor=uid, policy=policy,
            opening_cash=opening_cash, confirmation=confirmation)


def review_fees(store, uid, *, proof, statement, confirmation):
    # Historical bookkeeping needs no new trading consent, active broker login
    # or worker. The same account lock serializes it with a running monitor.
    with store.lock(uid):
        path = _path(store, uid)
        if not path.is_file():
            raise Refused('Existing Live fill history required')
        with closing(Ledger(path, uid)) as ledger:
            row = ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
            if not row:
                raise Refused('Bound broker account required')
            return review_statement(Accounting(ledger, row[0]), actor=uid, proof=proof,
                                    statement=statement, confirmation=confirmation)

"""Durable manual requests consumed by the existing account worker.

No portal endpoint or service is enabled here. The caller must hold the account
lock. A request never calls the broker, and an acknowledgement never closes a
position. Exits use an explicitly confirmed limit, never an inferred market order.
"""
from datetime import datetime, timedelta, timezone
import json
import re

from .accounting import timestamp, bounded_amount
from .ledger import Refused, amount

TERMINAL = ('FILLED', 'CANCELLED', 'REJECTED', 'ABANDONED')
EXIT_REVIEW_CODES = {'strategy-requires-review', 'monitor-requires-review', 'unprotected-exposure'}


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS manual_requests(
        request_id TEXT PRIMARY KEY, entry_tag TEXT NOT NULL, action TEXT NOT NULL,
        limit_price TEXT, max_quantity INTEGER NOT NULL, expires_at TEXT NOT NULL,
        state TEXT NOT NULL, exit_tag TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS manual_cancellations(
        tag TEXT PRIMARY KEY, entry_tag TEXT NOT NULL)''')


def request(monitor, *, request_id, entry_tag, action, limit_price=None, now=None):
    """Account-authenticated caller submits an idempotent confirmed instruction.

    EXIT covers the currently confirmed remaining position, so a still-working
    entry must first be cancelled. Limit authorization expires after 60 seconds.
    CANCEL never cancels protective sells. PAUSE is represented by ledger.pause().
    """
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise Refused('Unique manual request identity required')
    if action not in ('CANCEL', 'EXIT'):
        raise Refused('Unsupported manual action')
    ledger, db = monitor.ledger, monitor.db
    now = timestamp(now or datetime.now(timezone.utc))
    install(db)
    with ledger.transaction():
        old = db.execute('SELECT * FROM manual_requests WHERE request_id=?', (request_id,)).fetchone()
        price = str(bounded_amount(limit_price)) if limit_price is not None else None
        if old:
            if (old['entry_tag'], old['action'], old['limit_price']) != (entry_tag, action, price):
                raise Refused('Manual request identity reused with different instructions')
            return dict(old)
        entry = ledger.get(entry_tag)
        if db.execute("SELECT 1 FROM manual_requests WHERE entry_tag=? AND state='PENDING'", (entry_tag,)).fetchone():
            raise Refused('Manual request already pending')
        quantity = 0
        if action == 'CANCEL':
            if price is not None or entry['status'] not in ('PREPARED', 'OPEN', 'PARTIAL'):
                raise Refused('Cancellable entry required')
        else:
            if entry['status'] not in ('FILLED', 'CANCELLED') or not entry['filled']:
                raise Refused('Cancel the working entry before exiting confirmed fills')
            quantity = monitor.protection._remaining(entry_tag)
            terms = db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry_tag,)).fetchone()
            if not terms or not quantity or quantity % terms['lot_size']:
                raise Refused('Confirmed whole-lot position required')
            if price is None or not amount(price) or amount(price) % amount(terms['tick']):
                raise Refused('Explicit tick-aligned exit limit required')
            from .kotak import OrderRequest
            body = json.loads(entry['body'])
            if body['segment'] == 'mcx_fo':
                from .entry_plans import bound_quantity_multiplier
                bound_quantity_multiplier(ledger, entry_tag)
            OrderRequest(tag=entry_tag, symbol=body['symbol'], segment=body['segment'], quantity=quantity,
                lot_size=terms['lot_size'], tick=terms['tick'], kind='EXIT', price=price).parameters()
        db.execute('INSERT INTO manual_requests VALUES(?,?,?,?,?,?,?,NULL)',
            (request_id, entry_tag, action, price, quantity, (now+timedelta(seconds=60)).isoformat(), 'PENDING'))
        from .authorization import revoke
        revoke(ledger)
        return dict(db.execute('SELECT * FROM manual_requests WHERE request_id=?', (request_id,)).fetchone())


def process(monitor, *, marks):
    """After validated book ingestion, perform at most one manual broker action.

    None lets normal protection proceed. A handled EXIT suppresses automatic
    target/trailing actions. Unknown outcomes remain latched by Commands.
    """
    db, ledger = monitor.db, monitor.ledger
    rows = db.execute("SELECT * FROM manual_requests WHERE state='PENDING' ORDER BY rowid").fetchall()
    if not rows:
        return None
    ledger.pause()
    if db.execute("SELECT 1 FROM broker_commands WHERE status IN ('SENDING','UNKNOWN')").fetchone():
        return monitor._health('review-required', 'manual-command-unknown')
    for item in rows:
        entry = ledger.get(item['entry_tag'])
        if item['action'] == 'CANCEL':
            if entry['status'] == 'PREPARED':
                ledger.abandon_prepared(entry['tag'])
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='live_signals'").fetchone():
                    db.execute("UPDATE live_signals SET state='INVALIDATED' WHERE source=?", (entry['event'],))
            elif entry['status'] in ('OPEN', 'PARTIAL'):
                prior = db.execute("SELECT 1 FROM broker_commands WHERE kind='ENTRY' AND tag=? AND operation='cancel'", (entry['tag'],)).fetchone()
                if not prior:
                    monitor.commands.cancel('ENTRY', entry['tag'])
                    return monitor._health('awaiting-broker', 'manual-cancellation-pending')
                # While awaiting cancellation, the base monitor protects fills.
                continue
            elif entry['status'] not in TERMINAL:
                return monitor._health('review-required', 'manual-entry-unresolved')
            db.execute("UPDATE manual_requests SET state='DONE' WHERE request_id=?", (item['request_id'],))
            continue

        remaining = monitor.protection._remaining(entry['tag'])
        exits = db.execute('SELECT * FROM protective_exits WHERE entry_tag=?', (entry['tag'],)).fetchall()
        active = [r for r in exits if r['status'] not in TERMINAL]
        if not remaining and not active:
            db.execute("UPDATE manual_requests SET state='DONE' WHERE request_id=?", (item['request_id'],))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='strategy_plans'").fetchone():
                db.execute("UPDATE strategy_plans SET phase='DONE',active_exit=NULL WHERE entry_tag=?", (entry['tag'],))
            continue
        if remaining > item['max_quantity'] or any(r['status'] not in ('OPEN','PARTIAL') for r in active):
            return monitor._health('review-required', 'manual-exit-unresolved')
        now = datetime.now(timezone.utc)
        if item['exit_tag']:
            current = monitor.protection.get(item['exit_tag'])
            if current['status'] in ('OPEN', 'PARTIAL'):
                if now >= datetime.fromisoformat(item['expires_at']):
                    return _cancel(monitor, current, 'EXIT')
                return monitor._health('awaiting-broker', 'manual-exit-pending')
            # Terminal partial/no fill: do not silently retry. Restore protection.
            return _halt(monitor, item, 'manual-exit-ended')
        if now >= datetime.fromisoformat(item['expires_at']):
            return _halt(monitor, item, 'manual-exit-expired')
        quote = marks.get(entry['tag'])
        fresh = False
        if isinstance(quote, tuple) and len(quote) == 2:
            fresh = 0 <= (now-timestamp(quote[1])).total_seconds() <= 5
            fresh = fresh and amount(quote[0]) >= amount(item['limit_price'])
        if not fresh:
            # Before cancellation this leaves existing stops in place; after a
            # cancellation it hands exposure back to normal protection.
            return _halt(monitor, item, 'manual-exit-price-unavailable')
        if any(r[0] not in EXIT_REVIEW_CODES for r in db.execute('SELECT code FROM live_incidents')):
            return monitor._health('review-required', 'manual-exit-incident')
        for row in active:
            kind = 'STOP' if amount(row['trigger']) > 0 else 'EXIT'
            return _cancel(monitor, row, kind)
        terms = db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry['tag'],)).fetchone()
        with ledger.transaction():
            row = monitor.protection.prepare_limit(entry['tag'], quantity=remaining,
                limit_price=item['limit_price'], tick_size=terms['tick'], lot_size=terms['lot_size'],
                manual_request_id=item['request_id'])
            monitor.protection.dispatch(row['tag'])
            db.execute('UPDATE manual_requests SET exit_tag=? WHERE request_id=?', (row['tag'], item['request_id']))
        monitor.commands.place('EXIT', row['tag'])
        return monitor._health('awaiting-broker', 'manual-exit-pending')
    return None


def authorize_exit(db, *, entry_tag, quantity, limit_price, request_id=None, exit_tag=None):
    """Only an exact, unexpired explicit manual exit can bypass known review codes.

    Uncertain broker commands, identity conflicts and unknown incidents never
    qualify. This grants no entry authority and does not clear any incident.
    """
    if (request_id is None) == (exit_tag is None):
        raise Refused('Exact manual exit binding required')
    key, value = ('request_id', request_id) if request_id is not None else ('exit_tag', exit_tag)
    row = db.execute(f'SELECT * FROM manual_requests WHERE {key}=?', (value,)).fetchone()
    if (not row or row['action'] != 'EXIT' or row['state'] != 'PENDING' or
            row['entry_tag'] != entry_tag or quantity > row['max_quantity'] or
            amount(limit_price) != amount(row['limit_price']) or
            datetime.now(timezone.utc) >= datetime.fromisoformat(row['expires_at'])):
        raise Refused('Manual exit authorization expired or changed')
    if any(r[0] not in EXIT_REVIEW_CODES for r in db.execute('SELECT code FROM live_incidents')):
        raise Refused('Manual exit blocked by unresolved broker evidence')


def _cancel(monitor, row, kind):
    if monitor.db.execute("SELECT 1 FROM broker_commands WHERE kind=? AND tag=? AND operation='cancel'", (kind, row['tag'])).fetchone():
        return monitor._health('awaiting-broker', 'manual-cancellation-pending')
    monitor.db.execute('INSERT OR IGNORE INTO manual_cancellations VALUES(?,?)', (row['tag'], row['entry_tag']))
    monitor.commands.cancel(kind, row['tag'])
    return monitor._health('awaiting-broker', 'manual-cancellation-pending')


def _halt(monitor, item, reason):
    with monitor.ledger.transaction():
        monitor.db.execute("UPDATE manual_requests SET state='HALTED' WHERE request_id=?", (item['request_id'],))
        if monitor.db.execute("SELECT 1 FROM sqlite_master WHERE name='strategy_plans'").fetchone():
            monitor.db.execute("""UPDATE strategy_plans SET phase='HALTED',
                active_exit=CASE WHEN active_exit IN
                (SELECT tag FROM protective_exits WHERE status IN ('OPEN','PARTIAL'))
                THEN active_exit ELSE NULL END WHERE entry_tag=?""", (item['entry_tag'],))
    # Resume protection on the following cycle, never another manual sell.
    return monitor._health('review-required', reason)

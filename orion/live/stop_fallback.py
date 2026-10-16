"""Opt-in, one-shot 20 -> 50 tick stop repricing; never creates a new sell.

Timers start at the first reconciled broker observation of a triggered stop,
not at placement or a quote crossing. No exchange trigger timestamp is exposed
by the supported book schema. Monitoring latency is therefore not bounded here.
"""
from datetime import datetime, timedelta, timezone
import json

from .ledger import Refused, amount
from .kotak import OrderRequest


def install(db):
    db.execute('CREATE TABLE IF NOT EXISTS stop_fallback_policy(entry_tag TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS stop_fallback_state('
               'tag TEXT PRIMARY KEY, first_triggered TEXT NOT NULL, deadline TEXT NOT NULL)')


def bind(ledger, entry_tag):
    """Only called with an explicitly reviewed opt-in, before an entry dispatch."""
    entry = ledger.get(entry_tag)
    if entry['status'] != 'PREPARED' or entry['broker_id'] or entry['filled']:
        raise Refused('Stop fallback must be bound before entry dispatch')
    ledger.db.execute('INSERT OR IGNORE INTO stop_fallback_policy VALUES(?)', (entry_tag,))


def authorize(ledger, tag):
    db = ledger.db
    row = db.execute('SELECT * FROM protective_exits WHERE tag=?', (tag,)).fetchone()
    state = db.execute('SELECT * FROM stop_fallback_state WHERE tag=?', (tag,)).fetchone()
    observed = db.execute('SELECT * FROM stop_observations WHERE tag=?', (tag,)).fetchone()
    now = datetime.now(timezone.utc)
    if not row or not state or not observed or not db.execute(
            'SELECT 1 FROM stop_fallback_policy WHERE entry_tag=?', (row['entry_tag'],)).fetchone():
        raise Refused('Reviewed stop fallback required')
    if (row['status'] not in ('OPEN', 'PARTIAL') or not row['broker_id'] or
            observed['state'] != 'triggered' or
            not 0 <= (now-datetime.fromisoformat(observed['observed_at'])).total_seconds() <= 5 or
            not 5 <= (now-datetime.fromisoformat(state['first_triggered'])).total_seconds() < 30 or
            now >= datetime.fromisoformat(state['deadline'])):
        raise Refused('Fresh triggered stop within recovery window required')
    if db.execute('SELECT 1 FROM live_incidents').fetchone() or db.execute(
            "SELECT 1 FROM broker_commands WHERE status!='CONFIRMED'").fetchone():
        raise Refused('Resolve outstanding commands before widening')
    entry = ledger.get(row['entry_tag'])
    if entry['status'] not in ('FILLED', 'CANCELLED'):
        raise Refused('Final entry fills required')
    body = json.loads(entry['body'])
    from .entry_plans import bound_quantity_multiplier
    bound_quantity_multiplier(ledger, row['entry_tag'])
    terms = db.execute('SELECT * FROM execution_terms WHERE tag=?', (row['entry_tag'],)).fetchone()
    old = db.execute("SELECT request FROM broker_commands WHERE kind='STOP' AND tag=? AND operation='place'", (tag,)).fetchone()
    if not terms or not old:
        raise Refused('Bound stop terms required')
    trigger, tick = amount(row['trigger']), amount(terms['tick'])
    if trigger-amount(json.loads(old[0])['request']['price']) != 20*tick:
        raise Refused('Fallback requires an initial 20-tick gap')
    remaining = row['quantity']-row['filled']
    if remaining <= 0 or remaining % terms['lot_size']:
        raise Refused('Whole-lot residual exposure required')
    # Kotak modify quantity is TOTAL order quantity, not remaining quantity.
    # Keeping that total and the same ID prevents adding back already filled units.
    request = OrderRequest(tag=tag, symbol=body['symbol'], segment=body['segment'],
        quantity=row['quantity'], lot_size=terms['lot_size'], tick=terms['tick'],
        kind='STOP', trigger=row['trigger'], price=str(trigger-50*tick))
    request.parameters()
    return request, state['deadline']


def cycle(manager, entry, stops, now, *, observe_only=False):
    db = manager.db
    if not db.execute('SELECT 1 FROM stop_fallback_policy WHERE entry_tag=?', (entry['tag'],)).fetchone():
        return None
    recovering = []
    for stop in stops:
        if stop['status'] not in ('OPEN', 'PARTIAL'):
            continue
        observed = db.execute('SELECT * FROM stop_observations WHERE tag=?', (stop['tag'],)).fetchone()
        state = db.execute('SELECT * FROM stop_fallback_state WHERE tag=?', (stop['tag'],)).fetchone()
        if not state and observed and observed['state'] == 'triggered':
            first = datetime.fromisoformat(observed['observed_at'])
            with manager.ledger.transaction():
                db.execute('INSERT OR IGNORE INTO stop_fallback_state VALUES(?,?,?)',
                    (stop['tag'], first.isoformat(), (first+timedelta(seconds=30)).isoformat()))
            state = db.execute('SELECT * FROM stop_fallback_state WHERE tag=?', (stop['tag'],)).fetchone()
        if not state:
            continue
        manager.ledger.pause()
        age = (now-datetime.fromisoformat(state['first_triggered'])).total_seconds()
        if age < 0 or now >= datetime.fromisoformat(state['deadline']):
            manager.protection.incident('strategy-requires-review')
            return manager._health('review-required', 'stop-recovery-deadline')
        recovering.append((stop, age))
    if observe_only:
        return None
    action = None
    for stop, age in recovering:
        prior = db.execute("SELECT status FROM broker_commands WHERE kind='STOP' AND tag=? AND operation='modify'", (stop['tag'],)).fetchone()
        if prior:
            action = 'stop-widening-observed' if prior[0] == 'CONFIRMED' else 'stop-widening-pending'
            continue
        if age < 5:
            action = action or 'stop-recovery-wait'
            continue
        try:
            authorize(manager.ledger, stop['tag'])
        except Refused:
            return manager._health('review-required', 'stop-widening-blocked')
        manager.commands.widen_stop(stop['tag'])
        return manager._health('awaiting-broker', 'stop-widening-requested')
    return manager._health('awaiting-broker', action) if action else None

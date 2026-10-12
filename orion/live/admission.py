"""Additional admission requires every existing position to have observed stops."""
import json

from .ledger import Refused, amount


def protected_book(ledger):
    db = ledger.db
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'broker_commands' in tables and db.execute("SELECT 1 FROM broker_commands WHERE status!='CONFIRMED'").fetchone():
        raise Refused('Pending commands block additional entries')
    if 'manual_requests' in tables and db.execute("SELECT 1 FROM manual_requests WHERE state='PENDING'").fetchone():
        raise Refused('Manual action blocks additional entries')
    for entry in db.execute('SELECT * FROM intents').fetchall():
        if entry['status'] == 'PREPARED' and not entry['filled'] and not entry['broker_id']:
            continue
        if entry['status'] not in ('FILLED', 'CANCELLED', 'REJECTED', 'ABANDONED'):
            raise Refused('Finalize working entries before adding a position')
        exits = db.execute('SELECT * FROM protective_exits WHERE entry_tag=?', (entry['tag'],)).fetchall()
        remaining = entry['filled'] - sum(row['filled'] for row in exits)
        if remaining < 0:
            raise Refused('Exit quantity exceeds exposure')
        working = [r for r in exits if r['status'] not in ('FILLED', 'CANCELLED', 'REJECTED')]
        if not remaining:
            if working:
                raise Refused('Working exit without exposure')
            continue
        if not {'strategy_plans', 'broker_commands', 'manual_requests'} <= tables:
            raise Refused('Existing position needs an exit plan')
        plan = db.execute('SELECT * FROM strategy_plans WHERE entry_tag=?', (entry['tag'],)).fetchone()
        terms = db.execute('SELECT * FROM execution_terms WHERE tag=?', (entry['tag'],)).fetchone()
        if not plan or not terms or plan['phase'] != 'WATCH' or plan['active_exit'] or not 0 <= plan['stage'] < 3:
            raise Refused('Position exit transition blocks additional entries')
        stop = (amount(terms['stop']) if plan['stage'] == 0 else
                amount(json.loads(entry['body'])['limit_price']) if plan['stage'] == 1 else
                amount(json.loads(plan['targets'])[0]))
        if (not working or any(r['status'] not in ('OPEN', 'PARTIAL') or not r['broker_id'] or
                               amount(r['trigger']) != stop for r in working) or
                sum(r['quantity']-r['filled'] for r in working) != remaining):
            raise Refused('Exact observed stop coverage required for every position')

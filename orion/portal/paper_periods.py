"""Paper testing periods preserve the execution ledger and signal deduplication."""
import json
from uuid import uuid4
from datetime import datetime, timezone
from ..core import dec, dumps


def metadata(state):
    return state.get('paper_period', {'id': 'legacy', 'label': 'Before first reset',
                                     'start_seq': 0, 'started_at': None})


def view(state):
    """Reporting copy only; the engine retains closed positions as signal guards."""
    excluded = set(state.get('paper_period', {}).get('excluded_signals', []))
    return {**state, 'positions': {sid: p for sid, p in state.get('positions', {}).items()
                                   if sid not in excluded}}


def archives(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_periods'").fetchone():
        return []
    return [dict(id=r[0], label=r[1], ended_at=r[2]) for r in db.execute(
        'SELECT id,label,ended_at FROM paper_periods ORDER BY end_seq DESC')]


def select(db, state, period_id):
    current = metadata(state)
    options = [{k: current[k] for k in ('id', 'label', 'started_at')}] + archives(db)
    end_seq = 9223372036854775807
    archived = period_id not in ('current', current['id'])
    if archived:
        if period_id not in {p['id'] for p in options[1:]}:
            raise ValueError('Unknown paper testing period')
        row = db.execute('SELECT state,end_seq,ended_at FROM paper_periods WHERE id=?', (period_id,)).fetchone()
        state, end_seq, ended_at = json.loads(row[0]), row[1], row[2]
        current = {**metadata(state), 'ended_at': ended_at}
    records = [dict(seq=r[0], at=r[1], event=r[2], body=json.loads(r[3])) for r in db.execute(
        "SELECT seq,at,event,body FROM audit WHERE seq>? AND seq<=? "
        "AND event IN ('PAPER_BUY','PAPER_EXIT','TARGET_REACHED') ORDER BY seq",
        (current['start_seq'], end_seq))]
    return view(state), records, {k: v for k, v in current.items() if k != 'excluded_signals'}, options, archived


def start(db, state, amount, label, expected_period, settings):
    amount = dec(amount)
    if not amount.is_finite() or not 0 < amount <= 100000000 or amount != amount.quantize(dec('0.01')):
        raise ValueError('Enter starting cash greater than zero, up to 100,000,000, with at most two decimals')
    label = label.strip()
    if not label or len(label) > 200:
        raise ValueError('Enter a period label of 1–200 characters')
    prior = metadata(state)
    if expected_period != prior['id']:
        raise ValueError('The paper period has changed. Refresh before starting another period.')
    if any(p['status'] in ('PENDING', 'OPEN') or p.get('remaining', 0) for p in state['positions'].values()):
        raise ValueError('Finish pending and open paper trades before starting a new period')
    db.execute('CREATE TABLE IF NOT EXISTS paper_periods '
               '(id TEXT PRIMARY KEY,label TEXT NOT NULL,ended_at TEXT NOT NULL,'
               'end_seq INTEGER NOT NULL,state TEXT NOT NULL,settings TEXT NOT NULL)')
    now = datetime.now(timezone.utc).isoformat()
    end_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM audit').fetchone()[0]
    db.execute('INSERT INTO paper_periods VALUES(?,?,?,?,?,?)',
               (prior['id'], prior['label'], now, end_seq, dumps(state), dumps(settings)))
    period_id = uuid4().hex
    body = dict(previous_period=prior['id'], period_id=period_id, label=label,
                before=state['cash'], starting_cash=str(amount))
    cursor = db.execute('INSERT INTO audit(at,event,body) VALUES(?,?,?)',
                        (now, 'PAPER_PERIOD_STARTED', dumps(body)))
    state['paper_period'] = dict(id=period_id, label=label, started_at=now,
                                start_seq=cursor.lastrowid, starting_cash=str(amount),
                                excluded_signals=list(state['positions']))
    state['cash'] = str(amount)
    state['days'] = {}
    db.execute('UPDATE state SET body=? WHERE id=1', (dumps(state),))
    return {k: v for k, v in state['paper_period'].items() if k != 'excluded_signals'}

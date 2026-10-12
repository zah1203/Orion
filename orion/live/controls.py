"""Authenticated portal helpers for the existing Live worker's request queue.

No broker transport, worker startup, credentials, BUY permission or ledger creation.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import uuid
from types import SimpleNamespace

from .ledger import Ledger, Refused
from .manual import request
from .protection import Protection


def _path(store, uid):
    store.user(uid)
    path = store.account_dir(uid)/'live.db'
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Refused('Live control path requires review')
    if any((p/'RECOVERY_ONLY').exists() for p in path.parents):
        raise Refused('Recovered ledgers cannot control trading')
    return path


def _worker(db):
    try:
        row = db.execute('SELECT * FROM live_worker_health WHERE id=1').fetchone()
        return bool(row and not row['stopped'] and 0 <= (
            datetime.now(timezone.utc)-datetime.fromisoformat(row['checked_at'])).total_seconds() <= 10)
    except (sqlite3.Error, TypeError, ValueError):
        return False


def snapshot(store, uid):
    path = _path(store, uid)
    empty = dict(worker_available=False, entries_paused=True, positions=[], requests=[])
    if not path.is_file():
        return empty
    try:
        with closing(sqlite3.connect(path.absolute().as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('SELECT account FROM metadata').fetchone()[0] != uid:
                raise Refused('Live account mismatch')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'manual_requests','protective_exits','execution_terms'} <= tables:
                return empty
            active = _worker(db)
            positions = []
            pending = {r[0] for r in db.execute("SELECT entry_tag FROM manual_requests WHERE state='PENDING'")}
            for row in db.execute('SELECT * FROM intents'):
                body = json.loads(row['body'])
                sold = db.execute('SELECT COALESCE(SUM(filled),0) FROM protective_exits WHERE entry_tag=?', (row['tag'],)).fetchone()[0]
                remaining = row['filled']-sold
                cancel = row['status'] in ('PREPARED','OPEN','PARTIAL')
                if not remaining and not cancel:
                    continue
                terms = db.execute('SELECT tick,lot_size FROM execution_terms WHERE tag=?', (row['tag'],)).fetchone()
                positions.append(dict(tag=row['tag'], request_id=uuid.uuid4().hex, symbol=body['symbol'], status=row['status'],
                    remaining=remaining, entry_quantity=body['quantity'],
                    tick=terms['tick'] if terms else None,
                    can_cancel=active and cancel and row['tag'] not in pending,
                    can_exit=bool(active and terms and remaining > 0 and remaining % terms['lot_size'] == 0
                        and row['status'] in ('FILLED','CANCELLED') and row['tag'] not in pending)))
            requests = [dict(r) for r in db.execute('''SELECT request_id,entry_tag,action,state
                FROM manual_requests ORDER BY rowid DESC LIMIT 30''')]
            return dict(worker_available=active, positions=positions, requests=requests,
                entries_paused=bool(db.execute('SELECT paused FROM control WHERE id=1').fetchone()[0]))
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        raise Refused('Live control snapshot requires review') from None


@contextmanager
def _existing(store, uid):
    with store.lock(uid):
        path = _path(store, uid)
        state = snapshot(store, uid)
        if not path.is_file() or not state['worker_available']:
            raise Refused('Active Live worker required; no worker was started')
        with closing(Ledger(path, uid)) as ledger:
            yield ledger


def enqueue(store, uid, **instruction):
    with _existing(store, uid) as ledger:
        monitor = SimpleNamespace(ledger=ledger, db=ledger.db, protection=Protection(ledger))
        return request(monitor, **instruction)


def pause(store, uid):
    with _existing(store, uid) as ledger:
        ledger.pause()
        return dict(entries_paused=True)

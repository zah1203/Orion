"""One candidate check per received signal; orders require explicit permission.

Shadow checks are read-only broker operations and are not acceptance evidence by
themselves. Existing exposure always takes priority over candidate collection.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3

from . import status
from .authorization import revoke
from .coordinator import EntryCoordinator
from .controls import _path
from .ledger import Refused
from .monitor import ProtectionMonitor


class EntryWorker:
    def __init__(self, account):
        self.account = account
        self.coordinator = EntryCoordinator(account)
        self.db = account.ledger.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS live_entry_checks(
            source TEXT PRIMARY KEY, checked_at TEXT NOT NULL, purpose TEXT NOT NULL,
            state TEXT NOT NULL, stage TEXT NOT NULL, proposal TEXT)''')

    def cycle(self):
        a, c = self.account, self.coordinator
        now = datetime.now(timezone.utc)
        with a.store.lock(a.uid):
            health = a.monitor.health()
            if health['state'] == 'review-required':
                revoke(a.ledger)
            if (health['state'] != 'observed' or health.get('uncovered', 0) != 0 or
                    (health['exposure'] and not getattr(a.monitor, 'portfolio', False))):
                return 'monitoring-first'
            if self.db.execute("SELECT 1 FROM intents WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','ABANDONED')").fetchone():
                return 'monitoring-first'
            try:
                a.ledger._check_incidents(allow_protected=True)
            except Refused:
                revoke(a.ledger)
                return 'review-required'
            try:
                c.authority.resume_when_protected()
            except Refused:
                pass
            source = self.db.execute('''SELECT s.source,s.signal_time FROM live_signals s
                WHERE s.state='PENDING' AND NOT EXISTS(SELECT 1 FROM live_entry_checks e WHERE e.source=s.source)
                ORDER BY s.signal_time DESC LIMIT 1''').fetchone()
            if not source:
                return 'idle'
            if not 0 <= (now-datetime.fromisoformat(source['signal_time'])).total_seconds() <= 30:
                self.db.execute("UPDATE live_signals SET state='INVALIDATED' WHERE source=?",(source['source'],))
                return 'expired'
            purpose = 'shadow'
            try:
                c.authority.require()
                purpose = 'entry'
            except Refused:
                pass
            # Returning from a completed exit plan must restore partial-fill
            # protection for the next session-authorized entry.
            if a.strategy and not getattr(a.monitor, 'portfolio', False):
                a.monitor = ProtectionMonitor(a.ledger,a.session,collect_trades=True)
                a.strategy = False
            self.db.execute('INSERT INTO live_entry_checks VALUES(?,?,?,\'CHECKING\',\'starting\',NULL)',
                            (source['source'],now.isoformat(),purpose))
        try:
            report = c.submit(source['source']) if purpose == 'entry' else c.evaluate(source['source'])
            proposal = None
            if purpose == 'shadow':
                candidate = report['candidate']
                proposal = json.dumps({k:candidate[k] for k in ('symbol','quantity','limit_price')})
            state = 'ACKNOWLEDGED' if purpose == 'entry' else 'CHECKED'
            stage = 'entry-acknowledgement' if purpose == 'entry' else 'read-only-check-complete'
        except Exception:
            # Never store SDK text, tokens, source messages or raw response bodies.
            state, stage, proposal = 'REFUSED', getattr(c,'last_stage','entry-authorization'), None
            if purpose == 'entry':
                with a.store.lock(a.uid):
                    revoke(a.ledger)
        with a.store.lock(a.uid):
            self.db.execute('UPDATE live_entry_checks SET checked_at=?,state=?,stage=?,proposal=? WHERE source=?',
                (datetime.now(timezone.utc).isoformat(),state,stage,proposal,source['source']))
        return state.lower()


def snapshot(store, uid):
    path = _path(store,uid)
    result = dict(release_available=status.release_status()['order_submission_available'],
                  permission='not-authorized', day=None, checks=[], order_submission_available=False)
    if not path.is_file():
        return result
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            if db.execute('SELECT account FROM metadata').fetchone()[0] != uid:
                raise Refused('Entry status account mismatch')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'entry_permission' in tables:
                has_scope = 'scope' in {r[1] for r in db.execute('PRAGMA table_info(entry_permission)')}
                row = db.execute("SELECT active,day," + ("scope" if has_scope else "'one-entry' AS scope") + " FROM entry_permission WHERE id=1").fetchone()
                if row:
                    result.update(permission=('session-reviewed' if row['active'] == 1 and row['scope'] == 'portfolio-session' else 'not-authorized'),day=row['day'])
            if 'live_entry_checks' in tables:
                result['checks'] = [dict(r) for r in db.execute('''SELECT checked_at,purpose,state,stage,proposal
                    FROM live_entry_checks ORDER BY checked_at DESC LIMIT 10''')]
            return result
    except (sqlite3.Error, ValueError, TypeError, IndexError):
        raise Refused('Entry status requires review') from None

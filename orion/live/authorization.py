"""Account-owned, India-day entry permission. Never overrides release acceptance."""
from datetime import datetime, timezone
import json
import uuid

from . import status
from .accounting import Accounting, IST
from .cash import CashAllocation, digest
from .ledger import Refused
from .reconciliation import fingerprint
from .routing import reviewed_policy

CONFIRMATION = "AUTHORIZE ONE LIVE ENTRY TODAY WITH MY REVIEWED LIMITS"


def install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS entry_permission(
            id INTEGER PRIMARY KEY CHECK(id=1), nonce TEXT NOT NULL,
            worker_session TEXT NOT NULL, day TEXT NOT NULL, policy_digest TEXT NOT NULL,
            active INTEGER NOT NULL, reviewed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entry_dispatch_permission(
            tag TEXT PRIMARY KEY, nonce TEXT NOT NULL, committed_at TEXT NOT NULL);
    ''')


def revoke(ledger):
    ledger.pause()
    if ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='entry_permission'").fetchone():
        ledger.db.execute('UPDATE entry_permission SET active=0 WHERE id=1')


def review(ledger, store, uid, *, policy_version, confirmation):
    """Caller holds account lock; consume a fresh worker snapshot, never log in."""
    if status.release_status()['order_submission_available'] is not True:
        raise Refused('Real-money release acceptance remains incomplete')
    if uid != ledger.account or confirmation != CONFIRMATION:
        raise Refused('Own-account real-money confirmation required')
    now = datetime.now(timezone.utc)
    ucc = ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()[0]
    accounting = Accounting(ledger, ucc)
    cash = CashAllocation(accounting)
    with ledger.transaction():
        policy = reviewed_policy(store, uid, ucc)
        if type(policy_version) is not int or policy['version'] != policy_version:
            raise Refused('Review current pilot limits before authorizing entries')
        checked = ledger.db.execute('SELECT * FROM reconciliation_state WHERE id=1').fetchone()
        if (not checked or checked['digest'] != fingerprint(ledger) or
                not 0 <= (now-datetime.fromisoformat(checked['checked_at'])).total_seconds() <= 10):
            raise Refused('Fresh unchanged worker reconciliation required')
        if ledger.db.execute('SELECT 1 FROM live_incidents').fetchone():
            raise Refused('Incident review required')
        slot = None
        with store.db() as db:
            slot = db.execute('SELECT reserved,stop_requested FROM live_worker_slots WHERE uid=?', (uid,)).fetchone()
        if not slot or not slot['reserved'] or slot['stop_requested']:
            raise Refused('Active monitoring slot required')
        source = ledger.db.execute('SELECT state FROM live_source_health WHERE id=1').fetchone()
        if not source or source[0] != 'connected':
            raise Refused('Connected account signal source required')
        if ledger.db.execute("SELECT 1 FROM broker_commands WHERE status!='CONFIRMED'").fetchone():
            raise Refused('Outstanding broker commands require review')
        for table in ('intents', 'protective_exits'):
            if ledger.db.execute(f"SELECT 1 FROM {table} WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','ABANDONED')").fetchone():
                raise Refused('Authorization requires a flat, settled order book')
        if ledger.db.execute("SELECT 1 FROM manual_requests WHERE state='PENDING'").fetchone():
            raise Refused('Pending manual action requires review')
        if accounting.estimate_budget(now=now, marks={}, fee_reserve=policy['limits']['fee_reserve'])['open_quantities']:
            raise Refused('Authorization requires a flat account')
        cash.balance(policy=policy, now=now)
        ledger.db.execute('INSERT OR REPLACE INTO entry_permission VALUES(1,?,?,?,?,1,?)',
            (uuid.uuid4().hex,checked['session'],now.astimezone(IST).date().isoformat(),digest(policy),now.isoformat()))
        ledger.db.execute('UPDATE control SET paused=0 WHERE id=1')
        return dict(authorized=True, day=now.astimezone(IST).date().isoformat())


def authorize(store, uid, **instruction):
    # Check the release first: a missing ledger never turns into an initializer.
    if status.release_status()['order_submission_available'] is not True:
        raise Refused('Real-money release acceptance remains incomplete')
    from .controls import _existing
    with _existing(store, uid) as ledger:
        return review(ledger, store, uid, **instruction)


class EntryAuthority:
    def __init__(self, account):
        self.account, self.ledger = account, account.ledger
        install(self.ledger.db)

    def require(self, *, consumed=False):
        """Called while the account lock is held; permission is not a risk check."""
        a, ledger = self.account, self.ledger
        if (status.release_status()['order_submission_available'] is not True or
                a.ledger is not ledger or a.lease is None or a.lease.closed):
            raise Refused('Leased real-money release permission required')
        row = ledger.db.execute('SELECT * FROM entry_permission WHERE id=1').fetchone()
        policy = reviewed_policy(a.store, a.uid, a.session.ucc)
        now = datetime.now(timezone.utc)
        if (not row or row['active'] != (2 if consumed else 1) or row['worker_session'] != ledger.session or
                row['day'] != now.astimezone(IST).date().isoformat() or
                row['policy_digest'] != digest(policy) or datetime.fromisoformat(row['reviewed_at']) > now or
                ledger.db.execute('SELECT paused FROM control WHERE id=1').fetchone()[0]):
            raise Refused('Current account-owned daily entry permission required')
        with a.store.db() as db:
            slot = db.execute('SELECT reserved,stop_requested FROM live_worker_slots WHERE uid=?',(a.uid,)).fetchone()
        if not slot or not slot['reserved'] or slot['stop_requested']:
            raise Refused('Monitoring stop or handover blocks entries')
        return row

    def allowed(self):
        try:
            row = self.ledger.db.execute('SELECT active FROM entry_permission WHERE id=1').fetchone()
            consumed = bool(row and row[0] == 2)
            permission = self.require(consumed=consumed)
            if consumed:
                attempt = self.ledger.db.execute('''SELECT a.at FROM execution_attempts a
                    JOIN entry_dispatch_permission p ON p.tag=a.tag WHERE p.nonce=?''',(permission['nonce'],)).fetchone()
                if not attempt or not 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(attempt[0])).total_seconds() <= 30:
                    raise Refused('Working entry permission expired')
            return True
        except (ValueError, TypeError):
            self.ledger.pause()
            return False

    def bind(self, tag):
        if not self.ledger.db.in_transaction:
            raise Refused('Atomic dispatch permission required')
        row = self.require()
        self.ledger.db.execute('INSERT INTO entry_dispatch_permission VALUES(?,?,?)',
            (tag,row['nonce'],datetime.now(timezone.utc).isoformat()))
        self.ledger.db.execute('UPDATE entry_permission SET active=2 WHERE id=1')

    def transport(self, request):
        """Final capability check immediately before the one-shot broker call."""
        row = self.require(consumed=True)
        bound = self.ledger.db.execute('SELECT * FROM entry_dispatch_permission WHERE tag=?', (request.get('tag'),)).fetchone()
        if (not bound or bound['nonce'] != row['nonce'] or
                not 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(bound['committed_at'])).total_seconds() <= 5):
            raise Refused('Fresh bound dispatch permission required')
        entry = self.ledger.get(request['tag'])
        body = json.loads(entry['body'])
        command = self.ledger.db.execute("SELECT status FROM broker_commands WHERE kind='ENTRY' AND tag=? AND operation='place'",(request['tag'],)).fetchone()
        if (entry['status'] != 'DISPATCHING' or not command or command[0] != 'SENDING' or
                request['kind'] != 'ENTRY' or request['symbol'] != body['symbol'] or
                request['quantity'] != body['quantity'] or str(request['price']) != body['limit_price']):
            raise Refused('Journalled entry request required')

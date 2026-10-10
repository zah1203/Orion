"""Two-account pilot configuration. Enrollment is never order authorization."""
import hashlib
import json
import time

from .ledger import Refused, amount, positive_int

MONEY = ('capital', 'max_order_premium', 'max_open_premium', 'daily_loss', 'fee_reserve', 'max_trade_loss', 'max_open_risk')
COUNTS = ('max_lots', 'max_entries')


def validate_limits(values):
    if not isinstance(values, dict) or set(values) != set(MONEY + COUNTS):
        raise Refused('Explicit pilot limits required')
    result = {}
    for name in MONEY:
        v = amount(values[name])
        if isinstance(values[name], bool) or not 0 < v <= 100000000 or v.as_tuple().exponent < -2:
            raise Refused('Invalid pilot monetary limit')
        result[name] = str(v)
    for name in COUNTS:
        positive_int(values[name])
        if values[name] > 100:
            raise Refused('Pilot count limit too large')
        result[name] = values[name]
    if not amount(result['max_order_premium']) <= amount(result['max_open_premium']) <= amount(result['capital']):
        raise Refused('Premium limits must fit capital')
    if amount(result['max_trade_loss']) > amount(result['max_open_risk']):
        raise Refused('Per-trade risk must fit open risk')
    if amount(result['daily_loss']) > amount(result['capital']):
        raise Refused('Daily loss must fit capital')
    return result


class Pilot:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS live_pilots(
                uid TEXT PRIMARY KEY REFERENCES users(id), version INTEGER NOT NULL,
                limits TEXT NOT NULL, consent INTEGER NOT NULL DEFAULT 0,
                enrolled INTEGER NOT NULL DEFAULT 1, ucc_hash TEXT NOT NULL)''')

    def configure(self, actor, uid, limits):
        limits = validate_limits(limits)
        self.store.user(uid)
        with self.store.lock(uid):
            credentials = self.store.credentials(uid)
            if not credentials.get('kotak_ucc'):
                raise Refused('Save account broker identity first')
            binding = hashlib.sha256(credentials['kotak_ucc'].encode()).hexdigest()
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                owner = db.execute('SELECT role,access FROM users WHERE id=?', (actor,)).fetchone()
                user = db.execute('SELECT role,access FROM users WHERE id=?', (uid,)).fetchone()
                if not owner or tuple(owner) != ('owner', 'approved') or not user or user['access'] != 'approved':
                    raise Refused('Approved owner and account required')
                if user['role'] == 'owner' and uid != actor:
                    raise Refused('Pilot owner mismatch')
                enrolled = db.execute('SELECT p.uid,p.ucc_hash,u.role FROM live_pilots p JOIN users u ON p.uid=u.id WHERE p.enrolled=1 AND p.uid!=?', (uid,)).fetchall()
                if len(enrolled) >= 2 or (uid != actor and any(r['role'] != 'owner' for r in enrolled)):
                    raise Refused('Pilot permits owner plus one additional account')
                if any(r['ucc_hash'] == binding for r in enrolled):
                    raise Refused('Pilot accounts require distinct broker identities')
                db.execute('''INSERT INTO live_pilots VALUES(?,1,?,0,1,?)
                    ON CONFLICT(uid) DO UPDATE SET version=version+1,limits=excluded.limits,
                    consent=0,enrolled=1,ucc_hash=excluded.ucc_hash''', (uid, json.dumps(limits, sort_keys=True), binding))
                db.execute('INSERT INTO admin_audit(at,actor,subject,action) VALUES(?,?,?,?)', (time.time(), actor, uid, 'live_pilot_configured'))
        return self.status(uid)

    def consent(self, uid, version):
        positive_int(version)
        with self.store.lock(uid), self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT access FROM users WHERE id=?', (uid,)).fetchone()
            changed = db.execute('UPDATE live_pilots SET consent=? WHERE uid=? AND enrolled=1 AND version=?', (version, uid, version)) if user and user[0] == 'approved' else None
            if changed is None or changed.rowcount != 1:
                raise Refused('Current enrolled policy required')
            db.execute('INSERT INTO admin_audit(at,actor,subject,action) VALUES(?,?,?,?)', (time.time(), uid, uid, 'live_pilot_reviewed'))
        return self.status(uid)

    def revoke(self, actor, uid):
        self.store.user(uid)
        with self.store.lock(uid), self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute('SELECT role,access FROM users WHERE id=?', (actor,)).fetchone()
            if actor != uid and (not owner or tuple(owner) != ('owner', 'approved')):
                raise Refused('Owner or account required')
            db.execute('UPDATE live_pilots SET enrolled=0,consent=0 WHERE uid=?', (uid,))
            db.execute('INSERT INTO admin_audit(at,actor,subject,action) VALUES(?,?,?,?)', (time.time(), actor, uid, 'live_pilot_revoked'))
        return self.status(uid)

    def status(self, uid):
        with self.store.db() as db:
            row = db.execute('SELECT version,limits,consent,enrolled FROM live_pilots WHERE uid=?', (uid,)).fetchone()
        return dict(enrolled=bool(row and row['enrolled']), version=row['version'] if row else None,
                    limits=json.loads(row['limits']) if row else None,
                    reviewed=bool(row and row['enrolled'] and row['consent'] == row['version']),
                    live_available=False, order_submission_available=False)

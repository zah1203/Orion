"""Durable notification journal. No network transport or incident resolution.

A delivery claim is committed before handing an event to a future sender. A
crash/timeout leaves SENDING or UNKNOWN for review, never an automatic resend.
Acknowledging notification delivery never clears incidents or resumes trading.
"""
import re
import uuid

from .ledger import Refused


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS live_alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, code TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING','SENDING','DELIVERED','UNKNOWN')),
        claim TEXT UNIQUE, claimed_at TEXT, finished_at TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS live_alert_health(
        id INTEGER PRIMARY KEY CHECK(id=1), signature TEXT NOT NULL)''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS live_incident_alert AFTER INSERT ON live_incidents
        BEGIN
            INSERT INTO live_alerts(kind,code,created_at)
            VALUES('incident',NEW.code,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
        END''')
    # Include incidents from ledgers created before notification journaling.
    db.execute('''INSERT INTO live_alerts(kind,code,created_at)
        SELECT 'incident',i.code,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM live_incidents i
        WHERE NOT EXISTS(SELECT 1 FROM live_alerts a WHERE a.kind='incident' AND a.code=i.code)''')


def health_event(ledger, state, uncovered):
    """Alert once per unhealthy transition; repetition cannot flood the queue."""
    code = 'uncovered-exposure' if uncovered > 0 else 'monitor-review-required' if state == 'review-required' else ''
    signature = code + ':' + str(uncovered) if code else 'healthy'
    with ledger.transaction():
        old = ledger.db.execute('SELECT signature FROM live_alert_health WHERE id=1').fetchone()
        if old and old[0] == signature:
            return
        ledger.db.execute('INSERT OR REPLACE INTO live_alert_health VALUES(1,?)', (signature,))
        if code:
            ledger.db.execute("INSERT INTO live_alerts(kind,code,created_at) VALUES('health',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))", (code,))


class AlertJournal:
    def __init__(self, ledger, account):
        if ledger.account != account:
            raise Refused('Alert account mismatch')
        self.ledger, self.db = ledger, ledger.db

    def claim(self):
        """Commit one one-shot delivery claim. No recipient or credentials stored."""
        if self.db.in_transaction:
            raise Refused('Delivery claim requires its own commit')
        with self.ledger.transaction():
            row = self.db.execute("SELECT * FROM live_alerts WHERE status='PENDING' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            self.db.execute("UPDATE live_alerts SET status='SENDING',claim=?,claimed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (token,row['id']))
            # Fixed codes only; malformed local content must never become a message.
            code = row['code'] if re.fullmatch(r'[a-z][a-z0-9-]{0,79}', row['code']) else 'incident-requires-review'
            return dict(account=self.ledger.account, id=row['id'], claim=token,
                        kind='incident' if row['kind'] == 'incident' else 'health', code=code, created_at=row['created_at'])

    def finish(self, alert_id, claim, *, delivered):
        """delivered=True requires a future sender's positive acknowledgement.

        False includes any uncertain send. UNKNOWN cannot be retried or upgraded
        automatically; receipt review and production delivery remain separate work.
        """
        if type(alert_id) is not int or alert_id < 1 or type(delivered) is not bool:
            raise Refused('Explicit delivery outcome required')
        if not isinstance(claim,str) or not re.fullmatch(r'[a-f0-9]{32}',claim):
            raise Refused('Valid delivery claim required')
        status = 'DELIVERED' if delivered else 'UNKNOWN'
        with self.ledger.transaction():
            row = self.db.execute('SELECT status,claim FROM live_alerts WHERE id=?', (alert_id,)).fetchone()
            if not row or row['claim'] != claim:
                raise Refused('Delivery claim mismatch')
            if row['status'] == status:
                return False
            if row['status'] != 'SENDING':
                raise Refused('Delivery outcome already recorded')
            self.db.execute("UPDATE live_alerts SET status=?,finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (status,alert_id))
            return True

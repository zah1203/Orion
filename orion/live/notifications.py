"""One-shot opt-in Expo delivery for Live alerts; never starts a trading worker."""
import json
import os
from pathlib import Path
import re
import time
import urllib.request

from .alerts import AlertJournal
from .ledger import Refused


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post(endpoint, payload):
    if endpoint not in ('send', 'getReceipts'):
        raise Refused('Unsupported notification operation')
    headers = {'Content-Type':'application/json','Accept':'application/json'}
    if os.environ.get('EXPO_ACCESS_TOKEN'):
        headers['Authorization'] = 'Bearer '+os.environ['EXPO_ACCESS_TOKEN']
    request = urllib.request.Request('https://exp.host/--/api/v2/push/'+endpoint,
                                    data=json.dumps(payload).encode(),headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request,timeout=10) as response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise Refused('Oversized notification response')
        return json.loads(raw)


class LiveNotifications:
    def __init__(self, store, ledger, *, _post=post):
        actual = ledger.db.execute('PRAGMA database_list').fetchone()['file']
        if Path(actual).resolve() != (store.account_dir(ledger.account)/'live.db').resolve():
            raise Refused('Notification ledger does not belong to account store')
        self.store, self.ledger, self.db, self.post = store, ledger, ledger.db, _post
        self.journal = AlertJournal(ledger, ledger.account)
        self.db.execute('''CREATE TABLE IF NOT EXISTS live_alert_receipts(
            alert_id INTEGER PRIMARY KEY, ticket TEXT UNIQUE NOT NULL,
            next_at REAL NOT NULL, expires_at REAL NOT NULL, device TEXT NOT NULL)''')

    def cycle(self, *, now=None):
        """One send OR receipt read per call; ticket acceptance is not delivery.

        Uses one primary opted-in device (latest expiry, deterministic tie-break).
        Receipt success means push-provider acceptance, not a human read receipt.
        Failure never retries a send, clears incidents or grants entry authority.
        """
        now = time.time() if now is None else now
        pending = self.db.execute('''SELECT r.*,a.claim FROM live_alert_receipts r
            JOIN live_alerts a ON a.id=r.alert_id WHERE a.status='SENDING' AND r.next_at<=?
            ORDER BY r.next_at LIMIT 1''', (now,)).fetchone()
        if pending:
            try:
                if now >= pending['expires_at']:
                    raise ValueError
                result = self.post('getReceipts', {'ids':[pending['ticket']]})
                if result.get('errors'):
                    raise ValueError
                receipt = result['data'].get(pending['ticket'])
                if not receipt:
                    self.db.execute('UPDATE live_alert_receipts SET next_at=? WHERE alert_id=?', (now+60,pending['alert_id']))
                    return 'receipt-pending'
                if receipt.get('status') != 'ok':
                    if receipt.get('details',{}).get('error') == 'DeviceNotRegistered':
                        with self.store.db() as db:
                            db.execute("UPDATE push_devices SET last_error='Notification registration required' WHERE id=? AND user_id=?",
                                       (pending['device'], self.ledger.account))
                    raise ValueError
                self.journal.finish(pending['alert_id'],pending['claim'],delivered=True)
                return 'provider-accepted'
            except Exception:
                self.journal.finish(pending['alert_id'],pending['claim'],delivered=False)
                return 'delivery-unknown'
        # Lock device ownership/opt-in through the one-shot send. This prevents a
        # concurrent logout/reassignment from substituting a different recipient.
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            device = db.execute('''SELECT d.id,d.ciphertext FROM push_devices d JOIN users u ON u.id=d.user_id
                WHERE d.user_id=? AND d.expires>? AND u.access='approved' AND d.last_error=''
                ORDER BY d.expires DESC,d.id LIMIT 1''', (self.ledger.account,now)).fetchone()
            if not device:
                return 'no-registered-device'
            event = self.journal.claim()
            if event is None:
                return 'idle'
            try:
                token = self.store.cipher.decrypt(device['ciphertext']).decode()
                if not re.fullmatch(r'(?:ExpoPushToken|ExponentPushToken)\[[A-Za-z0-9_-]+\]',token):
                    raise ValueError
                payload = dict(to=token,title='Orion Live needs attention',
                    body='Open Orion to review the account. Trading may require intervention.',
                    sound='default',priority='high',channelId='orion-attention',ttl=300,
                    data={'screen':'account'})
                result = self.post('send',payload)
                data = result['data']; ticket = data.get('id')
                if data.get('details',{}).get('error') == 'DeviceNotRegistered':
                    db.execute("UPDATE push_devices SET last_error='Notification registration required' WHERE id=?", (device['id'],))
                if result.get('errors') or data.get('status') != 'ok' or not isinstance(ticket,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ticket):
                    raise ValueError
                self.db.execute('INSERT INTO live_alert_receipts VALUES(?,?,?,?,?)', (event['id'],ticket,now+900,now+3600,device['id']))
                return 'receipt-pending'
            except Exception:
                self.journal.finish(event['id'],event['claim'],delivered=False)
                return 'delivery-unknown'


def dispatch_accounts(store, *, _post=post):
    """One bounded delivery cycle per existing ledger; never initializes one."""
    from .ledger import Ledger
    checked = failed = 0
    for user in store.users():
        if user['access'] != 'approved':
            continue
        path = store.account_dir(user['id'])/'live.db'
        if not path.is_file():
            continue
        ledger = None
        try:
            ledger = Ledger(path,user['id'])
            LiveNotifications(store,ledger,_post=_post).cycle()
            checked += 1
        except Exception:
            failed += 1
        finally:
            if ledger is not None:
                ledger.close()
    return dict(checked=checked,failed=failed)


def main():
    """Explicit independent runner. No installer enables it automatically."""
    import fcntl
    from orion.portal.store import Store
    key = Path(os.environ['ORION_PORTAL_KEY_FILE'])
    if key.is_symlink() or key.stat().st_mode & 0o077:
        raise Refused('Private portal key file required')
    root = Path(os.environ['ORION_PORTAL_DATA'])
    if not (root/'accounts.db').is_file() or any(p.is_symlink() for p in (root,*root.parents)):
        raise Refused('Existing private portal store required')
    store = Store(root,key.read_bytes().strip())
    with open(store.root/'live-notifications.lock','a') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            result = dispatch_accounts(store)
            # Counts only; never exception text, account IDs, devices or tokens.
            if result['failed']:
                print('Live notification cycle requires review',flush=True)
            time.sleep(15)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit('Live notification runner stopped; inspect configuration') from None

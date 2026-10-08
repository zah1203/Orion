"""Durable attention incidents and best-effort, opt-in device notifications.

Separate from trade processing: notification failure never authorizes an entry.
"""
import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import time
import urllib.request
from .store import Store

MESSAGES = {
    'kotak_auth': ('Kotak needs authentication', 'Open Orion and verify your Kotak connection. New entries are blocked.'),
    'kotak_feed': ('Kotak connection needs attention', 'The market feed is unavailable. Open Orion to check the connection; authentication may be required.'),
    'worker_offline': ('Orion monitoring interrupted', 'The account worker is offline. Open Orion to check its status.'),
}


def setup(db):
    db.execute('CREATE TABLE IF NOT EXISTS attention(user_id TEXT PRIMARY KEY, kind TEXT NOT NULL, since REAL NOT NULL, last_notice REAL NOT NULL DEFAULT 0, open_positions INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS push_devices(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,session_hash TEXT NOT NULL,ciphertext BLOB NOT NULL,expires REAL NOT NULL,last_error TEXT NOT NULL DEFAULT "")')
    db.execute('CREATE TABLE IF NOT EXISTS push_outbox(id INTEGER PRIMARY KEY, device TEXT NOT NULL, subject TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL DEFAULT "queued",attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL,ticket TEXT,created REAL NOT NULL)')


def classify(user, health, online, opened):
    if not opened and not user['enabled']:
        return None
    if not online:
        return 'worker_offline'
    if health.get('broker') == 'authentication_required':
        return 'kotak_auth'
    if health.get('broker') != 'connected':
        return 'kotak_feed'
    return None


def describe(row):
    title, message = MESSAGES[row['kind']]
    if row['open_positions']:
        message += ' Open paper positions cannot be reliably monitored while this interruption continues.'
    return {**dict(row), 'title': title, 'message': message}


class Attention:
    def __init__(self, store):
        self.store = store

    def has_open_positions(self, uid):
        path=self.store.account_dir(uid)/'paper.db'
        if not path.exists(): return False
        with sqlite3.connect(path) as db:
            row=db.execute('SELECT body FROM state WHERE id=1').fetchone()
        return bool(row and any(p['status']=='OPEN' and p.get('remaining',0)>0 for p in json.loads(row[0])['positions'].values()))

    def register(self, uid, token, session_token):
        key = hashlib.sha256(token.encode()).hexdigest()
        session_hash = hashlib.sha256(session_token.encode()).hexdigest()
        with self.store.db() as db:
            db.execute('DELETE FROM push_devices WHERE expires<?', (time.time(),))
            count = db.execute('SELECT count(*) FROM push_devices WHERE user_id=? AND id!=?', (uid,key)).fetchone()[0]
            if count >= 5:
                raise ValueError('Maximum five devices; disable alerts on an older device first')
            db.execute('INSERT INTO push_devices(id,user_id,session_hash,ciphertext,expires) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,session_hash=excluded.session_hash,ciphertext=excluded.ciphertext,expires=excluded.expires,last_error=""', (key,uid,session_hash,self.store.cipher.encrypt(token.encode()),time.time()+30*86400))
            # Never send a previous account's queued notification to a shared device.
            db.execute('DELETE FROM push_outbox WHERE device=?', (key,))
        return key

    def disable(self, uid, token):
        with self.store.db() as db:
            key=hashlib.sha256(token.encode()).hexdigest()
            if db.execute('SELECT id FROM push_devices WHERE id=? AND user_id=?',(key,uid)).fetchone():
                db.execute('DELETE FROM push_outbox WHERE device=?',(key,))
                db.execute('DELETE FROM push_devices WHERE id=?',(key,))

    def view(self, uid, owner=False):
        with self.store.db() as db:
            rows=db.execute('SELECT * FROM attention'+('' if owner else ' WHERE user_id=?'), () if owner else (uid,)).fetchall()
            devices=db.execute('SELECT count(*) FROM push_devices WHERE user_id=? AND expires>?',(uid,time.time())).fetchone()[0]
            errors=db.execute('SELECT count(*) FROM push_devices WHERE user_id=? AND last_error!=""',(uid,)).fetchone()[0]
        return {'incidents':[describe(r) for r in rows], 'registered_devices':devices, 'devices_with_errors':errors}

    def scan(self, now=None):
        now = time.time() if now is None else now
        users=self.store.users()
        for item in users:
            uid=item['id']; user=self.store.user(uid)
            opened=self.has_open_positions(uid)
            kind=classify(user,self.store.health(uid),self.store.worker_alive(uid),opened)
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT * FROM attention WHERE user_id=?',(uid,)).fetchone()
                if not kind:
                    db.execute('DELETE FROM attention WHERE user_id=?',(uid,))
                    db.execute('DELETE FROM push_outbox WHERE subject=? AND status="queued"',(uid,))
                    continue
                if not row or row['kind']!=kind:
                    # One interruption retains its timer across reconnect/auth status changes.
                    since=row['since'] if row else now
                    last=row['last_notice'] if row else 0
                    db.execute('INSERT OR REPLACE INTO attention VALUES(?,?,?,?,?)',(uid,kind,since,last,int(opened)))
                    row={'since':since,'last_notice':last}
                else:
                    db.execute('UPDATE attention SET open_positions=? WHERE user_id=?',(int(opened),uid))
                # Debounce transient startups/reconnects; repeat only while unresolved.
                delay=30 if kind=='kotak_auth' else 90
                if now-row['since']<delay or (row['last_notice'] and now-row['last_notice']<900):
                    continue
                title,_=MESSAGES[kind]
                body='An Orion account needs attention. Open the app to check its connection.'
                if opened:
                    body+=' Open paper positions may not be monitored.'
                recipients={uid}|{u['id'] for u in users if u['role']=='owner' and u['access']=='approved'}
                devices=[]
                for recipient in recipients:
                    devices.extend(db.execute('SELECT id FROM push_devices WHERE user_id=? AND expires>?',(recipient,now)).fetchall())
                for device in devices:
                    payload={'title':title,'body':body,'sound':'default','priority':'high','channelId':'orion-attention','ttl':300,'data':{'screen':'attention'}}
                    db.execute('INSERT INTO push_outbox(device,subject,kind,body,next_at,created) VALUES(?,?,?,?,?,?)',(device['id'],uid,kind,json.dumps(payload),now,now))
                if devices:
                    db.execute('UPDATE attention SET last_notice=? WHERE user_id=?',(now,uid))
        with self.store.db() as db:
            db.execute('DELETE FROM push_devices WHERE expires<?',(now,))
            db.execute('DELETE FROM push_outbox WHERE created<? OR device NOT IN (SELECT id FROM push_devices)',(now-7*86400,))

    @staticmethod
    def post(endpoint, payload):
        headers={'Content-Type':'application/json','Accept':'application/json'}
        if os.environ.get('EXPO_ACCESS_TOKEN'):
            headers['Authorization']='Bearer '+os.environ['EXPO_ACCESS_TOKEN']
        request=urllib.request.Request('https://exp.host/--/api/v2/push/'+endpoint, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(request,timeout=10) as response:
            return json.loads(response.read(1024*1024))

    def dispatch(self, now=None):
        now=time.time() if now is None else now
        with self.store.db() as db:
            rows=db.execute('SELECT o.*,d.ciphertext FROM push_outbox o JOIN push_devices d ON d.id=o.device WHERE o.status IN ("queued","receipt") AND o.next_at<=? AND d.expires>? ORDER BY o.id LIMIT 20',(now,now)).fetchall()
        for r in rows:
            try:
                if r['status']=='queued':
                    with self.store.db() as db:
                        incident=db.execute('SELECT 1 FROM attention WHERE user_id=?',(r['subject'],)).fetchone()
                    if not incident:
                        with self.store.db() as db: db.execute('DELETE FROM push_outbox WHERE id=?',(r['id'],))
                        continue
                    payload=json.loads(r['body']);payload['to']=self.store.cipher.decrypt(r['ciphertext']).decode()
                    result=self.post('send',payload).get('data',{})
                    status='receipt'; ticket=result.get('id'); next_at=now+900
                    if result.get('status')=='ok' and not ticket: raise ValueError('Missing ticket')
                else:
                    result=self.post('getReceipts',{'ids':[r['ticket']]}).get('data',{}).get(r['ticket'])
                    if not result: raise ValueError('Receipt unavailable')
                    status='provider_accepted';ticket=r['ticket'];next_at=now
                error=result.get('details',{}).get('error')
                if result.get('status')!='ok':
                    if error=='DeviceNotRegistered':
                        with self.store.db() as db:
                            db.execute('DELETE FROM push_devices WHERE id=?',(r['device'],))
                            db.execute('DELETE FROM push_outbox WHERE device=?',(r['device'],))
                        continue
                    raise ValueError('Push provider rejected notification')
                with self.store.db() as db:
                    db.execute('UPDATE push_outbox SET status=?,ticket=?,next_at=? WHERE id=?',(status,ticket,next_at,r['id']))
                    db.execute('UPDATE push_devices SET last_error="" WHERE id=?',(r['device'],))
            except Exception:
                # Never persist/log provider messages or device tokens.
                tries=r['attempts']+1
                with self.store.db() as db:
                    db.execute('UPDATE push_outbox SET attempts=?,next_at=?,status=? WHERE id=?',(tries,now+min(60*2**min(tries,6),3600),'failed' if tries>=5 else r['status'],r['id']))
                    db.execute('UPDATE push_devices SET last_error="Notification delivery needs checking" WHERE id=?',(r['device'],))

    async def run(self):
        async def scan_loop():
            while True:
                await asyncio.to_thread(self.scan)
                await asyncio.sleep(15)
        async def delivery_loop():
            while True:
                await asyncio.to_thread(self.dispatch)
                await asyncio.sleep(15)
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(scan_loop())
            tasks.create_task(delivery_loop())


def main():
    import fcntl
    key=Path(os.environ['ORION_PORTAL_KEY_FILE'])
    if key.stat().st_mode & 0o077: raise SystemExit('Key must be owner-only')
    store=Store(os.environ['ORION_PORTAL_DATA'],key.read_bytes().strip())
    with open(store.root/'attention.lock','a') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        asyncio.run(Attention(store).run())


if __name__=='__main__': main()

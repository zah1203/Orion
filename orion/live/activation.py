"""Explicit, own-account monitoring startup; never grants entry permission.

The portal forwards one TOTP over a private local socket to the existing
supervisor. The supervisor supplies it to an isolated child over an anonymous
pipe. No automatic login/restart and no secret-bearing persistent requests.
"""
import asyncio
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time

from .ledger import Ledger, Refused, positive_int
from .reconciliation import Reconciliation, require_current
from .routing import reviewed_policy
from .startup import ReviewedAccountMonitor

SOCKET = '/run/orion-live/activation.sock'
CONFIRMATION = 'START LIVE MONITORING; ENTRIES REMAIN PAUSED'


def validate_request(payload):
    if not isinstance(payload, dict) or set(payload) != {'uid', 'policy_version', 'totp', 'confirmation'}:
        raise Refused('Explicit monitoring request required')
    if not isinstance(payload['uid'], str) or not re.fullmatch('[a-f0-9]{32}', payload['uid']):
        raise Refused('Account required')
    positive_int(payload['policy_version'])
    if not isinstance(payload['totp'], str) or not re.fullmatch('[0-9]{6}', payload['totp']):
        raise Refused('Current six-digit TOTP required')
    if payload['confirmation'] != CONFIRMATION:
        raise Refused('Monitoring confirmation required')


def preflight(store, uid, version):
    """Caller holds account lock; final check also runs under worker lease."""
    user = store.user(uid)
    credentials = store.credentials(uid)
    policy = reviewed_policy(store, uid, credentials.get('kotak_ucc'))
    if policy['version'] != version:
        raise Refused('Review the current pilot limits first')
    if user['enabled']:
        raise Refused('Paper entries must already be paused; no settings were changed')
    required = ('kotak_consumer_key', 'kotak_mobile', 'kotak_ucc', 'kotak_mpin',
                'telegram_session', 'telegram_api_id', 'telegram_api_hash')
    if not all(credentials.get(k) for k in required):
        raise Refused('Complete the saved broker and Telegram connections first')
    if not user['settings'].get('channels'):
        raise Refused('Select account channels first')
    paper = store.account_dir(uid)/'paper.db'
    if paper.is_symlink():
        raise Refused('Paper path requires review')
    if paper.exists():
        # Read the WAL-aware current state without opening a writable Paper DB.
        with closing(sqlite3.connect(paper.absolute().as_uri()+'?mode=ro', uri=True)) as db:
            row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
            if not row:
                raise Refused('Paper state requires review')
            positions = json.loads(row[0])['positions']
            if any(p['status'] not in ('CLOSED', 'CANCELLED', 'REJECTED', 'EXPIRED') for p in positions.values()):
                raise Refused('Finish pending and open Paper positions before handover')
    return credentials['kotak_ucc']


class ActivationMonitor(ReviewedAccountMonitor):
    _portfolio = True

    def _authenticate(self):
        from .authorization import EntryAuthority
        from .startup import EntryCheckedSession
        protected = super()._authenticate()
        try:
            self.entry_authority = EntryAuthority(self)
            return EntryCheckedSession(protected._session, self.entry_authority.transport)
        except BaseException:
            protected.close()
            raise

    def _open_ledger(self, path):
        with self.store.lock(self.uid):
            ucc = preflight(self.store, self.uid, self._policy_version)
            existed = path.exists()
            ledger = Ledger(path, self.uid)
            try:
                if existed:
                    rows = ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchall()
                    if len(rows) != 1 or rows[0][0] != ucc:
                        raise Refused('Existing ledger identity requires review')
                Reconciliation(ledger, ucc)  # Existing UCC cannot be rebound.
                ledger.pause()
                with self.store.db() as db:
                    db.execute('''INSERT INTO live_worker_slots VALUES(?,?,1,0)
                        ON CONFLICT(uid) DO UPDATE SET policy_version=excluded.policy_version,
                        reserved=1,stop_requested=0''', (self.uid, self._policy_version))
                    db.execute('INSERT INTO admin_audit(at,actor,subject,action) VALUES(?,?,?,?)',
                               (time.time(), self.uid, self.uid, 'live_monitor_start_requested'))
                return ledger
            except BaseException:
                ledger.close()
                raise


def request_stop(store, uid):
    with store.lock(uid), store.db() as db:
        changed = db.execute('UPDATE live_worker_slots SET stop_requested=1 WHERE uid=? AND reserved=1', (uid,))
        if not changed.rowcount:
            raise Refused('No reserved Live worker slot')
        db.execute('INSERT INTO admin_audit(at,actor,subject,action) VALUES(?,?,?,?)',
                   (time.time(), uid, uid, 'live_monitor_stop_requested'))
    return dict(state='stop-requested', order_submission_available=False)


def release_if_flat(account):
    """Only an explicit stop plus a fresh complete reconciliation releases Paper."""
    store, uid, ledger = account.store, account.uid, account.ledger
    if getattr(account.session, 'closed', False) is True:
        # Transport has already killed/reaped its broker child. End this unusable
        # worker, retain the reservation/journal, and require a new explicit TOTP.
        # Never resume Paper or retry an uncertain command on session failure.
        ledger.pause()
        return True
    with store.lock(uid), store.db() as db:
        row = db.execute('SELECT stop_requested FROM live_worker_slots WHERE uid=? AND reserved=1', (uid,)).fetchone()
        if not row or not row[0]:
            return False
        with ledger.transaction():
            try:
                require_current(ledger)
                health = account.monitor.health()
                if health['state'] != 'observed' or health['exposure'] != 0:
                    return False
                if ledger.db.execute('SELECT 1 FROM live_incidents').fetchone():
                    return False
                for table in ('intents', 'protective_exits'):
                    if ledger.db.execute(f"SELECT 1 FROM {table} WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','ABANDONED')").fetchone():
                        return False
                if ledger.db.execute("SELECT 1 FROM broker_commands WHERE status!='CONFIRMED'").fetchone():
                    return False
                if ledger.db.execute("SELECT 1 FROM manual_requests WHERE state='PENDING'").fetchone():
                    return False
                ledger.pause()
                db.execute('UPDATE live_worker_slots SET reserved=0,stop_requested=0 WHERE uid=?', (uid,))
                db.execute('INSERT INTO admin_audit(at,actor,subject,action) VALUES(?,?,?,?)',
                           (time.time(), uid, uid, 'live_monitor_flat_stop_verified'))
                return True
            except (Refused, sqlite3.Error):
                return False


def snapshot(store, uid):
    store.user(uid)
    with store.db() as db:
        row = db.execute('SELECT reserved,stop_requested FROM live_worker_slots WHERE uid=?', (uid,)).fetchone()
    from .controls import snapshot as controls
    current = controls(store, uid)
    reserved = bool(row and row['reserved'])
    return dict(reserved=reserved, stop_requested=bool(row and row['stop_requested']),
                worker_available=reserved and current['worker_available'],
                state=('monitoring' if current['worker_available'] else 'authentication-required') if reserved else 'not-started',
                order_submission_available=False)


async def send_request(payload, *, socket_path=SOCKET):
    validate_request(payload)
    writer = None
    try:
        async with asyncio.timeout(40):
            reader, writer = await asyncio.open_unix_connection(socket_path, limit=4096)
            writer.write(json.dumps(payload).encode()+b'\n')
            await writer.drain()
            result = json.loads(await reader.readline())
            if result not in ({'state': 'started-paused'}, {'state': 'startup-refused'}, {'state': 'startup-unknown'}):
                raise ValueError
            return result | dict(order_submission_available=False)
    except Exception:
        # A lost acknowledgement must not kill/restart an authenticated worker.
        return dict(state='startup-unknown', order_submission_available=False)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


class ActivationServer:
    """Owned by the supervisor, not by an HTTP request or portal restart."""
    def __init__(self, store, master, key_file, *, socket_path=SOCKET):
        self.store, self.master, self.key_file = store, master, key_file
        self.socket_path = Path(socket_path)
        self.children = {}
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.server = None
        self.bound = False
        self.closing = False

    async def start(self):
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Supervisor's exclusive lease is required before removing a stale socket.
        if self.socket_path.is_symlink() or (self.socket_path.exists() and not self.socket_path.is_socket()):
            raise Refused('Activation socket path requires review')
        self.socket_path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self.handle, self.socket_path, limit=4096)
        self.bound = True
        self.socket_path.chmod(0o600)

    async def launch(self, payload):
        validate_request(payload)
        uid = payload['uid']
        async with self.lock:
            self.children = {k: v for k, v in self.children.items() if v.returncode is None}
            if self.closing or uid in self.children or len(self.children) >= 2 or self.store.worker_running(uid):
                return {'state': 'startup-refused'}
            with self.store.lock(uid):
                preflight(self.store, uid, payload['policy_version'])
            env = dict(PATH=os.defpath, ORION_PORTAL_DATA=str(self.store.root),
                       ORION_PORTAL_KEY_FILE=str(self.key_file), NEO_LOG_FILE_ENABLED='false')
            child = await asyncio.create_subprocess_exec(sys.executable, '-m', 'orion.live.activation',
                '--master', str(self.master), env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=4096)
            self.children[uid] = child
            child.stdin.write(json.dumps(payload).encode()+b'\n')
            await child.stdin.drain()
            child.stdin.close()
        try:
            line = await asyncio.wait_for(child.stdout.readline(), 35)
            if line == b'{"state":"started-paused"}\n':
                return {'state': 'started-paused'}
            return {'state': 'startup-refused'} if child.returncode is not None or not line else {'state': 'startup-unknown'}
        except (TimeoutError, ValueError):
            return {'state': 'startup-unknown'}

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            payload = json.loads(await asyncio.wait_for(reader.readline(), 3))
            validate_request(payload)
            result = await self.launch(payload)
            del payload
        except Exception:
            result = {'state': 'startup-refused'}
        try:
            writer.write(json.dumps(result).encode()+b'\n')
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            self.tasks.discard(task)

    async def close(self):
        self.closing = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        async def stop(child):
            if child.returncode is None:
                try:
                    child.send_signal(signal.SIGINT)
                    await asyncio.wait_for(child.wait(), 45)
                except ProcessLookupError:
                    pass
                except TimeoutError:
                    child.kill()
                    await child.wait()
        await asyncio.gather(*(stop(child) for child in self.children.values()))
        if self.bound:
            self.socket_path.unlink(missing_ok=True)


async def run_child(store, payload, master, ready):
    from .worker import serve_account
    validate_request(payload)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    monitor = ActivationMonitor(store, payload['uid'], payload['policy_version'], payload.pop('totp'))
    from .entry_worker import EntryWorker
    entries = None
    def started(account):
        nonlocal entries
        entries = EntryWorker(account)
        ready()
    def after_cycle(account):
        if release_if_flat(account):
            return True
        entries.cycle()
        return release_if_flat(account)
    try:
        await serve_account(store, monitor.uid, lambda: json.loads(Path(master).read_text()), None,
            stop=stop, collect_trades=True, _monitor_factory=lambda *a, **k: monitor,
            _on_started=started, _after_cycle=after_cycle)
    finally:
        monitor._totp = None
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    import argparse
    import logging
    import resource
    from ..portal.store import Store
    parser = argparse.ArgumentParser(description='Supervisor-owned Live monitoring child')
    parser.add_argument('--master', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    logging.disable(logging.CRITICAL)
    ack = os.dup(1)
    with open(os.devnull, 'w') as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
    try:
        payload = json.loads(sys.stdin.buffer.readline(4097))
        validate_request(payload)
        key = Path(os.environ['ORION_PORTAL_KEY_FILE'])
        if key.is_symlink() or key.stat().st_mode & 0o077:
            raise Refused('Private key file required')
        store = Store(os.environ['ORION_PORTAL_DATA'], key.read_bytes().strip())
        # Shared lease survives through reservation and login acknowledgement.
        # Deployment owns it exclusively before checking slots and switching code.
        with open(Path(SOCKET).parent/'deployment.lock', 'a') as deployment:
            fcntl.flock(deployment, fcntl.LOCK_SH | fcntl.LOCK_NB)
            def ready():
                os.write(ack, b'{"state":"started-paused"}\n')
                fcntl.flock(deployment, fcntl.LOCK_UN)
            asyncio.run(run_child(store, payload, args.master, ready))
        return 0
    except BaseException:
        return 1
    finally:
        os.close(ack)


if __name__ == '__main__':
    sys.exit(main())

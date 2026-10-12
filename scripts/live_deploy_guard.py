"""Refuse activation of another release while Live owns any account slot."""
from contextlib import closing
from pathlib import Path
import sqlite3
import fcntl
import os


def acquire_deployment_lease(root=Path('/')):
    directory = root/'run/orion-live'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise RuntimeError('Deployment lock directory requires review')
    fd = os.open(directory/'deployment.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lease = os.fdopen(fd, 'a')
    try:
        if os.fstat(fd).st_nlink != 1:
            raise RuntimeError('Deployment lock requires review')
        os.fchmod(lease.fileno(), 0o600)
        if root == Path('/'):
            import pwd
            user = pwd.getpwnam('orion')
            os.chown(directory, user.pw_uid, user.pw_gid)
            os.fchown(lease.fileno(), user.pw_uid, user.pw_gid)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_released_slots(root/'var/lib/orion/portal')
        return lease
    except BaseException:
        lease.close()
        raise


def require_released_slots(root=Path('/var/lib/orion/portal')):
    path = root/'accounts.db'
    if not path.exists():
        return
    try:
        with closing(sqlite3.connect(path.absolute().as_uri()+'?mode=ro', uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='live_worker_slots'").fetchone():
                if db.execute('SELECT 1 FROM live_worker_slots WHERE reserved=1').fetchone():
                    raise RuntimeError('Live slots are reserved. Complete verified-flat worker stop before deployment.')
    except sqlite3.Error:
        raise RuntimeError('Live deployment preflight requires operator review') from None

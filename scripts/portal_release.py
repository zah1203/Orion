"""Activate a tested portal release without switching or restarting Paper code."""
from pathlib import Path
from contextlib import closing
import re
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

DROPIN = 'etc/systemd/system/orion-portal.service.d/90-orion-release.conf'
MARKER = '# Managed by Orion portal-only deployment\n'


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.new')
    temporary.write_bytes(data)
    temporary.chmod(0o644)
    temporary.replace(path)


def worker_state(run):
    result = run(['systemctl', 'list-units', '--all', '--type=service', '--plain',
                  '--no-legend', '--no-pager', 'orion-worker@*.service',
                  'orion-supervisor.service', 'orion-attention.service', 'orion.service'],
                 capture_output=True, text=True, check=True)
    units = sorted({line.split()[0] for line in result.stdout.splitlines() if line.split()})
    if any(not re.fullmatch(r'orion(?:-worker@[a-zA-Z0-9_.\\-]+|-supervisor|-attention)?\.service', u) for u in units):
        raise RuntimeError('Unexpected worker unit inventory')
    return {u: run(['systemctl', 'show', u, '--property=MainPID,InvocationID,ActiveState',
                    '--no-pager'], capture_output=True, text=True, check=True).stdout for u in units}


def portal_health(root):
    origin = 'http://127.0.0.1:8000'
    for name in ('portal.env', 'public-web.env'):
        path = root/'etc/orion'/name
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith('ORION_PORTAL_ORIGIN='):
                    origin = line.split('=', 1)[1].strip().strip('"\' ')
    request = urllib.request.Request('http://127.0.0.1:8000/',
                                    headers={'Host': urlsplit(origin).netloc})
    for _ in range(30):
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('Portal health check failed')


def activate(release_id, *, root=Path('/'), run=subprocess.run, health=portal_health):
    from scripts.live_deploy_guard import acquire_deployment_lease
    with closing(acquire_deployment_lease(root)):
        return _activate(release_id, root=root, run=run, health=health)


def _activate(release_id, *, root, run, health):
    if not re.fullmatch('[0-9a-f]{40}', release_id):
        raise ValueError('Expected Git commit')
    release = root/'opt/orion/releases'/release_id
    if release.is_symlink() or not (release/'.venv/bin/python').is_file():
        raise RuntimeError('Tested release environment required')
    current = root/'opt/orion/current'
    old_current = current.resolve(strict=True)
    dropin = root/DROPIN
    if dropin.is_symlink():
        raise RuntimeError('Refusing symlink portal override')
    previous = dropin.read_bytes() if dropin.exists() else None
    if previous is not None and not previous.startswith(MARKER.encode()):
        raise RuntimeError('Existing portal override requires review')
    if run(['systemctl', 'is-active', '--quiet', 'orion-portal.service']).returncode:
        raise RuntimeError('Portal-only deployment requires a running portal')
    workers = worker_state(run)
    # Pin the portal alone. /opt/orion/current, supervisor, attention and workers
    # remain on the old release, including any worker restarted by the supervisor.
    pinned = '/opt/orion/releases/'+release_id
    override = (MARKER+'[Service]\nWorkingDirectory='+pinned+'\nExecStart=\nExecStart='+pinned+
                '/.venv/bin/python -m uvicorn orion.portal.app:factory --factory --host 127.0.0.1 '
                '--port 8000 --workers 1 --no-proxy-headers --no-access-log\n'
                'ReadWritePaths=-/run/orion-live\n')
    try:
        atomic_write(dropin, override.encode())
        run(['systemctl', 'daemon-reload'], check=True)
        run(['systemctl', 'restart', 'orion-portal.service'], check=True)
        health(root)
        actual = run(['systemctl', 'show', 'orion-portal.service', '--property=WorkingDirectory',
                      '--value'], capture_output=True, text=True, check=True).stdout.strip()
        if actual != pinned:
            raise RuntimeError('Portal release verification failed')
        if current.resolve(strict=True) != old_current or worker_state(run) != workers:
            raise RuntimeError('Paper worker continuity check failed')
    except Exception:
        # Roll back only the portal pointer. Never restore/overwrite application
        # databases: this release uses additive, backward-compatible tables.
        if previous is None:
            dropin.unlink(missing_ok=True)
        else:
            atomic_write(dropin, previous)
        try:
            run(['systemctl', 'daemon-reload'], check=True)
            run(['systemctl', 'restart', 'orion-portal.service'], check=True)
            health(root)
        except Exception:
            raise RuntimeError('Portal deployment failed; rollback needs operator review') from None
        raise RuntimeError('Portal deployment failed; previous portal configuration restored') from None
    print('Portal release verified: '+release_id)
    print('Paper worker identities and current release unchanged. Live orders remain disabled.')

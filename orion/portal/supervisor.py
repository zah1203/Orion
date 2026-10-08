"""Unprivileged, bounded process supervisor for isolated paper workers."""
import asyncio
import fcntl
import os
from pathlib import Path
import signal
import sys
import time
from .store import Store
from .service import Accounts
from .app import defaults


class Supervisor:
    def __init__(self, store, master, limit=20):
        self.store, self.master, self.limit = store, master, limit
        self.accounts = Accounts(store, defaults())
        self.children = {}
        self.retry_at = {}

    def wanted(self, user):
        uid = user["id"]
        # Suspension and pause block entries, but retain monitoring for open paper positions.
        active = self.accounts.active_positions(uid)
        if not active and not (user["access"] == "approved" and self.store.user(uid)["enabled"]):
            return False
        creds = self.store.credentials(uid)
        required = ("telegram_session", "telegram_api_id", "telegram_api_hash", "kotak_consumer_key", "kotak_mobile", "kotak_ucc", "kotak_mpin")
        return bool(self.store.config(uid)["channels"] and all(creds.get(k) for k in required))

    async def stop(self, uid):
        child = self.children.pop(uid)
        if child.returncode is None:
            child.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(child.wait(), 25)
            except asyncio.TimeoutError:
                child.kill()
                await child.wait()

    async def tick(self):
        for uid, child in list(self.children.items()):
            if child.returncode is not None:
                self.children.pop(uid)
                self.retry_at[uid] = time.monotonic() + 30
        for user in self.store.users():
            uid = user["id"]
            wanted = self.wanted(user)
            if not wanted and uid in self.children:
                await self.stop(uid)
            elif wanted and uid not in self.children:
                # The worker's file lease is the final duplicate-process guard.
                if self.store.worker_running(uid) or time.monotonic() < self.retry_at.get(uid, 0):
                    continue
                if len(self.children) >= self.limit:
                    self.store.health(uid, {"supervisor": "Waiting for worker capacity"})
                    continue
                self.children[uid] = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "orion.portal", "worker", "--background",
                    "--username", user["username"], "--master", self.master,
                    stdin=asyncio.subprocess.DEVNULL,
                )

    async def run(self):
        try:
            while True:
                await self.tick()
                await asyncio.sleep(5)
        finally:
            await asyncio.gather(*(self.stop(uid) for uid in list(self.children)))


def main():
    key = Path(os.environ["ORION_PORTAL_KEY_FILE"])
    if key.stat().st_mode & 0o077:
        raise SystemExit("Key must be owner-only")
    store = Store(os.environ["ORION_PORTAL_DATA"], key.read_bytes().strip())
    limit = int(os.environ.get("ORION_MAX_WORKERS", "20"))
    if not 1 <= limit <= 100:
        raise SystemExit("Worker capacity must be 1–100")
    with open(store.root / "supervisor.lock", "a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(Supervisor(store, str(store.root / "contracts.json"), limit).run())


if __name__ == "__main__":
    main()

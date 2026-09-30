"""Non-interactive worker controls; Telegram remains independent of broker login."""

import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path

from ..core import IST
from .broker_session import load
from .catalogue_refresh import refresh


class Background:
    def __init__(self, store, uid, master_path):
        self.store, self.uid = store, uid
        self.path = Path(master_path)
        self.state = {
            "telegram": "connecting",
            "broker": "authentication_required",
            "catalogue": "missing_or_stale",
        }
        self.signature = None
        self.master = {"synthetic": False, "as_of": "", "contracts": []}

    def status(self, **patch):
        self.state.update(patch)
        self.store.health(self.uid, self.state)
        self.store.heartbeat(self.uid)

    def session(self):
        return load(self.store, self.uid)

    async def refresh_catalogue(self):
        next_attempt = None
        while True:
            now = datetime.now(IST)
            _, current = self.catalogue()
            morning = now.replace(hour=8, minute=30, second=0, microsecond=0)
            if current:
                self.status(catalogue_refresh="Current",
                            catalogue_refreshed_at=self.master.get("verified_at"))
                next_attempt = None
            elif now < morning:
                self.status(catalogue_refresh="Scheduled for 08:30 IST")
            elif next_attempt is None or now >= next_attempt:
                self.status(catalogue_refresh="Downloading and validating broker exports")
                try:
                    result = await asyncio.to_thread(
                        refresh, self.path, self.store.user(self.uid)["username"]
                    )
                except Exception:
                    # Never expose SDK exceptions, URLs or credentials in health output.
                    result = {"catalogue_refresh": "Refresh failed; retrying in 5 minutes"}
                self.catalogue()
                self.status(**result)
                next_attempt = datetime.now(IST) + timedelta(minutes=5)
            await asyncio.sleep(30)

    def catalogue(self):
        try:
            stat = self.path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if signature != self.signature:
                candidate = json.loads(self.path.read_text())
                if (
                    not isinstance(candidate, dict)
                    or candidate.get("synthetic") is not False
                    or not isinstance(candidate.get("contracts"), list)
                    or not candidate["contracts"]
                ):
                    raise ValueError("Invalid catalogue")
                self.master = candidate
                self.signature = signature
            current = self.master.get("as_of") == datetime.now(IST).date().isoformat()
        except (OSError, ValueError, KeyError):
            current = False
        self.state["catalogue"] = "current" if current else "missing_or_stale"
        return self.master, current

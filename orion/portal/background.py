"""Non-interactive worker controls; Telegram remains independent of broker login."""

from datetime import datetime
import json
from pathlib import Path

from ..core import IST
from .broker_session import load


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

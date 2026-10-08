"""Channel attribution and daily realized P&L from the actual paper ledger."""
from decimal import Decimal
import json
import sqlite3


def enrich(pnl, state, path, channels):
    groups = {cid: dict(channel_id=cid, name=c.get("name", cid), trades=0, closed=0, wins=0,
                       realized=Decimal(0), unrealized=Decimal(0), stale_positions=0, missing_positions=0)
              for cid, c in channels.items()}
    exits = {}
    if path.exists():
        with sqlite3.connect(path) as db:
            for at, event, body in db.execute("SELECT at,event,body FROM audit WHERE event IN ('PAPER_EXIT','TARGET_REACHED') ORDER BY seq"):
                detail = json.loads(body)
                if event == 'PAPER_EXIT' or detail.get('remaining') == 0:
                    exits[detail.get('signal_id')] = at
    for p in pnl["positions"]:
        p["exit_time"] = exits.get(p["signal_id"])
        cid = p["channel_id"]
        g = groups.setdefault(cid, dict(channel_id=cid, name=cid, trades=0, closed=0, wins=0,
                                        realized=Decimal(0), unrealized=Decimal(0), stale_positions=0, missing_positions=0))
        g["trades"] += 1
        g["closed"] += p["status"] == "CLOSED"
        g["wins"] += p["status"] == "CLOSED" and Decimal(p["realized"]) > 0
        g["realized"] += Decimal(p["realized"])
        g["stale_positions"] += p["mark_status"] == "stale"
        g["missing_positions"] += p["unrealized"] is None
        if p["unrealized"] is not None:
            g["unrealized"] += Decimal(p["unrealized"])
    for g in groups.values():
        g["realized"] = str(g["realized"])
        g["unrealized"] = None if g["missing_positions"] else str(g["unrealized"])
    pnl["channels"] = list(groups.values())
    pnl["daily"] = [dict(date=day, realized=values["pnl"], entries=values["entries"])
                    for day, values in sorted(state.get("days", {}).items())]
    return pnl


def activity(path, before=None, limit=100):
    if not path.exists():
        return {"events": [], "next_cursor": None}
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT seq,at,event,body FROM audit WHERE seq<? ORDER BY seq DESC LIMIT ?",
                          (before if before is not None else 9223372036854775807, limit + 1)).fetchall()
    return {"events": [dict(seq=r[0], at=r[1], event=r[2], **json.loads(r[3])) for r in rows[:limit]],
            "next_cursor": rows[limit-1][0] if len(rows) > limit else None}

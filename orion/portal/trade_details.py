"""Read-only, account-local trade evidence; Telegram claims are not fills."""
from contextlib import closing
import json
import sqlite3
from ..core import dec, stamp


def details(path, signal_id, channels, offset=0):
    if not path.exists():
        raise LookupError("Trade not found")
    channel, sep, message_id = signal_id.rpartition(":")
    if not sep or not channel or not message_id:
        raise LookupError("Trade not found")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT body FROM state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else {}
        position = state.get("positions", {}).get(signal_id)
        messages = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_events'").fetchone():
            # Filter out quote payloads in SQLite, not in the HTTP response.
            for received, body in db.execute(
                "SELECT received_at,body FROM source_events WHERE json_extract(body,'$.type')='message' "
                "AND CAST(json_extract(body,'$.channel_id') AS TEXT)=? ORDER BY received_at,id", (channel,)
            ):
                e = json.loads(body)
                messages.append(dict(message_id=str(e.get("message_id")), reply_to=str(e.get("reply_to")),
                                     source_time=e.get("source_time"), at=received,
                                     text=e.get("text", ""), edited=bool(e.get("edited"))))
        linked = {message_id}
        while True:
            added = {m["message_id"] for m in messages if m["reply_to"] in linked} - linked
            if not added:
                break
            linked.update(added)
        messages = [m for m in messages if m["message_id"] in linked]
        ids = {channel + ":" + mid for mid in linked}
        events = []
        for seq, at, event, body in db.execute("SELECT seq,at,event,body FROM audit ORDER BY seq"):
            b = json.loads(body)
            if b.get("signal_id") in ids:
                events.append(dict(kind="orion", seq=seq, at=at, event=event, body=b))
        if position is None and not events and not messages:
            raise LookupError("Trade not found")
    own = [e for e in events if e["body"].get("signal_id") == signal_id]
    buys = [e for e in own if e["event"] == "PAPER_BUY"]
    exits = [e for e in own if e["event"] == "PAPER_EXIT" or
             (e["event"] == "TARGET_REACHED" and e["body"].get("remaining") == 0)]
    p = position or {}
    initial_stop = buys[0]["body"].get("stop") if buys else (p.get("stop") if not p.get("entry_fill") else None)
    pnl = p.get("pnl")
    outcome = ("Profit" if dec(pnl) > 0 else "Loss" if dec(pnl) < 0 else "Break-even") if pnl is not None and p.get("status") == "CLOSED" else p.get("status", "No filled trade")
    timeline = events + [dict(kind="telegram", **m) for m in messages]
    timeline.sort(key=lambda e: (stamp(e["at"]), e["kind"], e.get("seq", 0)))
    notes = []
    if not messages:
        notes.append("Telegram text was not retained for this signal.")
    if any(e["event"] == "ENTRY_LIMIT_REJECTED" and not e["body"].get("blockers") for e in own):
        notes.append("This older rejection did not record which limit triggered. Current settings cannot prove its historical cause.")
    notes.append("Telegram entries are linked by message/reply IDs, not by inferred price similarity. Unlinked posts and media content are not included.")
    name = buys[0]["body"].get("channel_name") if buys else None
    configured = channels.get(channel, {}).get("name")
    return dict(signal_id=signal_id, channel_id=channel, channel_name=name or configured or channel,
                channel_name_source="recorded at entry" if name else "current settings" if configured else "channel ID",
                symbol=p.get("contract", {}).get("symbol", "Unfilled signal"), status=p.get("status", "Not entered"),
                outcome=outcome, entry_price=p.get("entry_fill"), entry_time=p.get("entry_time"),
                initial_stop=initial_stop, current_stop=p.get("stop"), targets=p.get("targets", []),
                lots=p.get("lots", 0), remaining=p.get("remaining", 0), realized_pnl=pnl,
                exit_price=exits[-1]["body"].get("price") if exits else None,
                exit_reason=(exits[-1]["body"].get("reason") or "TARGET " + str(exits[-1]["body"].get("target"))) if exits else None,
                notes=notes, timeline=timeline[offset:offset+100],
                next_offset=offset+100 if len(timeline)>offset+100 else None)

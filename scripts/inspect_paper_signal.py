"""Read one paper signal's saved state and audit decisions without mutating it."""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3


def quote_day(db, position, day):
    """Stream matching retained receipts, including observations the engine may reject."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_events'").fetchone():
        print("No source_events table in this snapshot")
        return
    start = datetime.combine(day, datetime.min.time(), timezone(timedelta(hours=5, minutes=30)))
    contract = position["contract"]
    rows = db.execute("""
        SELECT received_at, body, EXISTS(SELECT 1 FROM seen WHERE seen.id=source_events.id)
        FROM source_events
        WHERE json_extract(body,'$.type')='quote'
          AND json_extract(body,'$.segment')=? AND CAST(json_extract(body,'$.token') AS TEXT)=?
          AND julianday(received_at)>=julianday(?) AND julianday(received_at)<julianday(?)
        ORDER BY julianday(received_at), rowid
    """, (contract["segment"], str(contract["token"]), start.isoformat(),
          (start + timedelta(days=1)).isoformat()))
    count = 0
    for received, body, processed in rows:
        quote = json.loads(body)
        fields = {k: quote.get(k) for k in ("source_time", "bid", "ask", "ltp", "market_open",
                                              "market_status_code", "market_status_observed_at")}
        print("Quote receipt:", json.dumps(dict(received_at=received, engine_seen=bool(processed), **fields)))
        count += 1
    print("Matching retained quote receipts:", count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--signal", required=True, help="Full channel_id:message_id")
    parser.add_argument("--quote-day", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="Show every retained same-contract quote received on this IST date and all signal decisions")
    args = parser.parse_args()
    with sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        state = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
        position = state.get("positions", {}).get(args.signal)
        print("Saved position:", json.dumps(position, indent=2))
        if args.quote_day:
            if not position:
                parser.exit(1, "Signal has no saved position in this snapshot\n")
            quote_day(db, position, args.quote_day)
        counts = Counter()
        # Bound repeated quote-related events while retaining each decision type.
        for seq, at, event, body in db.execute("SELECT seq,at,event,body FROM audit ORDER BY seq"):
            data = json.loads(body)
            if data.get("signal_id") != args.signal:
                continue
            counts[event] += 1
            if args.quote_day or counts[event] <= 3:
                print(seq, at, event, body)
        print("Event counts:", json.dumps(dict(counts), sort_keys=True))


if __name__ == "__main__":
    main()

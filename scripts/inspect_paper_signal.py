"""Read one paper signal's saved state and audit decisions without mutating it."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--signal", required=True, help="Full channel_id:message_id")
    args = parser.parse_args()
    with sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        state = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
        position = state.get("positions", {}).get(args.signal)
        print("Saved position:", json.dumps(position, indent=2))
        counts = Counter()
        # Bound repeated quote-related events while retaining each decision type.
        for seq, at, event, body in db.execute("SELECT seq,at,event,body FROM audit ORDER BY seq"):
            data = json.loads(body)
            if data.get("signal_id") != args.signal:
                continue
            counts[event] += 1
            if counts[event] <= 3:
                print(seq, at, event, body)
        print("Event counts:", json.dumps(dict(counts), sort_keys=True))


if __name__ == "__main__":
    main()

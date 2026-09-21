"""Validate an explicitly normalized broker shortlist; never guess token or multiplier."""

import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
from orion.core import IST, dec, resolve, stamp

p = argparse.ArgumentParser()
p.add_argument("--csv", required=True, help="Verified normalized CSV; see docs/contracts.md")
p.add_argument("--source", required=True, help="Original current broker export, for SHA256 provenance")
p.add_argument("--output", required=True)
a = p.parse_args()
now = datetime.now(IST)
with open(a.csv, newline="") as f:
    contracts = list(csv.DictReader(f))
if not contracts or len(contracts) > 200_000:
    raise SystemExit("Supply 1–200,000 verified option contracts")
master = {
    "synthetic": False,
    "as_of": now.date().isoformat(),
    "verified_at": now.isoformat(),
    "source_sha256": hashlib.sha256(Path(a.source).read_bytes()).hexdigest(),
    "contracts": contracts,
}
seen = set()
identities = set()
for c in contracts:
    c["order_quantity_per_lot"] = int(c["order_quantity_per_lot"])
    if c["product"] not in (
        "NIFTY",
        "BANKNIFTY",
        "GOLDM",
        "GOLD",
        "SILVERM",
        "SILVER",
        "CRUDEOIL",
        "CRUDEOILM",
    ):
        raise SystemExit("Unsupported exact product")
    if c["option_type"] not in ("CE", "PE") or dec(c["strike"]) <= 0:
        raise SystemExit("Invalid option")
    if not c["token"] or not c["symbol"]:
        raise SystemExit("Missing broker ID")
    k = (c["segment"], c["token"])
    if k in seen:
        raise SystemExit("Duplicate token")
    seen.add(k)
    identity = (c["segment"], c["product"], dec(c["strike"]), c["option_type"], c["expiry"])
    if identity in identities:
        raise SystemExit("Ambiguous contract identity")
    identities.add(identity)
    if stamp(c["expiry_at"]) <= now:
        raise SystemExit("Expired contract")
    resolve(c, {**master, "contracts": [c]}, now)
Path(a.output).write_text(json.dumps(master, indent=2) + "\n")
print(
    f"Wrote {len(contracts)} contracts. Schema checked; broker economics remain your verification responsibility."
)

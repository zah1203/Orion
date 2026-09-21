"""Build a full-expiry catalogue from fresh exports and verified economics."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from orion.catalogue import normalize
from orion.core import IST

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--nse", type=Path)
p.add_argument("--mcx", type=Path)
p.add_argument("--economics", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
os.umask(0o077)
paths = {k: v for k, v in [("nse_fo", a.nse), ("mcx_fo", a.mcx)] if v}
master = normalize(paths, json.loads(a.economics.read_text()), datetime.now(IST))
# Atomic replacement only after all rows pass. Never leave a partial master.
tmp = a.output.with_suffix(a.output.suffix + ".tmp")
with tmp.open("x") as f:
    json.dump(master, f)
os.replace(tmp, a.output)
print("Wrote", len(master["contracts"]), "contracts across all available expiries")

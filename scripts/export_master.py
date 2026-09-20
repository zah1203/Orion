"""Download original broker CSV without guessing its column schema."""

import argparse
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from neo_api_client import NeoAPI
from orion.runtime import credentials

p = argparse.ArgumentParser()
p.add_argument("--segment", choices=["nse_fo", "mcx_fo"], required=True)
p.add_argument("--output", required=True)
a = p.parse_args()
client = NeoAPI(consumer_key=credentials()["kotak_consumer_key"], environment="prod")
url = client.scrip_master(exchange_segment=a.segment)
if not isinstance(url, str) or urlparse(url).scheme != "https":
    raise SystemExit("Broker did not return a CSV HTTPS URL; inspect SDK response privately")
with urlopen(url, timeout=60) as response:
    content = response.read(50_000_001)
if len(content) > 50_000_000:
    raise SystemExit("Unexpected oversized master")
Path(a.output).write_bytes(content)
print("Saved broker export. Normalize a shortlist using docs/contracts.md.")

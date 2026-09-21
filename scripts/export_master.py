"""Download original Kotak CSV using a portal account or legacy AWS secret."""

import argparse
import contextlib
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from orion.core import IST

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--segment", choices=["nse_fo", "mcx_fo"], required=True)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--username", help="Use this portal account instead of the legacy AWS secret")
a = p.parse_args()
os.umask(0o077)
os.environ["NEO_LOG_FILE_ENABLED"] = "false"
logging.disable(logging.CRITICAL)
try:
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        from neo_api_client import NeoAPI

        if a.username:
            from orion.portal.store import Store

            store = Store(
                os.environ["ORION_PORTAL_DATA"],
                Path(os.environ["ORION_PORTAL_KEY_FILE"]).read_bytes().strip(),
            )
            creds = store.credentials(store.by_username(a.username))
        else:
            from orion.runtime import credentials

            creds = credentials()
        client = NeoAPI(consumer_key=creds["kotak_consumer_key"], environment="prod")
        url = client.scrip_master(exchange_segment=a.segment)
    if not isinstance(url, str) or urlparse(url).scheme != "https":
        raise ValueError("Missing HTTPS URL")
    started = datetime.now(IST)
    with urlopen(url, timeout=30) as response:
        if urlparse(response.url).scheme != "https":
            raise ValueError("Insecure redirect")
        content = response.read(50_000_001)
    if len(content) > 50_000_000 or b"pSymbol" not in content.splitlines()[0]:
        raise ValueError("Invalid CSV export")
    if started.date() != datetime.now(IST).date():
        raise ValueError("Day changed during export")
    a.output.write_bytes(content)
    Path(str(a.output) + ".receipt.json").write_text(
        json.dumps(
            {
                "as_of": started.date().isoformat(),
                "downloaded_at": started.isoformat(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "segment": a.segment,
            }
        )
    )
except Exception as exc:
    raise SystemExit("Export failed: " + type(exc).__name__) from None
print("Saved broker export and dated checksum receipt:", a.output)

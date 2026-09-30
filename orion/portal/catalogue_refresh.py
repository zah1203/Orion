"""Daily broker export validation. No login prompts or order placement."""

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from ..catalogue import normalize, validate_profile_date
from ..core import IST


def clock():
    return datetime.now(IST)


def download(segment, output, username):
    # The SDK redirects output and modifies logging; isolate it from the worker.
    # Credentials stay in the encrypted store, never in command arguments/output.
    subprocess.run(
        [sys.executable, "-m", "scripts.export_master", "--segment", segment,
         "--output", str(output), "--username", username],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=90,
    )


def refresh(path, username):
    """Run outside the event loop. A shared lease protects all account workers.

    Retain the last approved economics and its original verification date.
    Today's exports must still pass every lot/unit/expiry/checksum check.
    A failed attempt never replaces the existing catalogue.
    """
    path = Path(path)
    with path.with_suffix(path.suffix + ".refresh.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"catalogue_refresh": "Another worker is refreshing the catalogue"}
        now = clock()
        try:
            previous = json.loads(path.read_text())
        except FileNotFoundError:
            previous = {}
        if (previous.get("as_of") == now.date().isoformat()
                and previous.get("synthetic") is False and previous.get("contracts")):
            return {"catalogue_refresh": "Current", "catalogue_refreshed_at": previous.get("verified_at")}
        try:
            profile = previous.get("economics")
            if not profile:
                profile = json.loads((path.parent / "economics.json").read_text())
            validate_profile_date(profile, now, reuse_approved=True)
            if not profile.get("products"):
                raise ValueError("Missing products")
        except (OSError, ValueError, TypeError, AttributeError):
            return {"catalogue_refresh": "Operator review required: approved economics profile missing or invalid"}
        # Temporary files and atomic rename are on the target filesystem.
        with tempfile.TemporaryDirectory(prefix="catalogue-refresh-", dir=path.parent) as temp:
            paths = {}
            try:
                for segment in ("nse_fo", "mcx_fo"):
                    output = Path(temp) / (segment + ".csv")
                    download(segment, output, username)
                    paths[segment] = output
            except (OSError, subprocess.SubprocessError):
                return {"catalogue_refresh": "Broker export failed; retrying in 5 minutes"}
            try:
                master = normalize(paths, profile, clock(), reuse_approved=True)
                if now.date() != clock().date():
                    raise ValueError("Date changed during refresh")
            except (ValueError, KeyError, TypeError, ArithmeticError, OSError):
                return {"catalogue_refresh": "Validation failed: operator review of exports and approved economics required; retrying in 5 minutes"}
            target = Path(temp) / "contracts.json"
            with target.open("x") as output:
                os.chmod(target, 0o600)
                json.dump(master, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(target, path)
        return {"catalogue_refresh": "Current", "catalogue_refreshed_at": master["verified_at"]}

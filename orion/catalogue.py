"""Normalize original Kotak exports using explicit, dated economics verification.

Expiry date decoding follows Kotak's scrip_search.py. Encoded timestamps are NOT
exchange closing times. Expiry cutoffs and premium unit conversions require a
separate verified profile; never infer commodity economics from lMultiplier.
"""

import csv
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .core import IST, resolve

PRODUCTS = {"NIFTY", "BANKNIFTY", "GOLD", "GOLDM", "SILVER", "SILVERM", "CRUDEOIL", "CRUDEOILM"}


def expiry_date(raw, segment):
    offset = 315511200 if segment == "nse_fo" else 0
    return datetime.fromtimestamp(int(raw) + offset, timezone.utc).date()


def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("Expected finite positive contract economics")
    return number


def normalize(paths, profile, now):
    today = now.astimezone(IST).date().isoformat()
    if profile.get("verified_on") != today or not profile.get("source"):
        raise ValueError("Economics profile needs today's verification date and source reference")
    rules = profile.get("products", {})
    if not rules or set(rules) - PRODUCTS:
        raise ValueError("Select supported products in the economics profile")
    contracts, seen, identities, sources, found = [], set(), set(), {}, set()
    for segment, path in paths.items():
        if segment not in ("nse_fo", "mcx_fo"):
            raise ValueError("Unsupported segment")
        content = Path(path).read_bytes()
        sources[segment] = hashlib.sha256(content).hexdigest()
        # Export receipt, not its filesystem mtime, binds the raw export to this day.
        receipt = json.loads(Path(str(path) + ".receipt.json").read_text())
        if receipt.get("as_of") != today or receipt.get("sha256") != sources[segment]:
            raise ValueError("Fresh export receipt missing, stale or mismatched; export again")
        reader = csv.DictReader(content.decode("utf-8-sig").splitlines())
        for raw in reader:
            row = {k.strip().rstrip(";"): v.strip() for k, v in raw.items() if k and isinstance(v, str)}
            product = row.get("pSymbolName", "").upper()
            option = row.get("pOptionType", "").upper()
            if product not in rules or option not in ("CE", "PE"):
                continue
            expected = "nse_fo" if product in ("NIFTY", "BANKNIFTY") else "mcx_fo"
            if segment != expected or row["pExchSeg"] != segment:
                raise ValueError("Contract segment mismatch")
            expiry = expiry_date(row["pExpiryDate"], segment)
            if expiry < now.astimezone(IST).date():
                continue
            rule = rules[product]
            cutoff = time.fromisoformat(rule["expiry_time_ist"])
            if cutoff.tzinfo is not None:
                raise ValueError("Expiry time must be a local IST clock time")
            expires = datetime.combine(expiry, cutoff, IST)
            if expires <= now:
                continue
            lot = positive(row["lLotSize"])
            if lot != int(lot) or int(lot) not in rule["verified_lot_sizes"]:
                raise ValueError("Unverified lot size for " + product)
            if row.get("iLotSize") and Decimal(row["iLotSize"]) != lot:
                raise ValueError("Conflicting lot size fields")
            # Both submitted export samples and the official SDK use paise-scaled strikes.
            strike = positive(row["dStrikePrice"]) / 100
            tick = positive(row["dTickSize"]) / 100
            if row["lPrecision"] != "2":
                raise ValueError("Unsupported quote precision")
            if (
                row.get("pPriceUnits", "") != rule["price_units"]
                or row.get("pDeliveryUnits", "") != rule["delivery_units"]
            ):
                raise ValueError("Unverified commodity unit change")
            c = dict(
                product=product,
                strike=str(strike),
                option_type=option,
                segment=segment,
                expiry=expiry.isoformat(),
                expiry_at=expires.isoformat(),
                symbol=row["pTrdSymbol"],
                token=row["pSymbol"],
                order_quantity_per_lot=int(lot),
                tick_size=str(tick),
                premium_multiplier=str(lot * positive(rule["premium_rupees_per_quantity_point"])),
            )
            if not c["token"] or not c["symbol"]:
                raise ValueError("Missing broker identifiers")
            token_key = (segment, c["token"])
            identity = (segment, product, strike, option, c["expiry"])
            if token_key in seen or identity in identities:
                raise ValueError("Duplicate token or ambiguous contract identity")
            seen.add(token_key)
            identities.add(identity)
            contracts.append(c)
            found.add(product)
            if len(contracts) > 200_000:
                raise ValueError("Catalogue exceeds 200,000 contracts")
    if found != set(rules):
        raise ValueError("No unexpired options found for: " + ", ".join(sorted(set(rules) - found)))
    master = dict(
        synthetic=False,
        as_of=today,
        verified_at=now.isoformat(),
        source_sha256=hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
        sources=sources,
        economics=profile,
        contracts=contracts,
    )
    for c in contracts:
        resolve(c, {**master, "contracts": [c]}, now)
    return master

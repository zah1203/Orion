# Contract selection and sizing

The packaged instrument file is **synthetic**: its token and multiplier are deliberately fictitious. It is only accepted by replay. Do not replace its date and use it with a real feed.

The legacy workflow below takes a verified normalized file; the full-catalogue workflow later in this document imports the broker exports directly. Obtain today's original Kotak master using `scripts/export_master.py` (requires `ORION_SECRET_ARN` and AWS credentials). The CSV column mapping must be checked against the actual authenticated export; that export was not available during this build. No unverified raw-column mapping has been hardcoded.

```bash
python scripts/export_master.py --segment nse_fo --output /tmp/nse-master.csv
python scripts/export_master.py --segment mcx_fo --output /tmp/mcx-master.csv
```

Create a normalized CSV with exactly these headers and your verified values:

```csv
product,strike,option_type,segment,expiry,expiry_at,symbol,token,order_quantity_per_lot,premium_multiplier,tick_size
```

| Field | Meaning |
|---|---|
| product | Exact product, e.g. GOLDM, never silently replace with GOLD |
| strike | Option strike, not entry premium or underlying price |
| option_type | CE or PE |
| segment | nse_fo or mcx_fo |
| expiry | Actual option expiry, YYYY-MM-DD |
| expiry_at | Actual expiry cutoff with timezone, e.g. ISO timestamp ending +05:30; verify exchange/broker timing |
| symbol / token | Exact broker identifiers for this option |
| order_quantity_per_lot | Broker quantity units required for one whole lot |
| premium_multiplier | Rupees of premium cost for a one-point premium change in one whole lot |
| tick_size | Option premium tick in quoted price units |

**Do not assume premium_multiplier equals order_quantity_per_lot for commodities.** Verify both against broker contract details, premium/margin preview and exchange contract specifications. For this long-option simulator, premium cost = fill premium × premium_multiplier × lots. Risk-to-stop = (fill premium − stop) × premium_multiplier × lots, plus fee allowance. These formulas are accounting calculations, not a claim about any product's current lot size. Gap losses can exceed stop risk.

Combine verified selected rows from both exports; retain originals privately. Generate the daily master (source may be an archive of both original exports):

```bash
python scripts/prepare_contracts.py --csv /tmp/verified-shortlist.csv --source /tmp/original-exports.zip --output /tmp/contracts.json
```

The converter validates schema and unique matching, not the truth of manually entered economics. It stamps today's IST date and source hash. Refresh against a fresh export each trading day. Include all relevant expiries in your shortlist so nearest-expiry selection has the correct candidates. Nearest means nearest **in this verified shortlist**; an omitted nearer contract cannot be detected from it.

The resolver matches exchange segment + exact product + exact strike + CE/PE. Explicit signal expiry must match exactly. With no expiry, it selects the nearest unexpired option expiry. Same-day expiry is eligible until the configured expiry buffer; no hardcoded weekly calendar. No exact match or multiple matches → review, no simulated order. A signal outside the loaded catalogue is recorded for review. Runtime dynamically subscribes to pending/open contracts; daily master refresh remains operator-managed.

The SDK's feed timestamp epoch and seconds/milliseconds setting must be checked with actual ticks. Unknown or stale epochs fail closed. Trading sessions come from the feed status; cutoffs in paper.json are conservative configurable defaults, not an exchange holiday/session calendar.

## Full catalogue workflow (portal accounts)

The new importer retains **all unexpired options and all expiries** for the products
in an explicitly verified economics profile. It does not select the first 100 rows.
The older normalized-CSV converter now accepts up to 200,000 verified rows as well.

Use the deployed release as your working directory so source and installed code
cannot diverge. Run these commands as `orion`, with `ORION_PORTAL_KEY_FILE` and
`ORION_PORTAL_DATA` set to the existing portal paths. No secrets belong in argv:

```bash
cd /opt/orion/current
.venv/bin/python scripts/export_master.py --username jadmin --segment nse_fo --output /var/lib/orion/portal/broker-exports/nse_fo.csv
.venv/bin/python scripts/export_master.py --username jadmin --segment mcx_fo --output /var/lib/orion/portal/broker-exports/mcx_fo.csv
```

The exporter creates a dated `.receipt.json` beside each CSV. Refresh each trading
day; old manually downloaded files need re-exporting, not a changed date. Receipts
prove which downloaded bytes were used, not the accuracy/freshness of broker data.

Prepare a private `economics.json` with this structure. This is a **schema example,
not approved trading economics**; replace every placeholder with values verified
from the current broker/exchange specifications. Add an entry for every enabled
product. Leave unsupported/unverified products disabled in the portal.

```json
{
  "verified_on": "YYYY-MM-DD",
  "source": "References and date of broker/exchange verification",
  "products": {
    "NIFTY": {
      "verified_lot_sizes": [],
      "premium_rupees_per_quantity_point": "VERIFY",
      "price_units": "",
      "delivery_units": "",
      "expiry_time_ist": "HH:MM:SS"
    }
  }
}
```

`verified_lot_sizes` allows explicitly verified lot-size transitions across expiries.
The importer uses the actual row's `lLotSize` as the order quantity, and multiplies
it by `premium_rupees_per_quantity_point` to calculate rupees per premium point per
lot. Verify that the broker's quantity units really correspond to this interpretation.
For commodities, check quotation units, delivery units, premium preview and lot
quantity together. Never copy `lMultiplier=-1` or assume GOLD and GOLDM use the same
conversion. A profile is an operator attestation, not automatic broker verification.

Expiry **dates** follow Kotak's `services/scrip_search.py`: NSE raw seconds plus
315511200; MCX raw seconds with no offset, extracting the UTC calendar date in both
cases. These encoded times are not exchange closing times. The profile supplies
an independently verified IST expiry cutoff. Strikes and ticks are divided by 100;
unsupported precision, unknown lot sizes, unit changes, duplicate identities and
stale receipts reject the import. Same-day contracts are retained only before cutoff.

```bash
.venv/bin/python scripts/import_catalogue.py --nse /var/lib/orion/portal/broker-exports/nse_fo.csv --mcx /var/lib/orion/portal/broker-exports/mcx_fo.csv --economics /var/lib/orion/portal/economics.json --output /var/lib/orion/portal/contracts.json
.venv/bin/python -m orion.portal worker --username jadmin --master /var/lib/orion/portal/contracts.json
```

The worker prompts privately for a fresh TOTP. Portal verification does not retain
its broker authentication session or start the worker. This remains an operator-run
foreground process, not a new background service or browser start button.

An explicit signal expiry must match exactly. Only missing expiry selects nearest
valid expiry for the exact product, strike and CE/PE. No missing explicit expiry is
silently substituted. Runtime subscribes to unique PENDING/OPEN contracts only,
restores open-position subscriptions on restart, and removes subscriptions after
closure/cancellation. A 100-active-contract guard is separate from catalogue size;
exceeding it stops the worker for review rather than silently ignoring positions.
Subscription failures also stop the task group. Pending signals still require fresh
quotes and the existing crossing/age rules; no historical target hits are inferred.

These changes require an application deployment, not Terraform changes. Setup docs
are now packaged with releases. Keep entries paused through deployment and verify
worker/feed status and quote units before enabling paper entries. No real orders.

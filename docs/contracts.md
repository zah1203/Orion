# Contract selection and sizing

The packaged instrument file is **synthetic**: its token and multiplier are deliberately fictitious. It is only accepted by replay. Do not replace its date and use it with a real feed.

The live-data connector takes a small, verified broker shortlist, not the whole exchange master. Obtain today's original Kotak master using `scripts/export_master.py` (requires `ORION_SECRET_ARN` and AWS credentials). The CSV column mapping must be checked against the actual authenticated export; that export was not available during this build. No unverified raw-column mapping has been hardcoded.

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

The resolver matches exchange segment + exact product + exact strike + CE/PE. Explicit signal expiry must match exactly. With no expiry, it selects the nearest unexpired option expiry. Same-day expiry is eligible until the configured expiry buffer; no hardcoded weekly calendar. No exact match or multiple matches → review, no simulated order. Runtime subscribes only to this shortlist. A signal outside it is recorded for review; automatic subscription/master refresh is future work.

The SDK's feed timestamp epoch and seconds/milliseconds setting must be checked with actual ticks. Unknown or stale epochs fail closed. Trading sessions come from the feed status; cutoffs in paper.json are conservative configurable defaults, not an exchange holiday/session calendar.

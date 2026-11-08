# MCX Live foundation: blocked pending broker verification

This local implementation adds commodity-aware admission, unit arithmetic,
reservations, cash debits, quote identity and protective-order plumbing. It does
not enable a product, change the production release gate, or establish that MCX
Live is ready. MCX BUY dispatch and broker funding reads remain explicitly blocked
pending broker/account acceptance. Test values are synthetic, not approved
commodity economics. No broker connection or order was used in validation.

No product is selected automatically. Only explicitly selected commodity channel
products are admitted; standard and mini contracts remain distinct. Current scope
is explicit-expiry BUY-range signals. Buy-above, overnight and expiry-day entries
remain rejected. Other known commodity names share validation infrastructure but
have not been declared broker-accepted products.

## Contract-unit boundary

The catalogue's `premium_multiplier` means rupees per one quoted premium point
per whole lot. `order_quantity_per_lot` means broker order units per lot. Existing
Live accounting instead binds rupees per premium point per broker quantity unit.
Use the quotient only when exact and supported; never bind the catalogue value
directly. The helper validates positive finite bounded numbers and exact decimal
conversion, tick alignment, integer lots, and long-stop direction. Admission binds
this information to a dated catalogue/source digest and explicit economics review.
An evidence digest detects changed data; it does not authenticate its truth or
replace broker acceptance. No symbol/token/product defaults are provided. Stop risk excludes fees and gap/slippage losses.

## Official evidence checked 2026-10-16

- [Kotak instruments documentation](https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/market-data-apis/instruments.md): fetch current daily master links; quantity must be a multiple of `lLotSize`; expiry documentation now names `lExpiryDate`, with MCX epoch decoded without NSE's offset. Existing importer reads `pExpiryDate`. Resolve against an actual fresh export, refusing conflicting fields, before changing mapping.
- [Kotak trading API documentation](https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/trading-apis.md): placement lists `mcx_fo`; BO/CO discontinued from April 1, 2026. Modify field list inconsistently omits MCX. Generic support is not proof an MCX option/product/order-type combination is accepted.
- [MCX CRUDEOIL product page](https://www.mcxindia.com/products/energy/crude-oil), March 2026 onwards options specification: option premium tick 0.10, underlying 100-barrel futures contract; option expires two business days before underlying futures; trading close depends on US DST. ITM options can devolve into futures. These exchange facts do not establish Kotak's order quantity mapping.
- [Kotak devolvement guidance](https://www.kotakneo.com/support/what-is-devolvement-of-commodity-options/): close expiring options before 22:00 IST; broker may liquidate NRML long options after that time. Do not substitute exchange session close for broker risk cutoff or rely on RMS to guarantee exit.
- [Kotak brokerage calculator](https://www.kotakneo.com/calculator/brokerage-calculator/): exact charges require account order-page estimates. Do not reuse paper's synthetic fee or a generic plan headline.

## Acceptance sequence and remaining gates

1. Confirm exact user-selected products including standard versus mini; no alias substitution. Obtain today's original MCX export and dated receipt through the existing authorized read-only exporter. Verify token, symbol, segment, option type, strike, option expiry, lot quantity, quote precision, tick and price/delivery units. Independently reconcile rupees-per-point against exchange specification and broker premium preview. No `lMultiplier` guessing.
2. The separate commodity admission path requires fresh explicit economics, source provenance, selected exact products and no expiry-day admission. Broker-specific exit timing, holiday/DST/devolvement acceptance still needs completion before release. Saved protection economics remain usable after admission day; a new catalogue cannot rewrite an open position.
3. Order/quote/market/session plumbing now binds segment explicitly; quote keys are (segment, token). MCX margin/funding reads remain refused rather than silently using NSE RMS semantics. Reject mismatched quote segment/token. Prove MCX NRML/MIS and limit/SL support with official broker confirmation or a permitted non-submitting preview. No real validation orders.
4. Immutable contract economics now flow through candidate, source-derived reservation, entry plan, fill accounting and cash debit/risk calculations. Premium and stop-risk use per-lot money; submitted and cumulative fill quantities use broker units. Verify partial fills and target allocations on lot grids, rounding down quantities and never silently rounding money.
5. MCX sale-credit reuse remains refused until settlement semantics are verified. Check account MCX entitlement, current usable cash/margin, portfolio liabilities and account-specific fee estimates in the same segment. Reject stale/unavailable/ambiguous broker responses. Funds reservation must cover the intended fee and slippage policy, including multiple target exits.
6. Protective stop and target manager: independent orders, not discontinued BO/CO. Preserve total broker quantity on modifications; tick-scaled stop-limit gap; broker acknowledgment before reporting protection; reconcile fills racing modify/cancel; prevent sell quantity exceeding long exposure; restart/timeout/unknown state fail closed. Avoid an unprotected cancel-and-recreate window.
7. Tests: exact segment/token across placement, quote, margin and account cash; crude/gold/silver standard versus mini rejection; stale/ambiguous catalogue; tick changes and fractional lot rejection; per-lot versus per-unit scaling; partial fill/P&L/fees; stop/target resize races; MCX expiry timezone/DST/holiday/cutoff; unsupported order/product; no activation after import or restart. Run full lint/compile/tests after integration.

## Still missing

No authenticated current master or account preview was available in this task.
No broker order was placed or modified. MCX entitlements, actual quote units,
precise account fees, cash/RMS semantics and MCX modify-stop acceptance remain
unverified. Do not treat passing offline tests as approval for real trading.

# Staged Live rollout

## Current status

Stage 1 provides the **offline development foundation**; Stage 2 adds an
owner-triggered read-only broker probe; Stage 3 adds an offline protective-exit
protocol; Stage 4 adds offline broker-book comparisons; Stage 5 adds fill history
and daily accounting evaluation. Merging or deploying this PR
cannot submit broker orders. The production worker still opens paper.db, the
Paper/Live API rejects Live, and no runtime imports the new ledger. There is no
production enable flag, order-submission transport, executable live strategy, or order API.
The pending plain-ORION header change is independent of Live availability.

GET /api/live/readiness is authenticated and owner-only. It returns explicit
blockers and always reports live_available=false and order_submission_available=false.
The readiness GET does not contact Kotak or create files. A readiness response is not approval
to trade and does not certify broker connectivity.

## Audit findings

- orion/core.py fills simulated orders. Reusing its fills as broker confirmations
  would misstate balances and expose real positions to unmanaged risk.
- orion/portal/__main__.py always selects the per-account paper.db; the supervisor
  is a paper-worker supervisor. Mode selection must not repurpose that database.
- broker_session.py persists a deliberately restricted market-data session.
  Trading authentication/session fields need their own validated design and
  credential-binding tests; feed authentication does not demonstrate order access.
- The pinned kotakneoapi dependency is 3.0.7. Its installed place_order signature
  accepts tag, and the broker SDK documentation describes tag-based tracking.
  A tag is a correlation identifier, not a guaranteed idempotency key.
- Existing recovery inventories do not yet support a live ledger. Creating a
  live.db within current account storage will fail backup inventory checks. Do
  not do this in production until backup/restore support is implemented and tested.
- Broker static-egress/account API approval must be checked before rollout. Do
  not infer eligibility or order access from the public HTTPS gateway or feed.

Primary references reviewed 2026-10-10:
- https://github.com/Kotak-Neo/kotak-neo-python
- https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/MIGRATION.md
- https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/trading-apis.md

## Implemented offline contracts

orion/live/ledger.py accepts a separate private live.db and binds it to one account.
It rejects paper.db, symlink paths and isolated recovery destinations. Use only
synthetic test data and temporary development directories in this stage.

Entries start paused. The development_resume method is only for the offline test
harness; there is no portal, command-line or worker integration invoking it.
The ledger reserves the full long-option entry premium using exact decimals and
lot multiples. Fresh quote/signal timestamps, explicit tick size, entry count,
lot and loss limits are checked. The development caller supplies these values;
they are not yet broker-verified. Fees and exit losses are not accounted here.
The entry-count and premium caps are deliberately lifetime limits in this stage,
not a completed daily-risk implementation. No automatic daily reset exists.

A unique account/event intent prevents duplicate local reservation; a changed
payload under the same event is rejected. Before a future transport call, the
intent transitions durably from PREPARED to DISPATCHING. A crash leaves that
state blocking fresh entries and resubmission. An ambiguous result transitions
to UNKNOWN and pauses entries. Absence from a broker response never resolves it.
Matching observations bind the account, tag, symbol, quantity and unique broker
order ID. Fill quantities cannot decrease, terminal outcomes cannot mutate,
and partial cancellations retain a conservative full premium reservation.
Pause blocks prepared dispatches but permits reconciliation of existing orders.
No method in this module places, modifies, cancels or closes a broker order.

## Remaining stages, in order

1. **Read-only broker validation:** implement short-lived account-bound trading
   authentication without persisting TOTP codes/seeds. Validate response schemas,
   authenticated account identity, positions, orders, balances, session expiry and
   fixed outbound IP/API approval. Redact SDK output. Use fixtures first, then an
   explicitly approved owner read-only probe; no place/modify/cancel calls.
2. **Execution and reconciliation:** add broker response normalization, durable
   fill/trade identifiers, complete order/position reconciliation on startup and
   after reconnects, partial fills, rejected orders, rate limiting and ambiguous
   timeout handling. Recheck risk/freshness immediately before actual dispatch.
   Require fresh complete snapshots; unknown external positions/orders must block
   new entries. Never blindly retry placement after a network error. Account for
   fees, fills, realized/unrealized losses and exchange session boundaries.
3. **Protective exits:** implement and test broker-accepted protective orders,
   partial-fill protection, target/stop amendments, cancellations racing fills,
   disconnects and restart recovery. A software stop alone cannot guarantee a
   maximum loss. Stop-new-entries and liquidate-positions are distinct actions;
   liquidation must not be implied by pause or mode switching.
4. **Production integration:** implement separate paper/live ledgers, settings,
   balances, positions, worker leases and visible mode selection. Existing paper
   positions continue monitoring across view changes. Explicit pilot allowlisting and account-owner approval must
   be enforced server-side at each boundary, with explicit confirmation and an
   audit trail. Trading-affecting requests require CSRF/native authentication.
5. **Recovery and operations:** inventory and snapshot the live ledger; isolated
   restore must remain unable to trade. A restored ledger must reconcile with the
   broker before any order capability is available. Test monitoring, rollback,
   session expiry and stale data. Revalidate backups after schema changes.
6. **Supervised pilot:** only after all preceding gates pass, the human account
   owner chooses the instrument, quantity and monetary limits and independently
   approves/enables real trading. Minimum-size tests still incur real financial
   exposure. Code implementation approval is not order-placement authorization.

No AWS resources or production services were changed while developing Stage 1.
Local/CI tests use synthetic data and do not establish live broker readiness.


## Stage 2: owner-triggered read-only probe

POST /api/live/probe requires owner authorization, the normal CSRF/native checks,
a fresh six-digit `totp` and `confirmation: "READ ONLY CHECK"`. The API accepts
no credentials or account selector from the request: it uses only that owner's
saved Kotak credentials. The endpoint is rate-limited to three attempts under the
existing connection limiter. It acquires the account's broker-auth/worker leases
and refuses enabled entries or a running worker; it never stops or pauses one.
There is not yet a mobile UI button for this endpoint. Do not send TOTP codes,
MPINs or API tokens through GitHub workflow inputs, command arguments or chat.

A fresh subprocess authenticates with totp_login/totp_validate and invokes only
order_report, positions and limits. Full-session tokens remain in child memory;
the worker's encrypted feed session and credentials are not replaced. SDK output
is discarded and cache files are confined to private temporary storage. The
parent enforces a 60-second timeout, kills/reaps the child on timeout/cancellation
and validates the summary before returning it. A new broker login can affect
other broker sessions; run the first real check in a reviewed maintenance window.
No automated probe runs on startup, merge, readiness GET or backup activation.

The check validates the broker-returned UCC, trade-session marker, HTTPS broker
routing, error envelopes, numeric fields, order quantities/statuses and matching
account IDs on nonempty books. Empty successful lists are valid; missing data,
SDK error dictionaries, mismatched identities and unknown shapes fail closed.
The read window is limited to 20 seconds. It is not an atomic or continuously
fresh broker snapshot. Unknown future response variants require a reviewed parser
update rather than accepting them silently.

Only counts, a locally observed timestamp, identity-match status and RMS Net are
returned. RMS Net can be negative and is **not cash, approved capital, or a
validated live risk budget**. No positions, tokens, account numbers or raw SDK
errors are emitted. A successful probe still reports live_available=false.
Protective exits, order/position reconciliation and live risk/recovery gates remain.
The broker approval/static outbound IP check remains a separate operator gate;
read access does not prove that order placement is permitted.

Validation uses synthetic fixtures shaped after the pinned SDK and official
portfolio/order documentation; no real brokerage authentication or API read has
been performed by the implementation agent. Real response compatibility remains
unverified until an authorized owner runs the probe after deployment.

Stage 2 schema references:
- https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/orders/order_report.md
- https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/portfolio/positions.md
- https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/portfolio/limits.md

## Stage 3: offline protective-exit protocol

`orion/live/protection.py` is a synthetic, normalized-observation harness using
additional tables in the development ledger. No runtime imports it and it has
no SDK, HTTP, order placement, cancellation or liquidation implementation.
Production Live remains disabled. This stage does not complete the execution
and reconciliation gates listed above.

For a terminal entry with confirmed fills, the harness reserves the remaining
sell quantity for one protective exit. It refuses simultaneous stop/target exits,
repeat dispatches, unresolved replacements and quantities requiring lot rounding.
An entry still filling must first reach a confirmed terminal state; automatic
incremental protection of a working entry is **not implemented**. Trigger prices,
ticks, lot sizes and observations are supplied by tests, not broker verified.
A prepared exit does not establish broker-accepted protection.

Cancel requests persist as CANCEL_PENDING. Partial fills during cancellation
update cumulative fills without releasing the remaining sell capacity. Only an
explicit terminal observation permits a replacement for the remaining exposure.
A fill winning the cancel race leaves no replacement quantity. Crash recovery
retains DISPATCHING/UNKNOWN and never resends automatically. Observations check
account, instrument, segment, sell side, quantity, broker identity and monotonic
fills. Terminal contradictions roll back the observation, latch an incident and
block replacement dispatch, including exits already prepared before the conflict.

Rejection/cancellation with remaining exposure latches an unprotected-exposure
incident. A replacement may be modeled, but cannot automatically clear the entry
lock. Unknown outcomes and observation conflicts additionally block protective
dispatch pending review. Incidents survive reopen; there is deliberately no
production incident-clear endpoint. Pause never cancels or liquidates. This
conservative offline gate blocks new entries while any tracked filled exposure
remains, even when its modeled stop is open.

Tests use fabricated account/order data and cover cancellation races, partial
fills, rejected stops, contradictory late fills, lot remainders, concurrent
reservations and restart uncertainty. They neither demonstrate real broker
compatibility nor guarantee a stop execution price. Remaining requirements
include raw broker normalization, stable trade IDs, complete/fresh account
snapshots, external order/position detection, fees and daily risk accounting,
working-entry protection, product and stop-order validation, broker transport,
production controls and live-ledger backup support. No AWS operation, real broker
read/order, deployment or worker restart was performed for this stage.

## Stage 4: offline broker-book reconciliation

`orion/live/reconciliation.py` parses Kotak-shaped order/position fixtures and
compares them with the development ledger in one SQLite transaction. It makes
no network calls and is not connected to the probe endpoint or a worker.
The fixture parser uses documented raw fields (`nOrdNo`, `actId`, `exSeg`,
`prod`, `tok`, `trdSym`, quantities, prices and order state). Net positions use
carry-forward plus fresh buys minus sells; SDK-computed P&L and netQty are not
trusted. Unknown states, errors, duplicates and incomplete shapes fail closed.

The comparison checks the exact set of already-bound broker order IDs, order
side, product/token/instrument, quantity, cumulative fills and state. Entry
limit/average prices and protective trigger/type are also compared. Position
quantities are summed across local entry and exit fills. External orders,
unexpected shorts and position discrepancies block new entries. Missing orders
are never considered cancelled/rejected; ambiguous DISPATCHING/UNKNOWN orders
cannot be resolved by this comparator. CANCEL_PENDING remains unresolved until
an explicit observation updates the ledger. Comparison does not import orders,
update fills or reconcile individual trade IDs.

The development harness must bind a UCC to the ledger and supply immutable
product/token metadata per entry. These are fixture assertions, not verified
contract catalogue data. No tag-to-order binding is inferred. Before production,
a trusted adapter must bind metadata before submission, collect complete books,
verify account/session identity and resolve pagination/truncation. The supplied
`complete=True` flag and collection timestamps are **not proof** of completeness
or an atomic broker snapshot. Actual SDK response compatibility is unverified.

Collection must take at most 20 seconds and completion must be within 30 seconds.
After reconciliation is installed on a development ledger, resume, new reservation
and dispatch require a matching snapshot from that ledger connection. Any change
to intents, exits or instrument bindings invalidates it. Reopening the ledger
requires another comparison, including after a crash. Reserving an intent also
invalidates the snapshot, so bind its metadata and compare again before simulated
dispatch. A match does not resume entries or clear earlier incidents. A failed
comparison invalidates the previous snapshot, persists a redacted blocked audit
record, pauses entries and latches `broker-snapshot-mismatch`; this also blocks
protective replacement preparation/dispatch pending review.

This conservative stage requires **all locally bound order IDs** in the supplied
book, including historical terminal orders. A daily broker book that omits them
will block; multi-day history/trade reconciliation is still required. Other
remaining gates include fill IDs, working-entry protection, stop-limit price and
execution guarantees, fees/daily risk, production integration and live-ledger
backup/restore. A matched snapshot is not evidence of sufficient stop protection
or approved capital. There is no production incident-clear operation.

Validation is local/CI using synthetic fixtures; no brokerage login, real order,
AWS operation or production restart is part of Stage 4. Live remains disabled.

## Pilot target: October 19, 2026

The requested pilot scope is now **the admin account plus one explicitly approved
new account**. This replaces the planned owner-only pilot scope, but does not
change implemented authorization: the read-only probe remains owner-only and
Live remains unavailable for every account. October 19 is a target, not a timed
enablement or evidence of readiness. No account is automatically enrolled.

Before either account is enabled, the following work and evidence are required:

- Implement a server-side allowlist for these two account IDs, independent broker
  identity/credentials, balances, limits, worker leases and explicit approvals.
  Registering another account must not make it eligible for Live. No shared capital
  or copied admin limits. The new account ID and both accounts' final limits remain
  to be supplied and reviewed before activation.
- Complete broker transport and history reconciliation, working-entry protection,
  supported stop types, cancellation races, fees and atomic pre-dispatch risk checks.
- Validate read-only broker compatibility and fixed-egress/API eligibility for each
  account. Then run isolated execution/recovery scenarios with no real orders.
- Add live-ledger backups and demonstrate an isolated restore that cannot trade.
  Obtain deployment permission for the necessary service restart window.
- Each owner explicitly approves a supervised minimum-size real-money test only
  after the preceding gates pass. Monitor broker-accepted protection and reconcile
  actual fills before broadening operation. Real orders are not authorized by
  merging development PRs. If a gate fails, postpone activation.

## Stage 5: durable fill history and daily accounting

`orion/live/accounting.py` stores normalized fill fixtures in the separate live
ledger, keyed by exchange segment, IST trade date and trade ID. It binds the
broker UCC consistently with reconciliation and requires an existing confirmed
broker order binding. Account, side, symbol, segment, price, execution time and
cumulative quantity are checked. Exact replays are no-ops; conflicting replays
roll back, pause entries and latch an incident. Broker-ID reuse across trading
days is not yet supported; exchange trade IDs may recur on a different day.
Corrections, busts and later fee adjustments need a reviewed correction protocol;
they must not overwrite immutable history.

The summary verifies that individual fill quantities cover cumulative order fills
and that entry prices agree exactly with the stored average. Rounded broker
averages need a reviewed adapter before production use. Unresolved dispatch or
cancellation states block the summary. Execution timestamps determine accounting
days in Asia/Kolkata. FIFO cost attribution is per Orion entry, not tax accounting
across every position in the same instrument. Realized P&L is assigned to the
exit execution day and fees to each fill's day. Contract currency multipliers are
explicit and immutable; they are not assumed from quantity or symbol.

The conservative loss calculation is:

`max(0, -today_realized_PnL) + today_fees + open_unrealized_losses`

Realized/open gains do not offset those fee/open-loss components. Open losses use
acquisition cost, including carried positions, so midnight does not erase carried
losses. This is **not broker daily settlement MTM**. Open marks must be present,
finite, nonnegative and no older than 30 seconds. Closed positions need no mark.
The summary also reports acquisition premium still invested and counts entries
by the IST day of their first fill. It does not report available cash or prove
capital adequacy. Fill or multiplier changes invalidate a prior reconciliation.

`check_limits` is an **offline evaluation**, not an execution gate. It can pause
the development ledger when loss, filled-entry count or open-premium limits are
reached, with a persistent incident that blocks a simple manual resume. There is
no automatic midnight reset or incident-clear endpoint. It does not modify positions. Pending reservations,
unfilled/rejected attempts, candidate-order cost, reviewed daily resets, portfolio
stop risk and atomic pre-dispatch evaluation still need to be
combined into the future execution gate. Existing lifetime ledger limits remain.
Fees, marks, multipliers, timestamps and trade IDs are synthetic caller assertions;
actual broker adapters and fee reserves remain required. Production workers do not
import this module and no portal endpoint exposes its evaluator.

Tests exercise replay/restart, partial exits, fees, carried exposure, Indian day
boundaries, missing/conflicting fill data, marks and multipliers, and separate
admin/new-account ledgers using the same synthetic order/trade IDs. This validates
storage isolation, not a completed two-account authorization or worker rollout.
No AWS backup, broker call, deployment or actual-money operation was performed.

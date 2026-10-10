# Owner-only Live rollout

## Current status

Stage 1 is an **offline development foundation**. Merging or deploying this PR
cannot submit broker orders. The production worker still opens paper.db, the
Paper/Live API rejects Live, and no runtime imports the new ledger. There is no
production enable flag, SDK transport, executable live strategy, or order API.
The pending plain-ORION header change is independent of Live availability.

GET /api/live/readiness is authenticated and owner-only. It returns explicit
blockers and always reports live_available=false and order_submission_available=false.
It does not contact Kotak or create files. A readiness response is not approval
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
   positions continue monitoring across view changes. Owner-only approval must
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

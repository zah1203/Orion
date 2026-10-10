# Consolidated Live release — working draft

**Not approved for real-money deployment.** October 19, 2026 is the requested
pilot target for the owner plus one approved account. This branch collects the
remaining implementation in one PR; do not merge individual components as a
substitute for an integrated acceptance decision.

## What this draft implements

- Owner-managed pilot enrollment limited to the owner plus one approved account.
  Policies are independent, require explicit monetary/count limits, and require
  the selected user's review. Policy/credential changes invalidate review;
  suspension and withdrawal revoke enrollment. Duplicate broker UCCs are rejected.
- Account/Owner preparation screens with policy review, withdrawal and a deliberate
  read-only broker check. The second account may probe only after review and uses
  only its own saved credentials. No TOTP is retained in app state after submission.
  A worker lease prevents probing an active paper worker; the endpoint never stops it.
- An integrated deterministic simulator joining reservation, pre-dispatch risk,
  durable uncertain outcomes, fill journal, protective exits and reconciliation.
  Dispatch rechecks signal/quote age, daily attempt count, fee/stop-risk budgets,
  available capital and all prepared/unresolved premium reservations. Nested SQLite
  savepoints keep the decision and dispatch intent atomic before simulated I/O.
- A pinned Kotak 3.0.7 adapter with explicit NSE NRML limit-order mapping and
  strict acknowledgement validation. A private process holds the authenticated
  session; each call has a wall-clock deadline. Timeout kills/reaps the process
  and removes its temporary cache. Credentials are sent through an anonymous pipe,
  never command arguments, files or logs. HTTP redirects and retries are disabled.
- A durable command journal linking already-committed dispatches to that adapter.
  Placement/cancellation is one-shot across restart. An acknowledgement binds an
  order ID but never implies a fill or completed cancellation. Unknown responses
  pause the ledger and require review; order tags are not treated as idempotency keys.
- Atomic ingestion of account-bound current order/position books, including partial
  fills and fill/cancel races. Identity, quantity, entry limits and protective stop
  limits must match. A conflicting batch rolls back and leaves a durable incident.
  Additional confirmed entry lots can receive disjoint protective stops without
  cancelling existing protection; unknown stops and cancels retain sell capacity.
- A protection-monitor worker component combining those pieces: fresh book ingestion,
  one-shot cancellation of a partially filled entry's remainder, explicit stop-limit
  placement for confirmed lots, late-fill coverage and durable health counts. It
  never submits a BUY. The account wrapper acquires the existing worker lease before
  calling its session factory, so an active Paper worker cannot be displaced or
  subjected to a competing login. No production launcher constructs it yet.
- An atomic broker evidence path joining stable order/position books with the
  current-day trade report. The private SDK process discards unnecessary response
  fields, joins missing trade tokens only through account-bound order IDs, checks
  fill quantities/prices/times and rejects changed books. A missing or conflicting
  fill rolls back order transitions and command confirmations as well as fills.
  Replayed fills are idempotent. Broker charges remain explicitly unverified;
  accounting refuses to calculate a trading budget from placeholder zero fees.
  RMS Net is retained as a diagnostic only, never treated as spendable cash.
- Read-only account health endpoints and Account/Owner displays for last observed
  exposure, uncovered units, pending commands, stale checks and incident counts.
  Reads never create a ledger, authenticate to the broker, clear incidents or
  enable orders. Owners can inspect selected accounts; other users see only their
  own health. Missing, corrupt, misbound and recovered ledgers cannot look healthy.
- Append-only, account-bound fee corrections with opaque statement references,
  idempotent replay, time ordering and as-of accounting. Original fill evidence is
  unchanged. Broker statement import and verification of actual charges remain open.
- Sealed terminal-order history for daily broker-book rollover. Sealing requires
  fresh reconciled books, complete fill evidence, matching execution averages and
  fresh marks for carried exposure. Earlier-day evidence is revalidated against
  local order/fill/instrument facts before use; today's positions must still match.
  Missing same-day, working and unknown orders remain blockers. This is local
  retained evidence, not an implementation of a broker historical-download API.
- Daily IST entry counts and exposure-based premium/risk reservations. Confirmed
  closed exposure releases capacity; paid fees and realized loss remain in daily
  accounting. Unsent, working and unknown orders retain reservations across midnight.
- Online `live.db` backup through the SQLite backup API, including committed WAL
  data. Archive verification checks account binding and integrity. Isolated restore
  preserves paper/live ledger bytes, revokes pilot enrollment and writes the recovery
  marker that forbids Store/Ledger startup. Old paper-only archives remain verifiable.

These are locally tested capabilities. They do not establish actual broker
compatibility, completeness of broker books or successful live-ledger backup in AWS.
No production workers or accounts are changed by developing or merging code.

## Required work still open in this same PR

| Requirement | Current status | Acceptance evidence |
| --- | --- | --- |
| Kotak order transport and bounded session handling | Adapter/journal implemented; not connected to production | Fake SDK, real pinned SDK with mock HTTP, timeout/reaping and restart tests pass locally; actual broker validation pending |
| Telegram/quote Live worker and lease/supervisor integration | Protection-monitor component and shared account lease implemented; entry feed and production launcher still open | Monitor tests pass without paper mutation; full signal-to-order acceptance pending |
| Partial-entry protection and serialized target/trailing exits | Monitor coordinates entry remainder cancellation and confirmed partial-fill protection; targets/trailing still open | Late fills, rejection escalation, unknown outcomes and restart tests pass; end-to-end strategy acceptance pending |
| Multi-day trade/order history and fee corrections | Retained terminal history, bounded current-day trade collection and fee corrections implemented; verified statement import still open | Day rollover, carried positions, absent working orders, archive conflicts and fee corrections tested locally |
| Production pre-dispatch risk and account authorization | Simulator only | Same atomic checks with current policy, revocation and trusted broker funds/marks |
| Live monitoring, incidents and controlled daily reset | Account/Owner health displays and read-only endpoints implemented; production alert delivery and reviewed incident resolution remain open | Account isolation, stale/corrupt/missing ledger tests; no reset or automatic incident clearing |
| Production two-account execution routing | Not implemented | Independent sessions/ledgers, no cross-account orders, fresh approval at each boundary |
| Actual AWS live-ledger restore drill | Not performed | Encrypted upload/readback and isolated restore using the deployed recovery build |
| Supervised real order | Not performed/authorized | Explicit account-owner approval after all previous gates pass |

`ExecutionHarness` accepts only the built-in `SimulatedBroker`; replacing it with
an SDK object is rejected. It supports only NSE NRML, unit premium multiplier 1,
one open exposure per account, daily entry limits and reservations for all pending
or unsold exposure. It is not a production worker or a substitute for pending
transport/strategy work. Do not remove these restrictions to make an activation
switch work. Pilot consent means reviewing configuration, not authorizing trades.

`KotakSession`, `ProcessSession`, `Commands` and `Observations` are the new transport
integration components; no production entrypoint constructs them. The command
journal is not an authorization/risk gate. It only accepts an already-committed
dispatch and does not replace the outstanding policy, cash, fee and worker checks.
Current books and current-day trades are not a historical archive, and the adapter deliberately does not
claim that RMS Net is available cash. A missing order remains a blocker.
The sole exception is an earlier-day terminal order with previously sealed,
unchanged order/fill evidence. Retaining an order does not certify its fees, broker
statement completeness, settlement or available cash. No code infers an expired
DAY order merely because a new trading day began.

The SDK adapter maps protective sells to **stop-limit (SL)** with an explicit,
tick-aligned limit no higher than the trigger. A triggered stop-limit may remain
unfilled through a price gap. The simulated SL-M examples do not prove that market
stops are available for the actual instrument. Production needs a reviewed stop
limit/gap policy, monitoring and an explicit escalation procedure before activation.
Do not round an odd-lot partial fill upward or sell beyond confirmed exposure.
`ProtectionMonitor` requests cancellation of the unfilled entry remainder on the
first partial fill and observes/protects fills that win that race. It takes at most
one action per fresh book cycle. Cancellation uncertainty blocks further commands
and explicitly reports review-required exposure. The component is not connected to
a production service; polling cadence, alerts and supervised gap escalation remain
release requirements. A transport acknowledgement never reduces its uncovered count.

The monitor requires a separately bound, immutable stop-limit price per entry. It
does not infer a price from a paper setting, round odd lots upward, replace rejected
stops in a loop, clear incidents, or liquidate an account on withdrawal. Its account
wrapper currently always blocks/cancels new entries while preserving protective
management. There is no entry-authorization switch in the wrapper.

## Transport validation evidence

`test_live_kotak.py` exercises the installed pinned SDK through HTTP MockTransport;
it never sends a broker request. Other cases use synthetic SDKs to check identity
and route changes, expired sessions, malformed acknowledgements, sensitive error
suppression, process timeout and cleanup. `test_live_commands.py` verifies durable
handoff, crash-before-send, accepted-but-timed-out placement, cancellation races
and cross-account rejection. `test_live_observations.py` verifies all-or-nothing
book ingestion, missing orders, changed terminal fills and protective price checks.
`test_live_protection.py` includes incremental partial fills and retained capacity
during cancellation. `test_live_monitor.py` covers the full monitor protocol and
verifies that a competing worker prevents even session creation. Fee-correction
tests preserve original fills across restart and reject cross-account, changed or
out-of-order statement evidence. None of these tests is a real broker or AWS
acceptance test.

History tests cover next-day reconciliation and observation ingestion, restart,
same-day absence, missing fills, changed local evidence and broker position mismatch.
Reservation tests verify that midnight resets the count without releasing pending
premium, and that a closed losing trade frees exposure while its loss still blocks
the next order when the daily budget would be exceeded. Date rollover never clears
incidents, resumes entries or bypasses a fresh broker-book comparison.

The production Paper/Live API still returns 409 for Live. There is no environment
variable, repository variable, timer or UI button that enables real order submission
in this draft. No new systemd Live service is installed. No trading behavior changes
on October 19 merely because that date arrives.

## Integrated evidence and operator checks

The session's `evidence` operation brackets trades, positions and limits with two
order-book reads. Both normalized books must match within the existing 20-second
collection bound. The full process call still has its wall-clock deadline and no
retry. This detects a moving order book; it does not prove the broker APIs provide
an atomic or complete snapshot. Actual account response validation remains open.

`Observations.ingest_evidence` commits observed orders, positions, command
confirmations and current-day fills together. It rechecks reconciliation after
adding the fill fingerprint. `ProtectionMonitor(collect_trades=True)` exercises
this path before a protective command. Tests use the real adapter with a synthetic
SDK, including a partial fill, cancellation race, incremental stop coverage and
late fills. There is still no production launcher or BUY authorization path.

The SDK trade report does not provide verified charges, and the RMS documentation
does not establish a spendable-cash field. Each imported fill therefore has an
explicit `broker_fill_evidence.fee_status=unverified` marker. A zero fee in the
immutable fill row is only a storage placeholder. `Accounting.summary` refuses
risk accounting while any such evidence exists, even after an offline fee
correction. Terminal history sealing also remains blocked until verified fee
import is implemented. Do not manually remove these markers to enable entries.

Health is observational: a matching book does not mean the release is authorized.
Counts refer to the last successfully ingested book. During a failed cycle, actual
broker exposure may have changed; investigate using the broker application.
A stale check (older than 30 seconds), unresolved command or incident must not be
cleared by editing SQLite or restarting the service. The health endpoints use
read-only SQLite connections and return counts/statuses, not broker IDs or secrets.
The UI refreshes health every 20 seconds while mounted and discards failed reads.
No automatic liquidation, retry, incident reset or worker restart is provided.

## Preparation API

All routes use existing web CSRF/Origin or native bearer authorization.
No API credentials, TOTP seeds or passwords belong in GitHub inputs or chat.

- Owner: `PUT /api/admin/live/pilot/{uid}` with `limits` containing capital,
  max_order_premium, max_open_premium, daily_loss, max_trade_loss, max_open_risk,
  fee_reserve (rupee decimal strings), max_lots and max_entries (integers).
- Account health: `GET /api/live/health`; owner inspection: `GET /api/admin/live/health/{uid}`.
- Account: `GET /api/live/pilot`; review current policy with
  `POST /api/live/pilot/review`, version and confirmation `REVIEW PILOT LIMITS`.
- Account withdrawal: `POST /api/live/pilot/revoke` with `{}`.
- Owner revocation: `POST /api/admin/live/pilot/{uid}/revoke` with `{}`.
- Read-only probe: existing `POST /api/live/probe` with a current TOTP and
  `READ ONLY CHECK`, now also available to the reviewed second pilot account.

The UI starts limit fields blank. Paper cash/settings are never copied into a live
policy. Limits are configuration proposals, not verified broker capital. A fee
reserve is an explicit estimate and is not a cap on actual exchange/broker charges.
New-account registration alone does not enroll it in the Live pilot.

## One eventual deployment

After the code requirements above are closed and CI passes on the final commit:

1. Review one consolidated PR, then merge it. Record the exact release commit.
2. Verify the current private backup and key escrow. Confirm the separate recovery
   bundle used by `orion-backup.service` is upgraded to support `live.db` **before**
   any production live ledger is created. App deployment does not necessarily
   update that independent backup bundle. Run the existing backup activation/drill
   workflow after review; inspect the version-pinned restore evidence.
3. Agree on a service restart window. The existing **Deploy paper application**
   workflow builds the Expo web app and release in GitHub/AWS. Its installer restarts
   workers/supervisor/portal; this is not a zero-interruption deployment. No local
   build is required from the operator. Keep Live disabled for deployment.
4. In Owner, enroll the two selected approved accounts with independent limits.
   Each account reviews its policy and validates its own broker connection in the
   planned maintenance window. Do not send TOTP values in GitHub workflow inputs.
5. Verify the exact deployed release, paper positions, the plain ORION header,
   backup health, both broker identities and static-egress/API approval. A successful
   read check is not order-placement authorization or execution readiness.
6. Only after remaining production gates pass, request separate explicit real-money
   activation and supervised minimum-size validation for each account. Neither
   account inherits the other's approval. Otherwise keep Paper running and defer.

Do not roll back to a recovery build that rejects or omits existing live ledgers.
Never replace a running live ledger with a restored backup: preserve broker history,
reconcile all unknown outcomes and positions, and keep recovered copies quarantined.

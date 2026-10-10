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
| Kotak order transport and bounded session handling | Not implemented | Fake-SDK failure tests, then separately approved account-bound broker validation |
| Telegram/quote Live worker and lease/supervisor integration | Not implemented | End-to-end signals to reconciled orders with no paper mutation |
| Partial-entry protection and serialized target/trailing exits | Incomplete; terminal-entry simulator only | Partial fills, rejects, fill/cancel races, disconnects and restart tests |
| Multi-day trade/order history and fee corrections | Incomplete | Overnight positions and absent historical orders reconcile without assumptions |
| Production pre-dispatch risk and account authorization | Simulator only | Same atomic checks with current policy, revocation and trusted broker funds/marks |
| Live monitoring, incidents and controlled daily reset | Not implemented | Operator-visible unprotected exposure, stale feeds and tested recovery |
| Production two-account execution routing | Not implemented | Independent sessions/ledgers, no cross-account orders, fresh approval at each boundary |
| Actual AWS live-ledger restore drill | Not performed | Encrypted upload/readback and isolated restore using the deployed recovery build |
| Supervised real order | Not performed/authorized | Explicit account-owner approval after all previous gates pass |

`ExecutionHarness` accepts only the built-in `SimulatedBroker`; replacing it with
an SDK object is rejected. It supports only NSE NRML, unit premium multiplier 1,
one open exposure per account, and conservative lifetime reservations inherited
from the foundation. It is not a production worker or a substitute for pending
transport/strategy work. Do not remove these restrictions to make an activation
switch work. Pilot consent means reviewing configuration, not authorizing trades.

The production Paper/Live API still returns 409 for Live. There is no environment
variable, repository variable, timer or UI button that enables real order submission
in this draft. No new systemd Live service is installed. No trading behavior changes
on October 19 merely because that date arrives.

## Preparation API

All routes use existing web CSRF/Origin or native bearer authorization.
No API credentials, TOTP seeds or passwords belong in GitHub inputs or chat.

- Owner: `PUT /api/admin/live/pilot/{uid}` with `limits` containing capital,
  max_order_premium, max_open_premium, daily_loss, max_trade_loss, max_open_risk,
  fee_reserve (rupee decimal strings), max_lots and max_entries (integers).
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

# Consolidated Live acceptance record

PR #33 is merged at `f4ad10059e6b811ec2e26e1cfd3f9930522d687b`.
The follow-up integrates [bounded session authorization and worker checks](live-entry-activation.md).
Local implementation and synthetic validation have not submitted real orders,
started production services, authenticated to Kotak, changed Paper settings, or
established AWS Live recovery acceptance.

## Confirmed pilot scope

The owner confirmed deposited cash only, no pledged collateral, and no manual
trading or withdrawals outside Orion while it runs. Orion must provide manual
cancellation and position exits. This is an operating constraint, not evidence
that an API field is spendable cash. The second account needs its own equivalent
review; do not inherit the owner's cash declaration or opening balance.

The user does not need to reverse engineer Kotak's internal cash calculation.
Validate a cash-only funding approach from real, account-bound observations and
broker records. A configured allocation is a spending ceiling, not proof of funds.
Candidate-specific margin checks remain an additional check, not a cash reservation.
Do not promote `Net`, `avlCash`, zero shortfall, or missing collateral fields to
verified cash. The implemented reviewed opening allocation does not recycle sale proceeds.
It remains an operating cap, not a broker-verified spendable-cash field.

## Implemented in this batch

- `EntryPreparation` atomically joins source/policy/quote/risk evaluation,
  reservation, instrument and multiplier bindings, execution terms, and immutable
  exit instructions. An error rolls back the entire reservation. It holds the
  account lock once, avoiding nested file-lock acquisition. This is not dispatch
  authorization. Prepared entries can be abandoned only before any dispatch
  attempt or broker command; their source and audit records prevent replay.
- Stale quote responses return no usable prices while keeping a valid session
  available for independent book reads. Missing exit quotes block price-based
  actions but do not prevent confirmation, fill protection or target timeouts.
  Identity/schema/transport failures still close the broker session.
- Durable manual requests are processed by the existing leased worker, after
  broker reconciliation, through the same command journal and capacity checks.
  Cancel requests retain partial-fill protection. Exit requests wait for terminal
  cancellation observations for existing sells and recompute unsold quantity.
  Cancellation acknowledgements do not release capacity. Restart, duplicate
  clicks and timeouts never resend a previously attempted command.
- Authenticated own-account routes: GET/POST `/api/live/controls` and POST
  `/api/live/pause`. Mutations require CSRF for browser sessions and a recent Live
  worker heartbeat. Missing/inactive workers and recovery trees are refused;
  the routes cannot create a ledger, start a worker, log in to Kotak or grant BUY
  permission. The owner cannot use these endpoints to trade another user's account.
- The Account screen provides a second confirmation step, shows pending outcomes,
  and accepts an explicit tick-aligned minimum exit price. A full exit covers only
  final confirmed entry fills; first cancel any still-working entry. Authorization
  expires after 60 seconds. Limit orders may not fill. An expired working exit is
  cancelled once; after its terminal observation the remaining position returns
  to protection and review, with no automatic repricing or market fallback.
  Explicit manual exits can reduce exposure during known strategy/protection
  review incidents. Unknown broker outcomes, conflicting evidence and unexpected
  incident codes remain blocking. The exact manual authorization is rechecked
  immediately before transport and never clears an incident or enables entries.
- The one-shot read-only check now returns allowlisted numeric RMS fields for
  display next to its timestamp. Missing values are `null`, not zero. Raw response
  bodies, credentials, account identifiers and tokens do not leave the child
  process. This diagnostic snapshot is not saved in Git or treated as funding
  authorization. The existing worker lease still refuses a competing Paper login.

## Manual operations

When a Live worker is eventually installed and activated, open **Account → Your
Live trade controls**. Check the instrument and open quantity before confirming.

1. **Cancel pending entry** cancels only the entry's unfilled quantity. Confirmed
   fills remain open and managed. A completely unsent prepared intent is locally
   abandoned instead of sent to the broker.
2. **Review exit position** requires a minimum sell price. Confirm the instrument,
   quantity and limit. Existing stop/target orders are cancelled serially, with
   broker confirmation required before a replacement sell. Fills during the
   cancellation reduce the exit quantity.
3. **Pause new Live entries** closes the entry gate. It does not itself liquidate
   positions or disable protective management.
4. Pending means pending. For an unknown response, stale worker or review state,
   inspect broker orders/positions before taking any external action. Never infer
   an empty position from a command acknowledgement. Avoid competing manual
   broker orders while Orion has unresolved commands.

Monitoring activation is described in [live-worker-activation.md](live-worker-activation.md).
The separate bounded session authorization control remains locked by release acceptance. A new signal cannot reuse a
previous signal's durable entry identity after a manual exit.

## Remaining acceptance work

| Requirement | Evidence still required |
| --- | --- |
| Cash-only funding and fees | Compare fresh account data with broker funds records; validate the implemented cash allocation and fee model against settlement and unexpected changes. |
| Production BUY coordinator | Coordinator and bounded market adapter implemented and locally tested; bounded session authorization and activated-worker integration are implemented; actual broker evidence and release acceptance remain required. |
| Worker deployment and handover | Own-account monitoring launcher and authentication lifecycle implemented; scheduled deployment, installed-service verification and actual account handover remain required. Existing Paper workers are preserved until explicitly scheduled. |
| AWS recovery | Restore an actual Live ledger backup into isolation and verify its account, fills and keys without production writes. Existing Paper backup success does not establish this. |
| Broker acceptance | Validate contract mapping/static egress and both accounts' read-only evidence; obtain specific approval for a supervised real order and verify fills, cancellation and protection. |

The full test suite uses synthetic broker responses. Passing it does not establish
Kotak account compatibility, real fills, AWS restore success or Live readiness.
No real-money activation date is guaranteed by this record.

## Daily cash allocation and statement fees

`EntryPreparation` now requires the account's immutable India-day cash review.
The review binds current pilot policy, UCC and prior ledger history; reservations
use full purchase premium, retain attempted-entry fees and never reuse intraday
sale proceeds. Account-owned cash and closed-day statement forms/API routes are
integrated without enabling Live. Statement reviews append corrections only after
complete identity/quantity/price and charge-total matching. Later corrections or
new fills invalidate fee resolution. See [live-finance.md](live-finance.md) for
operational requirements, private document handling and remaining broker/AWS
acceptance. These checks do not certify RMS fields or authorize order dispatch.

Finance batch validation: the full local suite passed 488 tests. Subsequent
account-binding and inactive-worker bookkeeping refinements passed all 13 focused
cash/API tests (including two additional cases). TypeScript type checking, Expo
lint and the production web export passed. No AWS or actual broker operation was
performed by this batch; synthetic deployment/SSM output in tests is not evidence
of an AWS action. All changes remain on the single consolidated release branch.

## Coordinator and strict Live drill

See [live-coordinator.md](live-coordinator.md). The coordinator integrates broker
market/quote/funding reads with cash and policy checks, and tests the atomic
intent-to-command handoff while the production release gate remains closed.
`Backup operations` now offers `live-drill`, which cannot pass on Paper-only
backups or on two ledgers with mismatched broker identities or incomplete schemas.
Actual broker and AWS acceptance remain unverified.

Coordinator/recovery validation: 503 tests passed in the full local suite. The
final market-operation allowlist change passed all three market tests, including
real subprocess framing with a fake SDK and continued REST reads after missing
market status. This is synthetic broker validation, not a real broker login.
The previously validated frontend is unchanged by this coordinator/recovery step.

## Monitoring worker activation

The Account screen now provides own-account reviewed startup with a current TOTP,
paused entries, durable account reservation and verified-flat stop. The existing
supervisor owns isolated children; duplicate login, automatic reauthentication,
Paper takeover after a crash and deployment over reserved Live slots are blocked.
See [live-worker-activation.md](live-worker-activation.md) for the scheduled full
deployment and per-account handover requirements. The real-money entry gate
remains closed.

Activation validation: the full local suite ran 518 tests successfully with one
Unix-socket integration test skipped because the local runtime denies sockets.
The final activation/failure-isolation refinements passed all 14 focused tests
with the same one skip. TypeScript checking, lint and production web export also
passed. Linux CI must run the socket integration check; actual EC2/systemd and
broker acceptance are still outstanding.

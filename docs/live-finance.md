# Live cash and fee reviews

This is part of the consolidated Live release. It does not activate
Live, start workers, log into Kotak, authorize a BUY or modify Paper state.
Local tests use synthetic accounts and broker responses. No actual Kotak cash
semantics, real order, or AWS Live recovery drill is certified by these tests.

## Daily deposited-cash allocation

In Account → Live pilot preparation, **Your daily Live cash allocation** records
the signed-in account's own review. An owner cannot submit this review on behalf
of the second pilot account. The amount is a conservative allocation from cash
already settled and deposited, no greater than the account's reviewed capital.
It must not include collateral, credit, unsettled proceeds or anticipated funds.
There is no inferred formula using Kotak RMS `Net` or a missing funds field.

The account owner confirms deposited cash only, no external trades or withdrawals
while Orion runs, and acceptance that the configured round-trip fee allowance is
an estimate. Its adequacy still needs review against actual broker charges. Zero
API brokerage does not establish zero total charges. If settled cash cannot be
established, do not submit an allocation. Resolve the balance with the broker.

Recording requires an existing active Live monitor, a fresh complete reconciled
broker book, no pending commands, no incidents, a flat account, complete prior
fees, and no earlier intent that trading day. The current release does not start
that monitor from this form. It refuses an active Paper worker through the shared
worker lease; do not stop Paper just to exercise the new form.

Each allocation is immutable for its India trading day. An identical retry is
idempotent; replacement, top-up and reset are refused. It binds the account, UCC,
current reviewed pilot policy and a fingerprint of pre-existing ledger history.
A policy or credential change invalidates entry eligibility. Tomorrow requires a
new review. A mistaken allocation requires operator review; there is no same-day
reset endpoint, including for the administrator.

`EntryPreparation` now requires this review inside its atomic reservation
transaction. Full pending purchase premium and a fee allowance consume the cap.
A confirmed cancellation releases only unfilled premium. Filled premium remains
consumed even after an exit, including profitable exits. Sale proceeds are never
credited during the day. Unsent abandoned intents release their reservations;
attempted entries retain the fee allowance. Higher recorded charges increase the
hold. Daily risk also retains allowances for attempts without a fill.

This is explicitly **reviewed opening allocation**, not a verified broker balance.
Responses keep `available_cash_verified`, `fees_verified` and
`order_submission_available` false. An exact fresh per-order margin check and a
reviewed production dispatch gate remain separate dependencies. Never turn the
allocation response into an order authorization.

## Closed-day fee reconciliation

In **Review previous-day Live charges**, enter the India trading date and load
that account's known fills. Compare every trade against the final broker contract
note; if a broker trade is missing, stop and request reconciliation. Enter the
seven charge categories (explicit zero when actually confirmed) and allocate the
full total across fills to the paise. Every fill must occur once, with exact
broker order ID, side, quantity and price. Allocations must sum exactly to the
charge categories. Negative rebates and open-day provisional notes are unsupported
and require operator review rather than forced import.

Paste the original note text and confirm the review. This is a human-reviewed
transcription, **not** an automatic Kotak PDF parser or document authentication.
Keep the original document privately for audit. The server retains a SHA-256
fingerprint, normalized reviewed data and append-only fee corrections; it does
not save the original text or document. Do not put real statements or account
information in Git, workflow logs, issue comments or public artifacts.

The original `fill_history` and raw `broker_fill_evidence` remain unchanged. Raw
fills retain their original unverified provenance. Accounting resolves fees only
through the latest complete reviewed statement and its exact matching correction.
A subsequent fee correction or newly discovered fill makes that evidence
unresolved again. Reload the day and review an explicit superseding statement.
Replaying an older successful import does not re-certify later changes. Any
failed correction rolls back the whole review. Successful reviews pause entries;
there is no automatic resume or activation.

Current-day risk may use the explicitly accepted provisional fee reserve. Prior
days' unresolved charges block the next budget. The UI health count now reports
unresolved fills after considering statement reviews, rather than counting every
historically unverified raw row forever.

## Private API contract

All endpoints act only on the authenticated account. POSTs require the existing
origin/CSRF protection (or authenticated native bearer session). Cash reviews
require an existing fresh Live worker. Historical fee reviews need only the
account's existing bound Live ledger and work after pilot consent is withdrawn;
bookkeeping must not require renewed trading permission. Neither starts a worker.

- `GET /api/live/finance`: latest recorded cash review, not a live cash balance.
- `POST /api/live/finance/cash`: `opening_cash` decimal string and exact
  confirmation `DEPOSITED CASH ONLY; NO EXTERNAL TRADES OR WITHDRAWALS; FEES ARE ESTIMATES`.
- `GET /api/live/finance/statement/{YYYY-MM-DD}`: normalized complete fill template,
  unknown fees as blank fields and latest `supersedes` review ID.
- `POST /api/live/finance/statement`: template with reviewed charges and fees,
  confirmation `I REVIEWED THE BROKER STATEMENT`, and exactly one of `proof_text`
  (original note text, at most 100,000 characters) or `proof_base64` (original
  private document bytes, at most 10 MiB decoded). The normalized statement has
  exactly `account`, `ucc`, `trade_day`, `charges`, `fills`, `supersedes`.

The statement endpoint authenticates before accepting a larger body, is limited
to three submissions per 15 minutes per account, and caps its complete JSON body
at 16 MiB. Other endpoints retain the 32 KiB limit. Validation errors do not echo
statement content. Recovery-quarantined or symlinked ledgers refuse control.

## Release acceptance still required

Finish the production entry coordinator with trusted feed/market provenance,
atomic revalidation immediately before dispatch and broker-specific cash/margin
acceptance. Verify the static egress/broker contract configuration, Paper-to-Live
handover, actual AWS Live-ledger restore drill and supervised account activation.
The present software tests establish accounting and access-control behavior only.
Preserve the user's single consolidated release preference; do not independently
push or deploy this finance increment.

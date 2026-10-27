# Paper-testing periods

After deploying this change, open Account → Start a new paper-testing period.
Pause entries, finish any open paper trades, enter starting cash (the UI defaults
to ₹400,000), choose a label, type `START NEW PERIOD`, and press
**Archive results and start new period**. Pausing entries cancels pending signals
using the existing pause behavior. The reset itself rejects any pending or open
position and leaves entries paused.

The current period starts with the entered cash, zero realized P&L, and zero
daily entry counts. Risk limits, channel choices, credentials and connections do
not change. The daily loss allowance starts over even when resetting within the
same India trading day. This is an explicit paper-testing boundary, not a way to
claim the earlier losses never occurred or to prove that all were software bugs.

Performance defaults to the current period. Select an archived period to review
its P&L and trade breakdown, then open a trade to see its retained execution and
Telegram evidence. Dates still use India trading days; “Whole period” includes
all dates in the selected testing period. The Activity log retains all periods
and records `PAPER_PERIOD_STARTED` boundaries. Dashboard totals and cash
reconciliation describe the current period only.

## Persistence and safety

`POST /api/paper-period` uses existing authenticated, CSRF-protected account
identity. It accepts `amount`, `label`, `expected_period` (the current period ID,
or `legacy` before the first reset), and `confirmation: "START NEW PERIOD"`.
A stale/retried request returns 409 after the first successful reset. Refresh the
account to see the result if the first response was lost; do not submit a new
reset just to retry it.

The account file lock and a SQLite `BEGIN IMMEDIATE` transaction cover the
archive, boundary audit record, and current-state update. The `paper_periods`
table stores the prior state and settings with an inclusive final audit sequence.
No prior audit events, source messages, closed positions, signal fingerprints or
seen-event IDs are deleted. Retained positions prevent old Telegram calls from
being opened again. Reporting excludes the positions already present at the
start of the current period, using its recorded signal IDs; the engine continues
to retain these closed/cancelled records. Daily counters are copied into the
archive and then cleared. Nothing touches Live ledgers or submits broker orders.

There is no deployment-time reset. Existing accounts retain their behavior until
the user starts a period. Archives live in the same `paper.db` and are included
in its SQLite backups. Use period-aware portal code for reporting after a reset;
older portal versions do not understand the new reporting boundary.

`GET /api/performance?paper_period=current` selects the current period (default).
Supply an archived ID from `paper_periods` to review an archive. The corresponding
owner performance endpoint supports the same selector with existing owner access
checks. Trade detail URLs do not change.

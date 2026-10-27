# Paper provider close replies

Paper accepts direct replies to an original signal containing `Close Position`,
`Close This Position`, or `Exit Now`, optionally followed by the provider's
`| Partial Loss Booked from 1242.00 → 1122.50` template (profit also supported).
The explicit Close Position instruction closes all remaining lots; the word
Partial in this summary describes the booked loss/profit, not an exit quantity.
Standalone partial-booking instructions and conditional commentary remain
unexecuted. Edited instructions continue to require review.

Reply references resolve only within the originating channel and only to an
existing pending/open signal. Missing/closed parents or conflicting explicit
contract identities produce EXIT_REQUIRES_REVIEW, without guessing another
position. Full-identity exits without replies retain their unique-match behavior.
Pending signals are cancelled; open positions wait for a valid open-market quote
and sell at its bid. Text prices are never used as simulated fills. Requests
survive restart and duplicate events do not create extra fills.

This changes the Paper engine only. It does not rewrite historical outcomes or
implement Live exit execution. Deploy with the existing full deployment workflow
to restart Paper workers; a portal-only deployment does not update those workers.
Verify PROVIDER_EXIT_PENDING_QUOTE followed by PAPER_EXIT with reason PROVIDER
for subsequent qualifying messages.

For a missing entry, inspect its actual recorded admission decision before
changing risk, expiry, price or catalogue rules:

```bash
python3 scripts/inspect_paper_signal.py --db /path/to/paper.db --signal=-1003243650660:8116
```

The diagnostic opens SQLite read-only and prints only the requested position and
its audit decisions. A missing row alone does not establish a software defect.

## In-app comparison

Tap a Paper trade in Trades, an open-position preview, or the performance trade
breakdown to open execution and Telegram evidence. Signal Activity also links to
the same screen for rejected/unfilled signals. Owner views preserve the selected
account; the API independently enforces account isolation and owner permissions.

The screen shows entry, initial/latest stop, targets, execution prices and lots,
fees, cumulative realized P&L, and final exit reason. A STOP exit does not imply a
losing trade. The timeline combines ledger events with retained original Telegram
messages, edits and reply chains from the same channel, including provider and
receipt timestamps in IST. Telegram text is evidence of what the provider said,
not a simulated fill. Unlinked posts and media contents are not reconstructed.
Refresh updates the view; Load more retrieves further timeline events.

New entry-limit rejections record all triggered blockers and the observed bid,
ask, spread, daily P&L, entry count and open-position count alongside the limits
at that moment. Older generic rejections explicitly show that their exact reason
was not recorded; current settings are never substituted as historical evidence.
Existing ledgers and source messages are read without a migration or P&L rewrite.

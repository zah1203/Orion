# Opt-in stop-limit fallback

This change prepares a reviewed execution setting; it does not enable Live or
submit an order during setup. Existing policies default to fallback off. Saving
a policy requires the account to review its new version before entry use.

For an opted-in entry with an initial 20-tick stop-limit gap:

1. Place the usual stop-limit. A pending trigger or a quote crossing alone does
   not start the recovery timer.
2. Persist the first reconciled broker observation that the accepted SL order is
   triggered and working. The supported book has no exact exchange trigger
   timestamp, so polling and reconciliation latency precede this timer.
3. After 5 seconds, if still working, modify its limit once to trigger minus
   50 ticks. Keep its broker order ID, trigger, total quantity and order type.
   A partial fill never adds units back: the unchanged total includes fills.
4. Both stages share a deadline 30 seconds after the first observation. The
   modification transport waits only for the remaining time. At expiry, leave
   the stop working, pause new entries, and raise review-required health/alerts.
   Never cancel protection or place a replacement/market sell for this fallback.

An acknowledgement is not confirmation. Confirm changed terms from the broker
book. Rejected stops, unknown sends, malformed/stale evidence, unresolved
commands, odd-lot residuals and incidents do not authorize widening. A lost
response may mean the broker accepted the modification; reconcile and review,
never resend. A local timeout cannot undo a request already received by Kotak.
An acknowledged modification followed by terminal original terms reconciles the
actual fills but retains the unconfirmed journal and raises durable review health,
including when exposure is zero and after restart. It does not assume the
modification succeeded.

The policy is bound before entry dispatch and retained with the position through
restart or enrollment revocation. Timers start during observation, even before
strategy handoff. Automatic widening requires final entry fills and the strategy
coordinator; an unresolved partial entry remains in cancellation/protection
handling and cannot gain more time by later handing off. Profit-target timeout
is a separate setting and takes priority over fallback waiting or expiry. Its
cancellation still requires a terminal broker observation before restoring stop
coverage; fallback never releases that reserved target capacity. Stop deadlines
are recorded even while target cancellation is pending. A quote gap while the
broker still reports trigger-pending pauses entries without latching a new
incident that would prevent later eligible recovery; existing incidents remain.

50 ticks is a wider permissible sale limit, not a promised fill or a maximum
loss. For tick size ₹0.05, gaps are ₹1.00 and ₹2.50 below the trigger. A gap below
the second limit can still leave the order unfilled. The reviewed initial stop
must exceed 50 ticks to keep the fallback limit positive.

## Validation and release

Synthetic tests cover pending/triggered distinction, unchanged acknowledgements,
partial fills, a fill before modification, same-ID SDK request encoding, deadline
transport termination, restart, timeout retention, rejected/uncertain outcomes,
and opt-in binding. They do not constitute broker acceptance.

Before release acceptance, verify the pinned Kotak SDK 3.0.7 and broker book
semantics for triggered SL orders, modifying the same order after a partial
fill, total-quantity interpretation, and rejected/late modifications. Verify
operator alert delivery and restore of the timer/command journal. The Live
release gate remains closed pending the existing acceptance requirements.

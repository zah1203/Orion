# Background channel monitoring (paper only)

The per-account `orion-worker@.service` keeps listening to both configured Telegram
channels without a browser, laptop tunnel or Session Manager terminal. The portal
remains a separate service. A saved Telegram session is required. Broker session
expiry does not stop Telegram recording. No real orders are submitted.

## Authentication

Pause paper entries, then use **Authenticate Kotak feed** in the portal with a
fresh authenticator TOTP. Successful authentication saves only the SDK feed fields
(edit token, session ID, UCC and broker websocket URLs), encrypted with the existing
portal key, bound to the account and its credential fingerprint. Neither TOTP nor
an authenticator seed is stored. Tokens never return to the browser or journal.

The service polls the cache and picks up new sessions without restarting Telegram.
Restarting the service reuses a cached session while Kotak accepts it. Orion does not end reuse at midnight IST. Legacy cached midnight cutoffs are also
ignored because they were local policy, not broker expiry metadata. This does not
extend the broker's token lifetime. After expiry, revocation, or three failed feed attempts,
reauthenticate in the UI. This release does not automate daily TOTP generation.
UI authentication requires paused entries but does not require stopping the service.
Credential replacement and Telegram relinking still require stopping the worker.

## First deployment

1. Pause entries. Stop the old foreground worker with Ctrl+C in its own terminal.
   Do not delete the key, account database, credentials or paper ledger.
2. Merge the reviewed change and run **Deploy paper application** from main. The
   installer stops account workers before switching releases and leaves them
   stopped. Explicitly restart services after deployment. Existing release files
   are not hot-patched.
3. In an EC2 Session Manager shell run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart orion-portal.service
sudo systemctl enable --now orion-worker@jadmin.service
sudo systemctl status orion-worker@jadmin.service --no-pager
```

The unit is installed by the release installer. It reads `/etc/orion/portal.env`,
uses the existing portal key/data, and reads
`/var/lib/orion/portal/contracts.json`. It never prompts on stdin. For another
account, use its existing username in place of `jadmin`. The worker lease prevents
a service and foreground worker from driving the same paper account.

4. Open the dashboard; keep entries paused. Authenticate Kotak using the new UI
   action. Confirm Telegram connected, Kotak connected and catalogue current.
   Connected is transport status; verify a fresh quote timestamp during market
   hours before relying on paper fill behavior. Enable entries for paper testing.
5. Close the Session Manager worker shell, reopen a new session and inspect:

```bash
sudo systemctl is-active orion-worker@jadmin.service
sudo journalctl -u orion-worker@jadmin.service -n 40 --no-pager
```

This independent-session check and the first live channel/quote smoke test must be
performed on EC2; local mocked tests do not prove deployed connectivity.

## Overnight behavior and monitoring

* Source messages and edits from both configured channels are stored in the
  account's private `paper.db`, in `source_events`. The dashboard shows recent
  processing activity and the last channel/message timestamp.
* No broker session or stale/missing catalogue: keep recording, block new paper
  entries. Recording is not permission to execute later; archived calls are never
  replayed automatically on reauthentication or catalogue refresh.
* Feed failure cancels pending entries and clears crossing observations before
  retry. Existing paper positions remain recorded; while quotes are absent,
  stops and targets cannot be simulated. On reconnection only fresh quotes apply;
  there is no reconstruction of missed intraday prices.
* The background worker refreshes stale/missing catalogues daily from 08:30 IST,
  including weekends (no exchange holiday calendar). On startup after 08:30 it
  attempts immediately, then retries failures every five minutes. Before 08:30
  it shows the scheduled time. The worker must be running; no separate cron is needed.
  New entries remain blocked until today's catalogue passes validation.
* Refresh downloads both broker exports with fresh dated checksum receipts and
  checks all selected products, lot sizes, units, precision, expiries and duplicate
  identifiers. Approved economics come from the last catalogue's embedded profile,
  or `economics.json` beside the master when no embedded profile is available.
  The original `verified_on` and source are preserved, not redated. `verified_at`
  records catalogue validation; `economics_reused` identifies reuse of approved rules.
  Premium conversions and expiry cutoffs remain operator-approved assumptions;
  downloads do not independently reverify them or prove the broker updated its source.
  Economics changes require operator review and a manual validated import.
* Concurrent account workers share a catalogue file lease. Downloads run outside
  the event loop with bounded subprocess timeouts. Only a fully validated catalogue
  atomically replaces the old file; failures preserve it and keep new entries blocked.
  Temporary exports are removed after each attempt; their hashes remain in the master.
  Existing open positions retain their stored contract data. Refresh never enables
  paused entries, changes paper cash, replays signals, or submits broker orders.
* Broker retries use the same cached session, not repeated TOTP login. After three
  failures, Telegram keeps listening and the UI requests reauthentication. A new
  successful UI login resets the retry budget.
* Telegram temporary reconnects use Telethon's reconnect behavior. A terminal
  disconnect/error restarts the service after 30 seconds. Messages delivered while
  disconnected may be missing; there is no historical backfill in this release.
* Use **Pause and edit settings** in the dashboard. Pending calls are cancelled;
  open paper trades keep being monitored and block settings/balance changes until
  closed. With no open trades, save channel, instrument and risk changes while
  the worker stays online. Filters and entry limits reload for incoming events.
  Enable entries explicitly when ready. Credential replacement still requires
  stopping the worker.
* Health separates worker heartbeat, Telegram, broker, catalogue and last quote.
  An online worker is not proof of a fresh market feed.
* The service runs overnight and continues monitoring filled BTST paper positions.
  A BTST call must enter its stated range before the signal-day channel cutoff;
  otherwise it expires. Filled positions use their targets, trailed stop, explicit
  provider close messages, expiry protection and the next-session cutoff.

Journal output is limited to engine events and exception class names. Stored
messages grow over time; monitor disk usage. No automatic purge is added here.

## Paper balance adjustments

The dashboard's **Adjust paper balance** sets current simulated cash, separately
from starting capital. Entries must be paused and no OPEN/PENDING positions may
remain. A reason is required. Each changed balance is recorded with before/after
values in `PAPER_BALANCE_ADJUSTED`; retrying the same target is a no-op. Trades,
deduplication, realized P&L and daily entry/loss counters are preserved. This is not
a profit event or a reset of daily limits. The worker can remain online.

Deploy this application update once using the existing deployment workflow, then
restart the portal and account service as described above. Existing workers must
load this release before using live settings edits. Subsequent settings and balance
changes require no terminal access. Daily catalogue refresh is automatic after this
release is deployed and the worker restarted. Daily Kotak authentication still uses
the dashboard; this update does not generate OTPs. The dashboard distinguishes
enabled intake from readiness blocks, shows refresh progress/failures and the last
validation time. New `ENTRIES_PAUSED` events include the actual blocking reasons.

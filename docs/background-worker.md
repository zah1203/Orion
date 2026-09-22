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
Restarting the service reuses a cached session while Kotak accepts it. Orion's
conservative local reuse limit is midnight IST; it is NOT a guarantee of the
broker's token lifetime. After expiry, revocation, or three failed feed attempts,
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
* Daily master exports and the economics verification remain operator-managed.
  Do not simply redate yesterday's CSV/profile. Refresh from the broker and review
  contract economics, then import the catalogue. Atomic replacement is picked up
  by the running background worker; an old catalogue blocks new entries.
* Broker retries use the same cached session, not repeated TOTP login. After three
  failures, Telegram keeps listening and the UI requests reauthentication. A new
  successful UI login resets the retry budget.
* Telegram temporary reconnects use Telethon's reconnect behavior. A terminal
  disconnect/error restarts the service after 30 seconds. Messages delivered while
  disconnected may be missing; there is no historical backfill in this release.
* Channel/instrument settings changes still require pausing entries, stopping the
  account service and finishing open paper trades. Channel filters load at startup.
* Health separates worker heartbeat, Telegram, broker, catalogue and last quote.
  An online worker is not proof of a fresh market feed.
* The service runs overnight; this does not enable overnight positions or BTST.
  Existing channel exit times and BTST review-only behavior are unchanged.

Journal output is limited to engine events and exception class names. Stored
messages grow over time; monitor disk usage. No automatic purge is added here.

# Multi-user paper pilot

This adds a private dashboard to the existing Orion repository. Each user has a login, encrypted credentials, a channel/instrument allowlist, risk limits, an independent paper database, entry controls and trade history. It uses the existing Kotak and Telegram connectors; no real broker orders are implemented.

## Pilot boundaries

- Operator-provisioned accounts; no public signup, email delivery or password-recovery service.
- Kotak only and two existing complete-message parser profiles. A different provider format still needs parser work.
- Browser users can save credentials, complete Telegram login, select channels, validate Kotak with TOTP, and enable/pause entries. Starting the daily paper worker still requires the operator.
- One separately authenticated paper worker process per user. Enabling entries in the dashboard does not launch a worker.
- BTST/overnight calls remain review-only. Selecting a commodity does not activate overnight trading.
- User data is logically isolated by application authentication and per-account files. This is a trusted single-host pilot, not OS/container isolation between mutually untrusted customers.
- Keep the dashboard private through a local connection or SSM port-forwarding. Internet-facing deployment, managed identity/MFA, operational alerting, backups, scalable database/queues and public-service review are later work.

## Run locally (Linux or macOS)

Use Python 3.12 and the repository branch containing this feature:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
mkdir -p "$HOME/orion-pilot"
chmod 700 "$HOME/orion-pilot"
python -m orion.portal init-key --file "$HOME/orion-pilot/portal.key"
export ORION_PORTAL_KEY_FILE="$HOME/orion-pilot/portal.key"
export ORION_PORTAL_DATA="$HOME/orion-pilot/data"
export ORION_PORTAL_ORIGIN=http://127.0.0.1:8000
python -m orion.portal create-user --username alice
python -m orion.portal create-user --username bob
python -m uvicorn orion.portal.app:factory --factory --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers --no-access-log
```

Passwords are entered with hidden prompts. No default/demo account exists. Visit **http://127.0.0.1:8000** using two browser profiles, one per user. Use that exact hostname/port to match the Origin check. On Windows, run the app in WSL or use the Linux EC2 host.

Do not commit key files or runtime data. Encryption keys must be backed up privately, separately from data; losing a key loses access to saved API details and Telegram sessions. A wrong key fails startup. This pilot uses one deployment encryption key; per-tenant KMS envelopes and key rotation are not implemented.

## What to try

1. Sign in as Alice and Bob in separate browser profiles.
2. Set Alice to a maximum of 3 lots and Bob to 1. Give each account its permitted channel IDs and instruments. Settings can be changed only with entries paused, no worker process and no open/pending paper trades.
3. Run **Try your risk settings** for each user. This disposable synthetic NIFTY example uses that user's sizing limits; it does not submit orders or alter the real paper ledger, channels or entry-enabled flag.
4. Observe Alice's partial exits and Bob's one-lot stop movements. The example uses fictional multipliers and a synthetic price sequence; this is a software demonstration, not strategy performance.
5. Save API details if you intend to connect real inputs. The UI returns only saved-field names, never credential values. Saving credentials is separate from account authentication.

An account starts with no configured channels. Each selected instrument group needs its channel ID. The sample supports exact NIFTY/BANKNIFTY and MCX product names. Use only channels whose terms allow the intended processing; account membership alone does not establish permission.

## Per-user Telegram authorization

Use the browser connection setup below. As an optional fallback, after the user saves Telegram API ID/hash the operator can start an interactive login:

```bash
python -m orion.portal telegram-login --username alice
```

Telethon prompts for the Telegram phone number, login code and any Telegram 2FA password. Run this in a private operator session with the account owner. The resulting StringSession is encrypted in that user's credential record; it is not written as a plaintext `.session` file. API ID/hash changes invalidate the stored Telegram session. This CLI fallback is optional; the browser supports the same login steps.

## Per-user Kotak paper worker

Prepare the verified daily full contract catalogue using the portal-account workflow in `docs/contracts.md`. The portal does not yet import or verify a broker master in the browser. The master may be shared read-only when identical for both broker accounts, but account entitlements and quote access must be verified individually. Never use the synthetic demo master here.

With user credentials saved and Telegram authorized:

```bash
python -m orion.portal worker --username alice --master /path/to/verified-contracts.json
```

The operator enters Alice's current Kotak TOTP at a hidden prompt. Start Bob in a separate terminal with Bob's username and code. Never pass passwords, consumer tokens, Telegram sessions or TOTP through command-line arguments. Give distinct app users their own broker accounts; do not run concurrent workers for the same underlying brokerage account.

Each worker holds an exclusive per-user process lock. A second worker/login for that account is rejected, while another user's worker can run independently. Workers obtain credentials only from that selected user's encrypted record, and use that user's channel allowlist and ledger. Authentication/market-feed behavior still needs testing with actual accounts.

The dashboard's worker indicator means a worker heartbeat was seen within 45 seconds, not that quotes are fresh or broker authentication is currently valid. Inspect worker logs for stale-feed/authentication events. There are no automated Telegram/email alerts.

**Pause entries** immediately cancels pending signals and blocks new ones. The running worker continues managing existing simulated positions through the same stop/target rules. Pausing is not an emergency liquidation command. Do not stop a worker with open positions unless you are deliberately interrupting simulated management. Without fresh quotes no stop/target exit can be simulated.

Restart cancels pending signals and preserves open positions; it requires fresh authentication and a current instrument master. Settings/credential changes require stopping the account worker; reconnect afterward. The legacy single-account service remains separate—do not run it against a portal user's database.

## EC2 private dashboard

After merging and deploying this branch through the existing application workflow, its package contains the dashboard and operator commands. The installer installs both service files but leaves them stopped. On the host, create `/var/lib/orion/portal` owned by `orion` with mode 700, generate `/etc/orion/portal.key` with the CLI, and make that key owned by `orion` with mode 600. `/etc/orion` can remain root-owned.

Create `/etc/orion/portal.env` (root-owned mode 600) with:

```text
ORION_PORTAL_KEY_FILE=/etc/orion/portal.key
ORION_PORTAL_DATA=/var/lib/orion/portal
ORION_PORTAL_ORIGIN=http://127.0.0.1:8000
```

Run account-provisioning and worker commands as the `orion` OS user with these environment variables set. No AWS Secrets Manager value is needed for the portal's local encrypted vault. Keep the key outside the application's release directory. AWS KMS/Secrets Manager-backed per-user credential storage is a subsequent hosting improvement, not part of this local vault.

Install/start the loopback-only portal service:

```bash
sudo install -m 644 /opt/orion/current/scripts/orion-portal.service /etc/systemd/system/orion-portal.service
sudo systemctl daemon-reload
sudo systemctl start orion-portal
```

From a laptop with AWS CLI and the Session Manager plugin, forward the port:

```bash
aws ssm start-session --region ap-south-1 --target YOUR_INSTANCE_ID --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
```

Open **http://127.0.0.1:8000**. The network path to EC2 is the authenticated SSM tunnel; no inbound 8000/80/443 security-group rule is added. The supplied Terraform remains unchanged. For remote HTTPS hosting later, configure an exact HTTPS origin and a trusted reverse proxy rather than exposing Uvicorn directly.

Stop account workers before releasing code that changes the portal/engine. The deployment installer stops the dashboard but cannot safely identify and stop independently launched interactive account workers. Do not deploy during active pilot sessions.

## Storage, authentication and recovery

- `<data>/accounts.db`: user password hashes (PBKDF2-HMAC-SHA256, 600,000 iterations and random per-user salts), settings, encrypted credentials, hashed opaque login tokens, session expiry and login attempt counters.
- `<data>/accounts/<server-generated-id>/paper.db`: separate paper state, deduplication, source events and audit. Browser requests never select an account ID or file path.
- Sessions expire after 8 hours, use HttpOnly/SameSite=Strict cookies, and are revoked on logout. HTTPS origins receive Secure cookies. All browser writes require exact Origin and session CSRF verification (login requires Origin only).
- Login attempts are limited per username and direct client IP. No forwarded IP headers are trusted. Reverse-proxy deployments need a reviewed identity/rate-limit configuration.
- The authenticated UI never returns API tokens, MPIN or Telegram session material. Credential records include the owning user ID inside authenticated encryption so swapping database ciphertext between accounts fails.
- Operator password reset: `python -m orion.portal reset-password --username alice`. This invalidates that user's browser sessions, but does not stop their worker or revoke their broker/Telegram sessions.
- Back up application databases consistently and protect the encryption key separately. No backup scheduler, account deletion, retention automation, operator audit or MFA is included yet.

## Verification

63 tests pass locally: 25 engine tests, 9 P&L tests, 12 connection tests and 17 tests for login/session behavior, CSRF and Origin checks, authorization, encrypted credentials, ownership binding, request limits, independent sizing/deduplication/state, restart persistence, isolated settings, logout, login throttling and pause behavior. The app is also exercised through ASGI HTTP requests without requiring a live broker connection.

The tests use synthetic quotes. Two actual Kotak/Telegram accounts, concurrent live feeds, broker entitlements, production hosting remain to be tested during the pilot. Connection flows have automated coverage with fake providers; visual browser and real-provider validation are still required. No production-readiness claim is made.

References: [FastAPI security](https://fastapi.tiangolo.com/tutorial/security/), [Fernet authenticated encryption](https://cryptography.io/en/latest/fernet/).

## Instrument P&L dashboard

The dashboard shows realized, open and total paper P&L by exact instrument (for example GOLD and GOLDM remain separate), with a filter and individual option-contract rows showing strike, CE/PE and expiry. Results cover all recorded trades since the account started, not just today's trades. Pending and rejected signals are not counted as trades.

Realized P&L includes charged simulation fees, including the entry fee for an open position. Open P&L is `(last accepted bid - actual simulated entry) × premium multiplier × remaining lots`; it excludes future exit fees. Partial exits reduce remaining lots so realized and open results are not counted twice. Equity is cash plus the marked value of remaining options.

Quotes are persisted with their timestamps. Missing marks make open/total P&L unavailable; stale or closed-market marks remain visible as last-known estimates with a warning. Legacy ledgers gain marks on the next valid quote. The dashboard refreshes every 15 seconds, and freshness describes the time of the report. These figures are simulated accounting, not broker-confirmed P&L.

The suite now includes 51 tests, including nine P&L accounting/price-quality tests and an expanded cross-user P&L isolation assertion.

## Browser connection setup

The dashboard now has separate Telegram and Kotak credential forms and checks.
Existing encrypted credentials and account databases remain compatible.

1. Pause entries and stop any account worker before changing or checking connections.
2. Save Telegram API ID/hash, enter the phone number with country code, and select
   **Connect Telegram**. Enter the delivered code and two-step password if requested.
   Login expires after five minutes; a dashboard restart requires starting an unfinished
   login again. Completed sessions remain encrypted in the existing vault.
3. Select **Check connection & load channels**. Pick the index and/or commodity
   broadcast channels, select **Use selected channels**, then choose instruments and
   **Save settings**. The two existing provider formats still apply. Discovery inspects
   up to 500 dialogs and returns only broadcast channel titles/IDs, not message bodies.
4. Separately save Kotak token, registered mobile with country code, UCC and MPIN.
   Whitelist the server Elastic IP in Neo, then enter a fresh authenticator TOTP and
   select **Validate Kotak connection**. A successful timestamp means both TOTP login
   and MPIN validation succeeded at that time. No order is placed. The short-lived
   probe's broker tokens are not stored, and this does not start the worker or verify
   market-data entitlements. The operator worker still needs fresh authentication.

Pending Telegram login state is encrypted in process memory, bound to the user and
browser login session, and expires automatically. Codes and two-step passwords are
not persisted. Credential changes invalidate prior checks; Telegram API changes also
invalidate the saved Telegram session. Removing local credentials does not revoke
sessions at the provider; use Telegram Devices if provider-side revocation is needed.
Checks use the same worker lease as CLI authentication to prevent simultaneous use.
The portal must continue running with **one Uvicorn worker**, as shipped. Provider
rate limits are supplemented by per-user limits persisted in SQLite.

### Deploy this update

Merge the connection UI PR, then run **Deploy paper application** on `main` with the
existing AWS variables. Do not rerun Terraform just for this application update.
Stop any interactive account workers first. The installer preserves `/etc/orion`
and `/var/lib/orion` and leaves both services stopped. Do not regenerate the vault key
or recreate existing users. After the workflow succeeds, run on EC2:

```bash
sudo systemctl start orion-portal
sudo systemctl status orion-portal --no-pager
```

Keep/reopen the Mac SSM tunnel and reload the dashboard. Re-entering already-saved
credentials is unnecessary. Automated tests use fake providers;
real Telegram login and Kotak validation must be verified privately after deployment.

# Orion paper app pilot

This release includes an Expo/React Native app for iOS/Android, the same interface for the existing browser portal, owner approvals/reporting and automatic per-user paper workers. Live is visibly locked and the API rejects it. Dhan is not implemented.

## Deploy and owner setup

1. Merge the paper-app PR. It includes and supersedes the unmerged multi-user-workspace PR #12; do not merge that older PR afterward.
2. Run **Deploy paper application** on main. The workflow tests Python, checks TypeScript/lint, exports the browser app, and installs it with the server. Existing databases, cash adjustments, positions, credentials and encryption key remain in `/var/lib/orion/portal` and `/etc/orion`.
3. The installer restores a previously running portal and enables `orion-supervisor`. Legacy per-user worker units are stopped/disabled to avoid duplicate workers. If the portal was already stopped, start it explicitly: `sudo systemctl start orion-portal`.
4. Promote the existing operator account once on the server (no passwords in arguments):

```bash
sudo -u orion env ORION_PORTAL_DATA=/var/lib/orion/portal ORION_PORTAL_KEY_FILE=/etc/orion/portal.key /opt/orion/current/.venv/bin/python -m orion.portal bootstrap-owner --username jadmin
sudo systemctl status orion-portal orion-supervisor --no-pager
```

5. Refresh your existing tunneled dashboard and sign in as jadmin. Owner appears in the bottom navigation. No account is automatically made owner, and subsequent attempts to assign a different owner fail.

Existing accounts remain approved to preserve active monitoring. New self-registrations are pending approval. Approval leaves entries paused. Suspending a user cancels pending entries and blocks new ones while retaining monitoring of open positions. Owner can see paper P&L, all filled positions and paginated signal activity, but not credential values.

The supervisor scans every five seconds and starts approved/enabled accounts with saved connections and channels, up to 20 concurrent workers (`ORION_MAX_WORKERS`, max 100). Paused accounts with no open positions stop automatically so credentials/channel discovery can be changed without terminal access. Paused or suspended accounts with open positions retain a worker. Broker sessions are reused when accepted; this does not promise permanent authentication or bypass broker requirements.

## Test before real users

- Register a second account; verify pending approval blocks credentials, entries and owner endpoints.
- Approve it from Owner. Sign in as that user and connect their own Telegram and Kotak Neo accounts. Pause entries before configuration. Telegram may request a login code and two-step password; Kotak needs a fresh TOTP when verifying.
- Load channels, select instruments per channel, save risk limits and virtual cash, then enable paper entries. Check worker state and catalogue readiness. A green sign-in result alone does not prove an active quote feed.
- Verify a fresh, eligible channel signal progresses to a paper fill when its entry range is reached. Rejected/commentary messages should remain in Activity with their reasons.
- Compare the user's trades and P&L to Owner. Verify another regular user cannot see them.
- Pause entries; pending signals cancel, existing paper positions keep monitoring. Test suspension and approval. Verify Live is unavailable in UI and PUT `/api/mode` with `live` returns 409.
- Check quote freshness. Open P&L with stale marks is an estimate; missing marks display Unavailable. Realized P&L includes simulation fees; future exit fees are excluded from open P&L.

The 13 historical trades are not replayed or rewritten by this release. No live orders are placed by any test.

## Install on a phone

The checked-in app source and exported bundles are not a signed installable iPhone app. Signing and a reachable secure API are still required. Your Mac's `127.0.0.1` SSM tunnel is not reachable as the same address from an iPhone. Keep the API private until secure access is deliberately configured; do not expose port 8000 directly.

Once a trusted HTTPS API endpoint is available, set `ORION_PORTAL_ORIGIN` to that exact origin on the server. Build with `EXPO_PUBLIC_API_URL` set to the same origin (this is a public URL, not a secret). Native tokens are stored in SecureStore and expire after eight hours; a new app sign-in is separate from broker authentication. Browser access uses HttpOnly cookies and CSRF, not localStorage.

```bash
cd mobile
npm ci
npm run typecheck
npm run lint
npx expo export --platform all
npx eas-cli@latest login
npx eas-cli@latest build:configure
# Configure EXPO_PUBLIC_API_URL in the chosen EAS build environment.
npx eas-cli@latest build --platform ios --profile preview
```

EAS will require your Expo project/account and Apple signing setup/device registration for an internal iOS build. For TestFlight use the production profile, then `npx eas-cli@latest submit --platform ios`. Android preview builds produce an APK. Replace the provisional bundle identifier `com.orionpaper.app` if unavailable before the first signed build. Do not commit signing keys, OTPs, passwords or broker secrets.

## Scope and remaining release gates

This is an invite-approved paper pilot, not an App Store submission. Apple/Expo signing, secure mobile API access, physical-device verification and an end-to-end test with real Telegram/Kotak sessions remain release gates. Public onboarding also needs privacy/support/account-deletion flows before App Store distribution. Admin password reset remains an operator command in this pilot. No background monitoring runs on the phone: server workers must remain healthy.

## Kotak attention alerts

The new `orion-attention` service monitors worker health independently of the supervisor. It creates an in-app incident when an enabled account (or an account with open paper positions) has missing Kotak authentication, a disconnected/reconnecting feed, or an offline worker. The signed mobile app offers **Account → Phone alerts → Enable alerts on this phone**. Owners who opt in also receive generic alerts for other accounts, without names, balances or credentials in lock-screen messages.

Missing-auth pushes debounce for 30 seconds; feed/offline pushes for 90 seconds, plus the monitor polling interval. Normal reconnects are not treated as confirmed credential expiry. Unresolved incidents repeat every 15 minutes and queued reminders are removed on recovery. Delivery uses an encrypted, per-device token, persisted queue, retry backoff and Expo receipts. A provider-accepted receipt does not prove that the person saw the message. Invalid devices are removed. Registration lasts 30 days; enable again to renew. Logging out removes devices tied to that login, including an expired native login. A password reset revokes all devices for the account.

Before enabling phone notifications in a release:
- Configure the EAS project ID plus APNs/FCM push credentials and allow notifications on the phone. This needs a rebuilt signed app; the browser displays incidents but does not send OS push notifications.
- Allow outbound HTTPS from the alert service to `exp.host`. If enhanced push security is enabled in Expo, set `EXPO_ACCESS_TOKEN` in the protected server environment, never in the client bundle.
- Check `sudo systemctl status orion-attention --no-pager`. The deployment installs and starts it alongside the supervisor.
- On a separate paper test account, verify a missing session and a stopped worker create a warning, one push after debounce, and no additional push before the reminder interval. Verify recovery clears the incident, logout removes the registration, and revoked phone tokens are retired. Test with the app closed on a physical phone. Automated tests mock the provider; they do not demonstrate real phone delivery.

Limits: this monitor cannot notify if the entire server is down, and it does not detect every silent quote-stream stall while the socket reports connected. A separate off-host availability monitor and market-session-aware stale-quote alerts are required before Live mode. Future Live mode must also verify broker-native protective orders, reconcile orders/positions after reconnect and remain blocked while connectivity is unverified. App alerts are not a substitute for those controls. Live remains disabled.

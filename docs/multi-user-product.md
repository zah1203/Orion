# Orion multi-user paper workspace

Users create an Orion account and sign in, link their own Telegram account, choose accessible channels, connect their own Kotak Neo feed and configure virtual capital and risk. Account creation starts paused with no channels or credentials. Existing accounts and ledgers remain unchanged.

This release adds self-service registration and a responsive dark workspace with setup status. Registration uses the existing password hashing, per-user storage, encrypted credentials, origin checks and account isolation. Registration attempts are limited to five per source IP per fifteen minutes. Behind the existing private tunnel, users may share this quota.

Kotak Neo is the only implemented broker. Dhan is planned; its credentials, authentication, instruments and quote adapter must be implemented and tested before it becomes selectable. No real-order execution is exposed.

## Remaining work before a public mobile release

- Provision and supervise an isolated worker for each newly registered account. Registration does not currently start a worker; an operator must provision the existing per-user service.
- Add password recovery and account deletion, production abuse controls and onboarding error recovery.
- Generalize the current two channel profiles to multiple channel selections with explicit supported formats.
- Attribute simulated fills, costs, skips and performance to channels; keep provider target claims separate from observed execution.
- Build the downloadable native client against a secured API. The responsive workspace is not an App Store app.
- Define a broker adapter contract around authentication, instruments and quotes before adding Dhan. Keep future real-money authorization separate from paper mode.

One linked Telegram identity and one Kotak connection are currently supported per Orion user. Users can choose channels accessible to that identity. Supporting multiple Telegram identities within one user requires additional session and worker ownership design.

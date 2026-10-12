# Bounded Live session authorization

This implementation connects the activated account worker to the entry coordinator.
The release gate remains closed. No repository variable, worker-start action or
pilot consent bypasses it. All submitted-order tests use synthetic sessions.

## Account workflow after release acceptance

1. Review each account's limits, credentials, cash-only opening allocation and
   prior-day fees separately. An allocation is a spending ceiling, not verified
   spendable broker cash. Complete read-only broker and contract checks.
2. Start the account's monitoring worker using its own one-time authentication.
   Paper must be disabled and flat for that account; other Paper workers continue.
3. While flat, the worker checks each newly received signal once against fresh
   market status, contract quotes, risk limits and margin. Account → Live entry
   checks displays the outcome and a fixed failure stage, never raw broker output.
   A read-only check neither reserves capital nor proves an order would succeed.
4. After reviewed release acceptance, the account owner can authorize automatic
   entries for the current India trading day with the current reviewed limits.
   Fresh flat reconciliation, reviewed cash, a connected source and no outstanding
   commands or incidents are required. Each entry still passes all risk checks.
5. Permission is bound to the account, policy, worker session and India date.
   Explicit pause, manual cancel/exit, stop, restart, policy changes, midnight,
   source failure or a blocking entry/monitoring failure end automatic authority.
   Old one-entry permissions are not upgraded: fresh session consent is required.
6. A working entry has a 30-second window before cancellation; partial fills are
   cancelled and protected. After a confirmed, reconciled flat outcome, the same
   session grant can admit another signal without another approval. Unknown
   commands, pending manual actions and incidents block continuation. Every send
   retains its durable, single-attempt journal; an uncertain send is never retried.

**Scope remains sequential positions only.** Same-day sale proceeds are not reused.
Opening allocation is a ceiling, not verified broker spendable funds. This does
not reproduce Paper's concurrent positions, capital reuse or full instrument
coverage. Full unattended production acceptance remains outstanding. Session
permission does not remove broker authentication or daily cash review.

The owner cannot authorize another account through the own-account endpoint.
Refreshing the page does not authorize or resend anything. No production limits
are configured by this change. Example premium/risk/fee values are not user consent.

## Deployment and alerts

Use the repository's full deployment workflow in an agreed maintenance window.
It installs the supervisor activation socket and the independent
`orion-live-notifications.service`; portal-only deployment does not install these.
Full deployment restarts Paper services, so do not run it during active Paper work.
Reserved Live slots block deployment until reviewed flat handover completes.

The independent alert runner checks existing Live ledgers and reserved workers.
A missing/stale heartbeat queues a deduplicated review alert. Delivery requires
that account's approved, registered, opted-in device. Verify actual device receipt
after deployment. A provider ticket is not delivery or proof someone read it.
Unknown sends are not automatically retried. Alerts never resume entries, log in
to the broker, initialize another account's ledger, or clear incidents.

## Evidence still required outside these tests

- Scheduled AWS service installation and verification, preserving Paper state.
- Each pilot's real broker account, contract mapping, static egress, cash-only
  allocation and fee reconciliation evidence.
- An actual AWS backup and isolated restore containing the Live ledgers and keys;
  previous Paper recovery success does not establish Live recovery.
- Opted-in device delivery and stale-worker alert acceptance.
- Reviewed release-gate change and explicitly supervised real-order acceptance,
  including partial fills, cancellation, protection, exit and reconciliation.

Do not change the gate merely because CI passes. Keep credentials, broker records
and private backup data outside Git. No AWS or broker acceptance is claimed here.

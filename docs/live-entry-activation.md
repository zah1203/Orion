# Supervised Live entry integration

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
4. After the release gate is opened through reviewed code, the account owner can
   explicitly authorize **one next Live entry** against the current policy.
   Authorization requires fresh, unchanged flat reconciliation, a connected signal
   source, a reviewed cash allocation, and no outstanding commands or incidents.
5. The permission is bound to the account, policy, worker session and India date.
   Pause, stop, policy changes, restart or midnight block it. It is consumed
   atomically with the entry intent before transport. A failed/uncertain attempt
   cannot reuse it. A working unfilled entry is cancelled after its 30-second
   permission window; partial fills are cancelled and protected immediately.
6. Broker acknowledgement is not a fill. Inspect reconciled fills, protection,
   cancellation and final flatness before considering another authorization.
   Manual cancel, minimum-price exit and pause retain their existing controls.

This is a supervised one-entry pilot, not unattended repeated entry authorization.
The owner cannot authorize another account through the own-account endpoint.
Refreshing the page does not authorize or resend anything.

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

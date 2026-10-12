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
   Initial consent requires fresh flat reconciliation, reviewed cash, a connected source and no outstanding
   commands or incidents are required. Each entry still passes all risk checks.
5. Permission is bound to the account, policy, worker session and India date.
   Explicit pause, manual cancel/exit, stop, restart, policy changes, midnight,
   source failure or a blocking entry/monitoring failure end automatic authority.
   Old one-entry permissions are not upgraded: fresh session consent is required.
6. A working entry has a 30-second window before cancellation; partial fills are
   cancelled and protected. Once all existing positions have reconciled stop coverage and no pending
   order changes, the same session grant can admit another signal without approval. Unknown
   commands, pending manual actions and incidents block continuation. Every send
   retains its durable, single-attempt journal; an uncertain send is never retried.

**Portfolio entries remain gated by release acceptance.** New cash reviews may recycle reconciled NSE option principal; existing opening-only reviews remain unchanged.
Opening allocation is a ceiling, not verified broker spendable funds. This does
not establish Paper-equivalent fills, frequency or full instrument coverage. Full unattended production acceptance remains outstanding. Session
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

## Five-value setup

The owner can save capital, per-trade risk, daily loss, maximum lots and maximum
entries under **Your Live risk settings**. These per-account preferences persist
in `accounts.db` and are included in its existing backups. They are planning
values only, not an applied trading policy. Saving does not enroll an account,
change policy consent or existing limits, initialize a Live ledger, or touch Paper.
The UI reloads the selected account's saved values and clearly labels them as
not applied. The separate advanced policy editor retains strict validation.

No extra premium cap, fee reserve, combined risk, stop gap or timeout is guessed.
Automatic derivation and execution-policy review remain implementation work before
these preferences can drive trading. Maximum lots and entries are ceilings;
available cash and remaining daily risk can still reduce or block a trade.

## Reconciled NSE option principal reuse

Kotak's official support guidance permits same-day option-sale premium to fund
new option buys within the same segment:
https://www.kotakneo.com/support/can-i-use-options-premium-received-to-open-futures-and-options-positions/
(accessed 2026-10-12). This rule is not certification of an API funds formula or
of this account's current spendable balance.

New daily cash consent explicitly opts into `nse-option-reconciled-v1`. Legacy
rows migrate to `opening-only` and cannot change model or allocation during that
India day. The new model requires an unchanged fresh reconciled book, confirmed
commands, no incidents, complete broker-provenance fill history and fresh marks for any open exposure.
Only same-day NSE NRML long options with unit multiplier qualify. The credit is
the lesser of actual sales and the reserved acquisition cost of sold units.
Losses and conservative acquisition-price differences remain deducted. Fee holds
remain deducted for each attempted entry; profits never expand opening capital.
Repeated reads recompute from unique fills rather than accumulating credits.

Admission still checks cash, margin shortfalls, risk and authorization independently.
`available_cash_verified` remains false. Tests use synthetic books/trades; actual
broker settlement behavior and production acceptance remain unverified. Additional positions require observed stop coverage and stable exit plans across
the portfolio, with no outstanding broker commands or manual requests. This change does not alter Paper.

## Concurrent position lifecycle

The activation worker uses one account-owned portfolio monitor. Each cycle first
reconciles a complete snapshot, handles manual requests and new/partial entries,
then manages each final position's independent target/trailing plan. A cycle sends
at most one new broker command. Position inspection rotates after the position
actually processed so a waiting target/cancel cannot starve subsequent positions.
Targets never borrow another position's stop quantity. Unknown commands remain
blocked; only terminal observations release protective capacity.

An additional BUY requires final existing entries, confirmed commands, no pending
manual actions, WATCH-phase exit plans and exact broker-observed stop coverage for
every unsold unit. Risk assessment aggregates premiums, remaining stop risk, fees,
losses, pending reservations and entry counts; fresh marks cover all open positions.
Legacy flat-only ledger callers remain strict. Existing sequential session grants
are not enlarged: the new portfolio consent phrase and scope require fresh review.
The normal initial daily authorization still requires a flat account.

Acceptance tests cover two positions on the same instrument, independent stops,
non-starving cancellation, one-command cycles, aggregate budget rejection and a
second synthetic BUY under an unchanged grant. These are local synthetic results,
not real fills, broker latency tests or AWS acceptance. Release remains closed.


### Applying the five-value policy

The owner can now apply the five values using the simple-policy action. It derives
maximum order premium and maximum total open premium from capital, and combined
planned open risk from daily loss. These are ceilings; existing cash, fee, loss,
whole-lot and per-trade risk checks still size or reject each entry. This does not
promise Paper returns or guarantee a stop fill.

Fee reserve, stop-limit gap and target timeout have no inferred production defaults.
The first application requires explicit reviewed values for these three execution
assumptions. Subsequent edits load the selected account's previously saved values;
these are not imported from another account or from Paper. Saving preferences alone
still does not apply a policy. Applying increments the policy version and clears
account review, exactly like the advanced policy route. It neither starts a worker
nor enables the release gate. Fee reserve remains an estimate, not verified charges.

Validation is local and synthetic. Broker acceptance, account enrollment/review,
Live recovery validation and production deployment remain outstanding.

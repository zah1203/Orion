# Account-owned Live monitoring activation

This release connects the Account screen to an explicit monitoring launcher. It
does **not** open the production BUY gate. Monitoring can cancel pending entries,
maintain protection and process confirmed manual exit requests for existing Live
exposure. It is not a read-only broker probe. A startup acknowledgement confirms
authentication and lease ownership, not successful reconciliation or readiness
to trade real money.

The integrated worker also performs flat-account [entry checks](live-entry-activation.md).
Real entries additionally require release acceptance and a separate one-entry
authorization from that account owner. Starting monitoring does not grant it.

## Deploy from GitHub

Merge the consolidated PR after checks pass. Use the existing GitHub release
workflow to stage the release. A portal-only deployment updates the screen but
does not update the running supervisor; it cannot install this launcher by itself.
The initial launcher rollout requires a **scheduled full deployment**, because
the supervisor and its systemd unit must both be updated. That existing full
deployment restarts Paper services. Do not perform it while preserving running
Paper workers is required: schedule the window, finish open/pending Paper trades,
pause entries explicitly, and verify the current private backup first. No worker
activation, service restart or AWS operation was performed during implementation.

No new AWS service, IAM permission or permanent broker key is required by the
launcher. It reuses the EC2 supervisor and the existing private encrypted account
store. `/run/orion-live/activation.sock` is owner-only and is not a public listener.
The supervisor runs at most two Live children; each child belongs to one account.

Subsequent deployments refuse any reserved Live slot before changing services or
release pointers. A shared/exclusive deployment lease prevents startup racing the
check. Complete verified-flat shutdown first. Staging remains available without
stopping workers. Old deployment scripts predating this guard are not safe for
an active Live account; do not roll back using those scripts.

## Start each account separately

1. The approved owner enrolls the pilot with explicit limits. Each signed-in
   account reviews its own current policy and saves its own Kotak and Telegram
   credentials and channels. Enrollment is limited to owner plus one account.
2. Arrange Paper handover for that account: entries already paused, no pending or
   open Paper positions, and its worker lease released. Startup never pauses
   Paper, cancels a Paper position, kills a worker or changes Paper capital.
3. Open **Account → Live pilot preparation → Your Live monitoring worker**.
   Enter a current TOTP, review startup and confirm starting with entries paused.
   The owner cannot start another user's worker through these routes.
4. Wait for a current heartbeat and inspect **Live health** for a matched fresh
   broker book. Review-required is not success. A source failure does not abandon
   existing exposure; protection monitoring continues when broker transport is
   still usable. Review the separate daily deposited-cash allocation next.
5. Repeat separately for the second account. Run the existing GitHub `live-drill`
   after both account ledgers exist with the complete schemas. Actual AWS restore
   evidence and actual broker acceptance remain required.

New Live ledgers are initialized only after the exclusive worker lease and the
policy/Paper checks, with entries paused and the account/UCC bound. Existing Live
ledgers are retained and cannot be rebound. Recovery quarantine is refused.

## Restart and uncertain outcomes

The TOTP travels through the authenticated, CSRF-protected request, a private Unix
socket and an anonymous child pipe. It is never saved to the database, disk,
process arguments, process environment or workflow logs. Saved long-lived
credentials remain encrypted in the existing account store. Broker child output
and core dumps are suppressed. Python strings cannot promise memory zeroization.

There is no automatic reauthentication or Live child restart. Portal restarts do
not own or stop these children; the supervisor owns them. Supervisor/host restart
ends them. A dead broker transport stops its unusable worker and leaves the slot
reserved. Existing command outcomes remain in the journal; a fresh login is not
permission to resend an uncertain order. The user must inspect account health and
broker orders, then explicitly supply a new TOTP. Fresh policy/credential review
is required if those changed. An unconfirmed startup response must not be blindly
retried: inspect health and the supervisor first. Duplicate starts are refused.

## Stop and return to Paper

**Stop monitoring only when verified flat** records a request. It does not force
close positions or interrupt protection. The worker releases its reservation only
after a current reconciliation, zero exposure, terminal intents/protective orders,
no unresolved commands, no pending manual requests and no incidents. Paper entries
remain paused after release; starting them again is a separate decision.

If authentication is lost, the reservation remains even after the process exits.
Authenticate explicitly, then request verified-flat stop again. If reconciliation
cannot establish a flat account, review the broker/ledger discrepancy. Do not
delete the reservation, rewrite order outcomes, remove worker locks or reset the
ledger to bypass it. A stale heartbeat is not evidence that exposure is flat.

## Validation boundaries

Automated tests use synthetic sessions: busy Paper lease, policy revocation,
account isolation/CSRF, first-ledger creation, preserved Paper bytes, stale/failed
startup, explicit flat stop, crash reservations, transport expiry, deployment
locking, private request framing and duplicate starts. The real Unix-socket
integration test skips only when the execution environment denies socket access;
it must run on Linux CI and the installed EC2 service must still be validated.

No actual Kotak login, order, EC2 worker startup, systemd deployment or AWS Live
restore was performed locally. The production order gate remains closed pending
the acceptance items in `live-consolidated-acceptance.md`.

## Reusing a staged release

The installer can reuse an inactive release directory only when every packaged
file matches the newly downloaded, checksum-verified archive. Missing, changed,
linked or unexpected source files and incomplete virtual environments are refused.
It does not overwrite the staged tree or reinstall its dependencies. It reruns
the on-host tests before any activation step. Already-active worker or portal
releases are refused rather than silently restarted.

Archive timestamps may differ between builds; file contents must match. If a
rebuilt web bundle differs, stop and review the mismatch; do not delete or rename
the old release to force activation. Deploy a reviewed new commit instead.
The initial pre-fix staged release can remain on disk unchanged. A full deployment
of the new main commit performs its own installation and tests before switching
services, so another staging run is optional.

Staging success is evidence of installation and on-host tests only. Full deployment
still requires a maintenance window and a current verified private backup. It
restarts Paper services, preserves application databases, and does not itself
authorize real-money entries. The Live-slot deployment guard remains in force.

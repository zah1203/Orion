# Manage Orion backups and teardown from GitHub

No local Terraform, CloudShell scripts, permanent AWS access keys or secrets in
GitHub are required. The workflows install the pinned tools on temporary GitHub
runners and use the existing main-branch OIDC role. Terraform state stays in the
existing private S3 state bucket, using separate application/recovery state keys.

**Merging this code does not activate backups or destroy anything.** All changes
require manually running the appropriate workflow from `main`. Scheduling is
opt-in. Existing workers and paper accounts are untouched by backup activation.
Application teardown, by contrast, intentionally terminates the application.

## One-time AWS permission prerequisite

The existing deployment role can use SSM and the state/release buckets, but cannot
create recovery buckets, secret containers or KMS keys. It cannot grant itself
those permissions. A one-time AWS administrator must approve a repository-owned
CloudFormation permission template through the AWS Console. No local build is
involved; no access key is created. This is the trust bootstrap, not a recurring
manual deployment step.

1. In GitHub **Settings → Secrets and variables → Actions → Variables**, add:
   - `BACKUP_BUCKET`: choose a globally unique bucket name, for example
     `zah1203-280498740572-orion-backups`. Do not use the existing state/release buckets.
   - Leave `BACKUP_MONITOR_ENABLED` unset until activation passes.
2. Existing variables remain `STATE_BUCKET`, `ARTIFACT_BUCKET`, `AWS_ROLE_ARN`,
   `INSTANCE_ID`, `INSTANCE_PROFILE`, `AMI_ID`, `SECRET_ARN`, and
   `PUBLIC_WEB_ENABLED`. This installation uses `INSTANCE_PROFILE=orion-india-ec2`,
   which is also its role name. Confirm that identity if adapting to another deployment.
3. Run **Prepare backup permissions** from `main`. It uploads
   `bootstrap/github-recovery-permissions.json` to your existing private artifact
   bucket and provides an S3 template URL in the job summary. It applies nothing.
4. Open **AWS Console → CloudFormation → Mumbai → Create stack → With new resources**.
   Choose the S3 template URL from the summary; name the stack
   `orion-github-recovery-permissions`. Set `BackupBucketName` to the same GitHub
   variable. Role defaults are `orion-india-github` and `orion-india-ec2`.
5. Review the IAM policy, acknowledge IAM capability and create the stack using an
   authorized administrative role. Wait for `CREATE_COMPLETE`.

The template grants administration only over the dedicated backup bucket,
`orion-india/recovery/*` secret containers, tagged recovery KMS keys, inline
policies on the existing Orion runtime role, and deletion of specifically tagged
retained Orion volumes. IAM cannot scope PutRolePolicy to one inline policy name;
therefore the runtime-role permission is a material trust boundary: protect main,
require reviews and restrict who can run workflows. It does not grant new access
to unrelated buckets/roles or permit deleting the state/release buckets.

The permission stack is deliberately separate from the infrastructure it enables.
An AWS administrator can revoke it from CloudFormation. Changing its permissions
requires another administrative review; the deployment role is not allowed to
self-expand its bootstrap policy.

## Provision recovery infrastructure

1. GitHub **Actions → Backup infrastructure → Run workflow**: branch `main`,
   action `plan`.
2. Review the complete job summary. Expect a dedicated S3 bucket and its security,
   versioning/lifecycle configuration, the runtime backup policy, one dedicated
   KMS key, and two Secrets Manager secret **containers**. No EC2 replacement or
   application changes should appear.
3. Run the same workflow with action `apply`; copy `plan_key` and `plan_sha256`
   exactly from that summary.

The binary plan is private in S3 and bound to the source commit, operation, stack,
input fingerprint and checksum. If main/variables/state change, make a new plan.
Terraform state contains identifiers and policies, **never key values**. This
adds charges for two Secrets Manager secrets and a customer-managed KMS key in
addition to backup storage. Review those resources in the plan before applying.

## Activate, escrow, back up and verify

Run **Backup operations → activate** from `main`. The workflow:

1. Reads only Terraform output metadata.
2. Uses SSM to install an immutable backup-only source bundle under
   `/opt/orion/backup/releases/<commit>` and backup service definitions. It does
   not run the application installer, change `/opt/orion/current`, or restart
   portal/supervisor/attention/trading services.
3. Briefly grants the EC2 runtime role permission to put/read the two escrow
   secrets and use their KMS key. The existing portal key is preserved. A new
   archive key is generated only if no local/escrowed archive key exists.
4. Escrows both keys directly from EC2 to Secrets Manager. Existing escrow values
   must match; mismatches fail instead of rotating/replacing keys. It verifies
   the saved **specific secret versions**. Values are never in Terraform state,
   SSM command parameters, GitHub output, Git, or the data bucket.
5. Creates a SQLite-safe backup and verifies S3 readback. It records non-secret
   object/secret version identifiers and checksums in `/var/backups/orion/latest.json`.
6. Removes temporary escrow permissions in a `finally` block. A separate
   `always()` workflow step retries revocation if a job fails/cancels. After
   normal activation the EC2 role has no escrow read/write/delete permission.
7. Downloads the exact S3 version onto the disposable runner, retrieves the exact
   escrowed key versions into private temporary files, and runs recovery in a
   **separate Linux network namespace with AWS credentials removed from its
   environment**. Only the offline verifier gets access to the decrypted data.
   The runner never starts a broker client, worker or application server.
8. Enables the daily timer **only after** the isolated restore succeeds. Saves
   non-secret evidence privately in the artifact bucket and links it in the summary.

The data archive and keys use distinct AWS storage resources. GitHub's trusted
recovery role can retrieve both to demonstrate recovery; the host has only its
necessary local keys after activation. Protect this role and workflow access.
The hosted runner temporarily holds private keys, plaintext snapshots and account
data; treat it as a trusted recovery environment. No data/keys are uploaded as
GitHub artifacts or cache. Temporary data is removed on normal exits; a terminated
runner must be discarded, not reused. The isolated verifier uses an unprivileged
UID after creating its network namespace, so temporary cleanup remains possible.

If a run is forcibly terminated while granting escrow permission, run **Backup
operations → revoke-escrow** and inspect the named inline policy
`orion-backup-escrow-bootstrap`. A GitHub outage/hard runner termination can prevent
`always()` cleanup; do not assume permissions were removed without a successful
cleanup step. That action can be run independently and does not stop workers.

An SSM timeout does not necessarily cancel the host command. Inspect its command
ID/status before retrying. Failed uploads retain local encrypted archives. Inspect
private host logs for failures; workflow logging intentionally suppresses raw SDK
and SSM error output to avoid exposing credentials. Activation failures never
report a completed disaster-recovery setup.

## Day-to-day controls (all GitHub)

| Backup operations action | Result |
|---|---|
| `status` | Fail if last verified upload is older than 26 hours or timer is disabled |
| `backup-drill` | Fresh backup, version-pinned download, escrow retrieval and isolated restore |
| `verify-latest` | Restore-check the recorded latest backup without changing source state |
| `disable` | Disable the backup timer only; never stop application workers |
| `revoke-escrow` | Remove any leftover temporary key-escrow permission |
| `activate` | Idempotent installation/escrow/backup/drill, then enable timer |

After activation succeeds, set `BACKUP_MONITOR_ENABLED=true` in GitHub Variables.
The daily 23:30 UTC workflow checks health after the 21:30 UTC backup window.
Enable GitHub Actions failure notifications in your GitHub notification settings.
This supplies a failed workflow signal, not an independently redundant paging
system. A missing/disabled GitHub schedule itself needs external monitoring if
strict availability is required. Run `backup-drill` monthly and after schema/key
changes. Counts/checksums are evidence; inspect representative recovered trades
privately when conducting an operational recovery review.

S3 keeps daily current objects 35 days, then noncurrent versions another seven
before asynchronous deletion. Daily local ciphertext is removed only after
verified upload. Source state is a series of consistent per-database snapshots,
not one atomic snapshot across all accounts. See [backup-recovery.md](backup-recovery.md)
for format, capacity, absent legacy files and recovery boundaries.

## Application teardown (keep recovery)

Do this only when you intend to shut down Orion. It ends running workers by
terminating their host; it is not part of ordinary backup activation.

1. **Orion teardown**: scope `application`, action `plan-destroy`, confirmation
   **`DESTROY ORION APPLICATION`**.
2. Review the plan and retained-data implications.
3. Run scope `application`, action `destroy`, with the same confirmation and exact
   `plan_key`/`plan_sha256`.

The workflow checks the saved plan and state lineage/serial before side effects,
then requires a **fresh backup and successful isolated restore** from that
instance. A failure blocks teardown. Only then does it disable EC2 termination
protection for that planned instance and apply the reviewed deletion plan. The
literal Terraform `prevent_destroy` guards remain in Git for normal provisioning;
only the temporary, explicitly confirmed destroy workspace relaxes them. On a
failed apply it attempts to restore termination protection on any remaining host.
Inspect AWS state after partial failure rather than assuming rollback succeeded.

Recovery bucket, escrowed keys and **retained root EBS volumes remain**. The job
summary supplies a private `retained-volumes.json` receipt. Partial teardown can
require a fresh plan and incident review; there is no unsafe skip-backup override.
After application destruction, set `BACKUP_MONITOR_ENABLED=false` to stop expected
stale-backup failures. Rebuilding the app does not automatically restore account
state or start recovered workers; promotion remains a reviewed recovery action.

For optional permanent removal of recorded root EBS volumes, run **Orion teardown**
with scope `application`, action `cleanup-volumes`, confirmation
**`DELETE RETAINED ORION VOLUMES`**, and the exact `volume_receipt` key. The script
requires the recorded original instance to be terminated, the same AWS account,
and each volume to be detached/available and explicitly tagged by teardown. It
never deletes arbitrary volumes supplied as inputs. EBS snapshots are not deleted.

## Separate recovery purge

This removes your recoverable data. It is never part of application teardown.

1. **Orion teardown**: scope `recovery`, action `plan-destroy`, confirmation
   **`DELETE ORION BACKUPS AND KEYS`**.
2. Review the destroy plan.
3. Run `destroy` with the same scope/confirmation and exact reviewed plan key/hash.

The workflow disables backup scheduling if the host still exists (or verifies it
was terminated), deletes **all versions and delete markers** from the dedicated
backup bucket, and destroys the reviewed recovery stack. Secret deletion has a
seven-day recovery window and KMS deletion a 30-day window; these are AWS-managed
pending deletions, not claims that the keys instantly vanished. Purged S3 data is
not recoverable through those key recovery windows. Existing local `/etc/orion`
key files, retained EBS volumes, private drill evidence and independently created
copies/snapshots are not silently erased by bucket teardown.

**Control-plane resources are retained:** the pre-existing Terraform state and
release/evidence buckets, GitHub OIDC provider/roles, instance profile, original
legacy credentials secret, and the CloudFormation permission stack. These were
bootstrapped separately and may be needed to rebuild or audit the service. The
new workflows intentionally cannot erase their own control plane or unrelated
snapshots. Fully closing the AWS project requires a separate reviewed retirement
of those bootstrap resources; do not interpret “application destroyed” as “every
AWS resource deleted.” Ordinary provisioning, backup activation/drills and
application/recovery teardown now run from GitHub without local tooling.

## Validation and limits

Tests exercise escrow mismatch rejection/idempotency, temporary permission cleanup,
version-pinned drill inputs, credential/environment isolation, unsafe receipt/plan
rejection, scope-specific destroy confirmations, protected control-plane buckets,
and source-only installer behavior. CI exercises recovery in an actual network
namespace and validates both Terraform stacks. Infrastructure operations are
mocked locally; CI success does not prove live AWS IAM, SSM, key escrow or backup
activation. Record the actual successful workflow evidence after deployment.

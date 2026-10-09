# Paper application backup and recovery

## Scope and evidence

This implements recovery tooling; it does **not** demonstrate that production has
an off-host backup. At implementation time the repository was inspected at
`a20af93` (public HTTPS gateway change). No production host was accessed, no AWS
resources were applied, no keys were retrieved and no production backup or AWS
restore drill was run. Local tests use synthetic accounts and trades. Mock S3
readback tests are not AWS evidence. Record the first real run and drill below
before inviting additional users.

### Persistent inventory (repository defaults; confirm host overrides privately)

| Data | Location / protection | Recovery treatment |
|---|---|---|
| Users, PBKDF2 hashes, settings, roles/access, audit, sessions, attempts, worker/attention metadata | `/var/lib/orion/portal/accounts.db`, SQLite WAL | Online SQLite backup, integrity and foreign-key checks |
| Broker credentials, Telegram StringSession, broker feed sessions, push device tokens | Encrypted records in `accounts.db` | Validate all ciphertext with separately supplied portal key, verify account ownership where encoded; never print decrypted values |
| Paper state, open/pending/closed positions, deduplication, source events and audit | `portal/accounts/<32-hex-id>/paper.db`, SQLite WAL | All existing ledgers included; users without a ledger are valid; orphan ledgers fail backup |
| Key identity marker | `portal/key-check` | Include encrypted marker; validate portal key before declaring success |
| Shared catalogue/economics | `portal/contracts.json`, `portal/economics.json` | Stable file copies; fail if replaced during copy |
| Account/worker/auth/catalogue lock files, SQLite WAL/SHM/journal sidecars | Under portal | Never raw-copy: locks recreated; committed WAL included via SQLite backup API |
| Temporary catalogue downloads | `portal/catalogue-refresh-*` | Excluded, reproducible |
| Legacy single-user paper ledger | `/var/lib/orion/runtime/paper.db` | Include if present; absence explicitly recorded |
| Legacy Telethon SQLite authorization | `/var/lib/orion/telegram.session` | Online SQLite backup if present; very sensitive even though not encrypted by portal |
| Deployment configuration and legacy catalogue | `/etc/orion/{config.json,contracts.json,economics.json,portal.env,public-web.env,environment}` | Include existing allowlisted files inside encrypted archive; env files are data, never sourced by recovery |
| Portal Fernet key | `/etc/orion/portal.key` or `ORION_PORTAL_KEY_FILE` override | **Excluded** from data archive; separate escrow required |
| New archive encryption key | `/etc/orion/recovery.key` | **Excluded**; distinct from portal key; separate escrow required |
| Legacy broker/API credentials | AWS Secrets Manager ARN in `/etc/orion/environment` | Not fetched/exported by this tool; separately retain secret versions, ARN, permissions and recovery access |
| TOTP / in-flight authentication | `/run/orion`, process memory | Do not back up; reauthenticate after recovery |

Unknown portal files/directories and symlinks cause failure rather than silently
omitting data or accidentally copying a key. Update the allowlist and tests when
new persistent files are introduced. Configuration outside these defaults needs
an explicit inventory update; do not assume it was captured. Inspect systemd
`EnvironmentFile` paths and the configured data/key paths locally without posting
secret-containing env files to tickets or logs. `--portal`, `--runtime` and
`--config` select alternate roots; the legacy session is sought in runtime's
parent. All three source directories must exist. An absent legacy ledger is not
an error, but an operator must check it matches the expected inventory.

Existing `infra/main.tf` enables encrypted root EBS, retains it on termination,
and prevents Terraform destruction/API termination. `bootstrap/create_backend.py`
creates private, encrypted, versioned **Terraform state and release** buckets and
a Secrets Manager container. None of these provisions copies application data.
No scheduled EBS snapshots/AWS Backup plan appears in the inspected repository.
Actual AWS policies, snapshots, secret versions and host overrides remain unverified.

## Guarantees and boundaries

`python -m orion.recovery backup` opens source SQLite databases in read-only mode
and uses SQLite's online backup API, including committed WAL transactions. It
never stops a worker, checkpoints the source, toggles accounts, resets a ledger,
or imports a broker/worker. SQLite may create/update WAL shared-memory sidecars;
normal read locks can briefly contend with writers. Each database copy has a
60-second deadline (configurable). A busy/corrupt source fails the whole run.

Each DB is transactionally consistent. **This is not a global point-in-time
snapshot across all databases/configuration.** The encrypted manifest records
the capture window, per-file SHA-256, sizes, counts and optional absences. Account
IDs and file inventory are checked again after copying; changes fail for retry.
Settings or trades can change between individual snapshots. Reconcile recovered
account settings against ledger history before any future operational cutover.

The archive uses streaming AES-256-GCM with a fresh random 96-bit nonce and an
authenticated format header. The independent 32-byte recovery key uses Fernet's
URL-safe base64 file format. Both keys are required for full verification. Plain
snapshot/tar files exist temporarily in `--work-dir`: use an encrypted local
volume, owner-only directories, sufficient free disk (allow **6 times** the
snapshot size plus WAL growth), and no shared `/tmp`. Buffers are bounded; the
current S3 single-PUT path supports archives up to 5 GiB. Larger archives require
a reviewed multipart implementation. Failed runs preserve existing backups.
Temporary directories are removed on normal/error exits, but abrupt power loss
or SIGKILL can leave plaintext work files; inspect and remove them privately
before retry, never run cleanup while a backup/drill is active. Deletion is not
secure erasure; volume encryption and access control remain essential.

Successful local creation requires decrypting and checking the complete archive,
SQLite integrity, all encrypted records, and account/paper counts. An S3 upload
adds a transport checksum and verifies the **specific returned object version**
by downloading and hashing every byte. Upload success alone is not a restore drill.
Failures produce a nonzero exit and exception class, never credential values or
SDK tracebacks. Investigate paths/permissions, disk space, SQLite health, correct
keys and IAM using the runbook; do not turn on secret-bearing debug logging.

## Protect both keys separately before scheduling

1. Generate only the **new recovery key** on the host; never regenerate or replace
   the existing portal key:

   ```bash
   sudo /opt/orion/current/.venv/bin/python -m orion.recovery init-key --file /etc/orion/recovery.key
   ```

2. Escrow the existing `portal.key` and new `recovery.key` outside EC2 and outside
   the data bucket. Use an approved private vault or separate Secrets Manager
   secrets encrypted under a recovery-controlled KMS key. Restrict retrieval to
   designated recovery operators; the runtime role must not be able to delete or
   overwrite escrow. Do not put either key in Git, Terraform variables/state,
   shell arguments, CI artifacts, logs, chat or the archive. Transfer key files
   through an approved private channel; never `echo` their contents.
3. Record non-secret vault identifiers/version IDs and key fingerprints in the
   restricted operations record. Retrieve both escrowed versions into temporary
   owner-only files on the isolated drill host and use those files for the drill.
   A same-host key test does **not** prove escrow recovery. Keep older versions
   until every associated retained backup expires and its last drill succeeds.
4. Retain/recover the legacy Secrets Manager secret separately if that deployment
   is still used. No secret payload is fetched by this code.

`init-key` refuses to overwrite an existing file. Key files must be mode 600;
backup keys equal to portal keys are rejected.

## Optional AWS retention stack (not automatically applied)

`infra/backup/` is a separate root stack. It creates a dedicated private bucket,
SSE-S3 encryption (in addition to client encryption), versioning, TLS-only policy,
35-day current-object expiration under `daily/`, seven-day noncurrent expiration
and incomplete-upload cleanup. Unique names mean normal backups are never
replaced. The runtime role receives only put/get/version-read and bucket-safety
inspection permissions, **no delete or bucket administration permissions**.
This is versioned retention, not immutable Object Lock or cross-region DR; an
AWS administrator can still remove data/change lifecycle. Choose stronger
cross-account/immutable recovery separately if required.

Run with an authorized infrastructure operator identity. The existing GitHub
application deployment role is not assumed to have the S3/IAM administration
permissions needed for this stack. Use the existing private state bucket and a
new globally unique backup bucket; the stack's state key is separate:

```bash
terraform -chdir=infra/backup init -backend-config="bucket=YOUR_EXISTING_STATE_BUCKET"
terraform -chdir=infra/backup plan -var='bucket_name=YOUR_NEW_PRIVATE_BACKUP_BUCKET' -out=recovery.tfplan
# Review the plan: only the recovery bucket/configuration and named IAM policy.
terraform -chdir=infra/backup apply recovery.tfplan
```

Do not apply the main EC2 stack or redeploy/restart the application to activate
backups. No recovery resources are created by regular application deployment.
Review actual AWS bucket lifecycle/public-block/versioning/policy and runtime-role
permissions after apply; record the observed settings. The uploader checks public
access block, versioning and policy status, but does not prove escrow/lifecycle.

## First backup and isolated drill

On the existing host, after the reviewed code is available (it can be a separate
checkout; no release activation is necessary):

```bash
sudo install -d -m 700 /var/backups/orion /var/backups/orion/work /var/backups/orion/encrypted
sudo /opt/orion/current/.venv/bin/python -m orion.recovery backup \
  --backup-key-file /etc/orion/recovery.key --portal-key-file /etc/orion/portal.key \
  --output /var/backups/orion/encrypted --work-dir /var/backups/orion/work \
  --bucket YOUR_NEW_PRIVATE_BACKUP_BUCKET
```

Use the selected checkout's Python/module location if the current deployed release
does not yet contain recovery code. Capture the JSON receipt privately: timestamp,
SHA-256, bucket/key, **version ID**, counts, and application commit. Failed uploads
leave local encrypted backups; there is no successful off-host receipt.

On a **disposable isolated host** with outbound networking denied (no worker,
supervisor, attention, legacy Orion service or portal installed/enabled), download
the exact object version with a recovery operator identity:

```bash
umask 077
mkdir -p "$HOME/orion-drill" "$HOME/orion-drill/work"
chmod 700 "$HOME/orion-drill" "$HOME/orion-drill/work"
aws s3api get-object --region ap-south-1 --bucket YOUR_PRIVATE_BACKUP_BUCKET \
  --key daily/EXACT_BACKUP_NAME.orb --version-id EXACT_VERSION_ID \
  "$HOME/orion-drill/download.orb"
sha256sum "$HOME/orion-drill/download.orb"
# Compare against the creation/readback receipt, then remove AWS access/network.
python -m orion.recovery restore --backup "$HOME/orion-drill/download.orb" \
  --backup-key-file /PRIVATE/ESCROW_RETRIEVED_RECOVERY_KEY \
  --portal-key-file /PRIVATE/ESCROW_RETRIEVED_PORTAL_KEY \
  --work-dir "$HOME/orion-drill/work" --destination "$HOME/orion-drill/recovered"
```

The destination must not exist; production `/var/lib/orion` and `/etc/orion`
paths, original source descendants, links and existing destinations are rejected.
The restore never merges into an existing tree. Archive authentication precedes
extraction; paths, duplicate names and link entries are checked. No remote calls
or application services run during restore.

The report demonstrates account/credential/broker-token/push-token decryption,
SQLite integrity, counts and unchanged paper ledger SHA-256. Inspect cash,
open/pending positions, audit and source events via offline SQLite queries in the
recovered copy; compare expected account count and representative known trades
privately. Ciphertext validity does not prove the broker/Telegram will accept an
old session; reauthentication is expected after cutover. The automated tests also
compare concrete synthetic position values and decrypted credential values.

After verification, **only the restored** account control metadata is changed:
new entries off, settings forced to paper with live inputs off, web/broker/push
sessions and worker status cleared. `paper.db` bytes remain unchanged: pending
positions are not cancelled. Stored encrypted account credentials are retained.
`portal/RECOVERY_ONLY` prevents `Store` initialization, including worker/portal
startup, even if someone points those services at the recovery directory. Do not
remove that marker during a drill, source copied environment files, or run the
legacy CLI against the copied Telegram session. Keep all drill networking denied.
No key files are written into the recovered tree.

Actual operational promotion is a separate, explicitly reviewed incident action:
fence the old host first, restore onto a new host, reconcile ledger/configuration,
refresh approved catalogue/economics, retrieve keys privately, reauthenticate,
verify paper-only controls and deliberately remove the marker only when ready.
Never overwrite existing accounts or run two workers for the same recovered
account. This PR performs no promotion and cannot enable Live trading.

## Scheduling, retention and monitoring

After one verified off-host backup and isolated escrow-based drill, install the
opt-in units (the deployment installer deliberately does not enable them):

```bash
# Privately create /etc/orion/backup.env, mode 600, containing only:
# ORION_BACKUP_BUCKET=YOUR_NEW_PRIVATE_BACKUP_BUCKET
sudo install -m 644 scripts/orion-backup.service scripts/orion-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orion-backup.timer
systemctl list-timers orion-backup.timer
```

The timer runs at 21:30 UTC daily (03:00 IST the following day), with up to ten
minutes jitter and a catch-up after downtime. Successful scheduled uploads remove
only their newly created local ciphertext **after version-specific S3 readback**.
S3 lifecycle retains daily backups for at least 35 days; asynchronous expiration
and seven-day noncurrent retention can retain bytes longer. Failed uploads retain
local files for recovery/retry; monitor capacity and remove them manually only
after verifying a replacement off-host backup. Do not run wildcard deletion.

Target RPO is approximately 24 hours **only while daily runs succeed**; there is
no guaranteed RTO until timed drills establish one. Check `systemctl status
orion-backup.service`, its journal and the last successful receipt daily. Alert
through your existing monitoring when no verified upload arrives for 26 hours,
service failures occur or disk space is low. This PR supplies scheduling and
nonzero failure status, not an integrated alert delivery service. S3 receipt and
timer installation must not be confused with monitoring being configured.

Run a monthly version-pinned S3 restore drill and after key/schema changes. Remove
disposable recovery storage/keys under your private-data disposal policy. Keep
non-secret drill evidence (counts, hashes, version IDs and duration) in restricted
operations records, not account names, positions, credential payloads or archives
in CI/PR artifacts.

| Evidence | Initially |
|---|---|
| Local synthetic tests and round trip | 145 tests passed with pinned requirements, including 13 recovery tests |
| AWS stack applied and actual policy/lifecycle audited | Not verified |
| Production host inventory and expected optional absences | Not verified |
| Production backup and version-specific S3 readback | Not verified |
| Separately escrowed key retrieval | Not verified |
| Isolated restore from S3 using retrieved escrow versions | Not verified |
| Daily timer plus failure/staleness alerts operational | Not verified |

## Development validation

```bash
python -m unittest discover -s tests -p test_recovery.py -v
python -m unittest discover -s tests -v
terraform -chdir=infra/backup fmt -check
terraform -chdir=infra/backup init -backend=false -lockfile=readonly
terraform -chdir=infra/backup validate
```

Tests cover committed WAL, multiple users, actual open paper positions, the legacy
DB/session, wrong keys, archive corruption, malicious members, unknown files,
source symlinks, timeouts, destination refusal, no network, source state preserved,
restored service guard, and mocked S3 security/versioned readback failures. CI runs
these without AWS credentials or production data.

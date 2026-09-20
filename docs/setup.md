# Setup: private GitHub → Mumbai EC2

No credentials, channel sessions or cloud resources are included. Complete local replay first. Keep this a private repository. The following steps are deployment instructions, not actions already performed.

## 1. Publish the source

Extract the project and initialize your repository:

```bash
git init -b main
git add .
git commit -m "Add Orion India paper simulator and Mumbai infrastructure"
git remote add origin YOUR_PRIVATE_GITHUB_REPOSITORY_URL
git push -u origin main
```

Enable Actions and protect `main` with review and passing `Validate` checks. Do not give untrusted users write access: the main-branch OIDC role can change infrastructure and install executable code on the host. These workflows use major-version action tags; pin reviewed action commit SHAs in your repository before widening access. Workflow validation never receives cloud credentials on pull requests.

## 2. Bootstrap remote state once

Use an AWS identity authorized to create S3 buckets, the two IAM roles/instance profile/OIDC provider and the secret container. Prefer a dedicated AWS account for this project. The bootstrap deliberately gives the deployment role broad EC2 infrastructure actions within Mumbai; it is not restricted to only Orion EC2 resources. The runtime role can read only the named credentials secret and release objects, plus SSM management permissions. Bootstrap itself is an administrative operation.

Run **Show AWS OIDC subject** from `main` in GitHub Actions. Copy only its printed `sub`, not any JWT. This avoids guessing the subject format: GitHub repositories may use immutable owner/repository IDs. The workflows do not use a GitHub Environment; adding one changes the subject and needs a trust-policy update.

With Python dependencies installed and temporary local AWS credentials configured:

```bash
python bootstrap/create_backend.py --prefix YOUR_GLOBALLY_UNIQUE_PREFIX --oidc-subject 'EXACT_PRINTED_SUBJECT'
```

This creates versioned, encrypted, private state/artifact buckets, IAM roles and an empty Secrets Manager secret. It writes non-secret IDs to `bootstrap-outputs.json`; no Terraform state is created locally. Re-running updates these named policies and resources; do not use the same fixed role names for multiple independent installations. Keep the bootstrap resources after removing the app infrastructure so state history remains available.

Create repository Actions **variables** from the output:

| Variable | Source |
|---|---|
| STATE_BUCKET | bootstrap output |
| ARTIFACT_BUCKET | bootstrap output |
| AWS_ROLE_ARN | bootstrap output |
| SECRET_ARN | bootstrap output; secret identifier only |
| INSTANCE_PROFILE | bootstrap output |
| AMI_ID | Verified official Canonical Ubuntu 24.04 x86_64 AMI in Mumbai |
| INSTANCE_ID | Add after Terraform apply |

Find an AMI with the AWS console's verified Canonical publisher, or inspect this AWS CLI result and pin the chosen ID:

```bash
aws ec2 describe-images --region ap-south-1 --owners 099720109477 --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*' 'Name=state,Values=available' --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name,CreationDate]' --output table
```

Do not store AWS access keys in GitHub: workflows obtain temporary credentials through OIDC. IAM propagation can take a short time after bootstrap; if the first role assumption fails, inspect trust claims before retrying.

## 3. Plan, review, then apply

Run **Infrastructure** with action `plan`, from `main`. Inspect the full plan in its job summary, then run the same workflow with action `apply`, copying the printed `plan_key` and `plan_sha256`. The saved binary must belong to the exact current commit and match its checksum. If main changed or Terraform says the plan is stale, create a new plan and review it. Do not use a plan from a different commit.

The backend bucket must exist before `terraform init`. Every workflow initializes the same S3 backend. Native `.tflock` locking and S3 versioning address your lost-local-state problem. An optional local plan uses the same backend:

```bash
terraform -chdir=infra init -backend-config="bucket=YOUR_STATE_BUCKET"
```

Supply the variables in `infra/terraform.tfvars.example` privately or via `TF_VAR_...`. Never put broker secrets in Terraform. Do not use `-backend=false` for real plan/apply; it is used only for offline validation. Never force-unlock until verifying that no apply is running.

Apply outputs `instance_id` and `kotak_whitelist_ip`. Set repository `INSTANCE_ID`. Add the Elastic IP to the Kotak Neo application's API IP settings as required by your account. EC2 is protected against Terraform destruction and API termination; removing it requires a deliberate code/lifecycle change. Encrypted root storage is retained on termination. EC2, EBS, public IPv4, S3 and Secrets Manager incur AWS charges; this is not a free-tier claim.

## 4. Install the app

Wait until the instance appears Online in AWS Systems Manager. Run **Deploy paper application** on main. It uploads a checksum-verified archive, installs Python dependencies in a per-release virtualenv and runs tests on the host. It preserves `/etc/orion` and `/var/lib/orion`, and leaves the service stopped.

If deployment fails, inspect the SSM command output. A partially created release directory is not overwritten automatically; inspect/remove that failed directory before retrying the same commit. A successfully installed commit is also not reinstalled. Deployment can stop an existing paper session after new tests pass; schedule changes outside an active session.

Open an SSM Session Manager session. No inbound SSH port or SSH key is required. Useful host checks:

```bash
sudo cloud-init status --long
sudo systemctl status amazon-ssm-agent
sudo journalctl -u orion -n 50
```

Ubuntu may run SSM as a snap; in that case inspect `snap.amazon-ssm-agent.amazon-ssm-agent.service` instead.

## 5. Configure credentials and channels privately

Populate the existing AWS Secrets Manager secret using its console JSON editor and the key names in `config/credentials.example.json`. Include your Telegram API ID/hash and Kotak consumer token, registered mobile, UCC and MPIN. Do not paste values into chat, GitHub, Terraform or shell arguments. TOTP is entered separately at each start and is not stored in the persistent secret. Enable the required Neo API/TOTP setup and confirm your account's index and MCX market-data access.

Copy the paper template on the host:

```bash
sudo install -o root -g orion -m 640 /opt/orion/current/config/paper.json /etc/orion/config.json
```

Authenticate your Telegram account interactively on the EC2 host. Obtain the secret ARN from `/etc/orion/environment` (the file contains identifiers, not actual credentials):

```bash
sudo -u orion env AWS_REGION=ap-south-1 ORION_SECRET_ARN=YOUR_SECRET_ARN /opt/orion/current/.venv/bin/python -m orion telegram-login --session /var/lib/orion/telegram
sudo -u orion env AWS_REGION=ap-south-1 ORION_SECRET_ARN=YOUR_SECRET_ARN /opt/orion/current/.venv/bin/python /opt/orion/current/scripts/list_channels.py --session /var/lib/orion/telegram
```

Telethon prompts for phone/login code and optional Telegram 2FA. The session file grants account access; keep it host-local with owner-only permissions. Use only channels you can legitimately access and whose terms allow the intended use. The listener does not bypass channel restrictions.

Replace the two placeholder numeric IDs in `/etc/orion/config.json`. Restrict products to those you have verified; remove crude until its real message format is tested. Adjust paper risk limits deliberately. Set `live_inputs_enabled` to true only when prepared to consume actual messages/quotes; this still cannot submit orders.

## 6. Prepare the daily instrument shortlist

Follow [contracts.md](contracts.md), then install the verified file:

```bash
sudo install -o root -g orion -m 640 /tmp/contracts.json /etc/orion/contracts.json
```

The account-specific raw broker CSV mapping, premium multipliers and feed epoch must be checked during this setup. The code does not infer them from screenshots. Verify quotes for at least one chosen NSE option and each MCX product, with correct symbol/strike/expiry, before relying on paper outcomes. If the feed sends no market-open status or an unsupported timestamp epoch, the engine will not enter trades.

## 7. Start and monitor

```bash
sudo /opt/orion/current/.venv/bin/python /opt/orion/current/scripts/start_paper.py
sudo journalctl -u orion -f
```

The helper asks for a current Kotak TOTP without echoing it. The service consumes/deletes the short-lived `/run/orion/totp` file. A successful `systemctl start` alone does not prove authentication; inspect logs for fresh data and parsed calls. Feed/authentication failure does not trigger an automatic restart with an expired code.

At the start of each trading day: refresh verified contracts, inspect any simulated positions left open during outages, then authenticate and start. Pending signals are cancelled on restart. Open simulated positions persist and may exit only on the next valid open-market quote. There is no automatic morning scheduler or session renewal in this release. Watchdog logs `FEED_STALE`; no external alert delivery is configured.

Stop using `sudo systemctl stop orion`. **Stopping does not close a simulated position** and there are no broker orders to close. This release is never a real-position risk manager.

## 8. State, backup and recovery

Application state is `/var/lib/orion/runtime/paper.db`; this is separate from Terraform's S3 state. Source events and audit live in that SQLite database. It is single-host storage on encrypted EBS, not a managed multi-AZ database. EBS snapshots/backups are not provisioned automatically. Use SQLite's backup API or stop the service and copy all DB/WAL files consistently; protect backups as private operational data.

EC2 user-data changes can require instance replacement, which `prevent_destroy` blocks. Preserve application state/session and review any replacement explicitly. Terraform state locking does not back up application positions. Do not reset the database merely to fix connectivity.

For a code rollback, stop the service, point `/opt/orion/current` to a previously tested release, restore its service file, run daemon-reload, and start with fresh authentication. Only roll back when its state schema is compatible; otherwise restore a verified application backup. Do not roll back broker state based on this simulator.

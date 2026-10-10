"""SSM-installed backup agent. Never starts/stops application or trading workers."""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path("/var/backups/orion")
CONFIG = Path("/etc/orion/backup-operations.json")
PYTHON = "/opt/orion/current/.venv/bin/python"
AGENT = "/opt/orion/backup/current"
FAILURE_CODES = (
    "runtime-dependencies", "backup-key-validation", "backup-snapshot-verification",
    "backup-s3-upload-readback", "backup-local-cleanup", "backup-subprocess",
)


SAFE_REASONS = ('accounts-changed-during-backup-retry', 'backup-checksum-mismatch', 'backup-exceeds-single-put-limit-do-not-delete-local-backup', 'backup-key-must-be-separate-from-the-portal-key', 'backup-manifest-mismatch', 'backup-work-directories-must-be-outside-source-roots', 'credential-ownership-structure-mismatch', 'directory-must-be-owner-only', 'expected-a-regular-file-symlinks-are-forbidden', 'invalid-account-identity', 'invalid-backup-name-or-s3-prefix', 'inventory-changed-during-backup-retry', 'key-file-must-be-owner-only', 'local-cleanup-requires-s3-upload', 'only-encrypted-orion-backups-may-be-uploaded', 'orphan-paper-ledger-reconcile-inventory-before-backup', 'paper-ledger-changed-during-restore', 'paper-ledger-has-no-state', 'portal-key-marker-mismatch', 'production-restore-paths-are-forbidden', 'public-bucket-policy-rejected', 'push-token-ownership-mismatch', 'recovery-inventory-mismatch', 'required-portal-state-missing', 'restore-destination-must-not-exist', 'restore-overlaps-original-source', 'restore-parent-must-be-an-existing-private-directory', 's3-bucket-must-block-all-public-access', 's3-readback-checksum-mismatch', 's3-versioning-is-required', 'sqlite-backup-deadline-exceeded-retry-later', 'sqlite-foreign-key-check-failed', 'sqlite-integrity-check-failed', 'source-file-changed-during-backup-retry', 'source-roots-must-be-existing-non-symlink-directories', 'symlink-directory-rejected', 'symlink-in-portal-state', 'truncated-backup', 'unexpected-archive-root', 'unknown-backup-format', 'unknown-directory-in-portal-state-review-inventory', 'unknown-file-in-portal-state-review-inventory', 'unsafe-archive-member', 'upload-did-not-return-a-version-id', 'sqlite-schema', 'sqlite-busy', 'sqlite-error', 'missing-file', 'permission-denied', 'disk-full', 'invalid-ciphertext', 'invalid-json', 'unexpected-data-shape', 'unknown')


class SafeHostFailure(RuntimeError):
    def __init__(self, stage, reason="unknown"):
        self.stage = stage if stage in FAILURE_CODES else "backup-subprocess"
        self.reason = reason if reason in SAFE_REASONS else "unknown"
        super().__init__(self.stage)



def checked_process(stage, command, **kwargs):
    try:
        return subprocess.run(command, check=True, capture_output=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        code = stage
        reason = "unknown"
        if stage == "backup-subprocess":
            # Match only fixed markers; never forward raw child output or arguments.
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            for line in stderr.splitlines():
                if line.startswith("ORION_RECOVERY_REASON="):
                    candidate_reason = line.removeprefix("ORION_RECOVERY_REASON=")
                    if candidate_reason in SAFE_REASONS:
                        reason = candidate_reason
                for candidate in FAILURE_CODES:
                    if line == "ORION_RECOVERY_STAGE=" + candidate.removeprefix("backup-"):
                        code = candidate
        raise SafeHostFailure(code, reason) from None



def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        os.chmod(stream.name, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
        temp = stream.name
    os.replace(temp, path)


def read_key(path):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Key must be a private regular file")
    key = path.read_bytes().strip()
    if len(base64.urlsafe_b64decode(key)) != 32:
        raise ValueError("Invalid encryption key")
    return key


def escrow(client, secret, key):
    """Idempotent first escrow. Existing values must match, never rotate implicitly."""
    metadata = client.describe_secret(SecretId=secret)
    versions = metadata.get("VersionIdsToStages", {})
    current = [version for version, stages in versions.items() if "AWSCURRENT" in stages]
    if versions and len(current) != 1:
        raise ValueError("Escrow has versions without one current version; operator review required")
    if current:
        result = client.get_secret_value(SecretId=secret, VersionId=current[0])
        stored = result.get("SecretBinary")
        if not isinstance(stored, bytes) or not hmac.compare_digest(stored, key):
            raise ValueError("Escrow mismatch; never overwrite existing key")
        return current[0]
    token = hashlib.sha256(key).hexdigest()
    result = client.put_secret_value(SecretId=secret, ClientRequestToken=token, SecretBinary=key)
    check = client.get_secret_value(SecretId=secret, VersionId=result["VersionId"])
    if not hmac.compare_digest(check["SecretBinary"], key):
        raise ValueError("Escrow verification failed")
    return result["VersionId"]


def backup(config):
    result = checked_process("backup-subprocess", [
        PYTHON, AGENT + "/orion/recovery.py", "backup", "--backup-key-file", "/etc/orion/recovery.key",
        "--portal-key-file", "/etc/orion/portal.key", "--output", str(ROOT / "encrypted"),
        "--work-dir", str(ROOT / "work"), "--bucket", config["backup_bucket"], "--remove-local-after-upload",
    ], text=True, timeout=1500)
    receipt = json.loads(result.stdout)
    if not receipt.get("s3", {}).get("ciphertext_readback_verified"):
        raise ValueError("No verified upload receipt")
    receipt.update(created_at=datetime.now(timezone.utc).isoformat(), key_versions=config["key_versions"],
                   portal_key_secret=config["portal_key_secret"], archive_key_secret=config["archive_key_secret"])
    atomic_json(ROOT / "latest.json", receipt)
    return receipt


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("activate", "backup", "status", "latest", "enable", "disable"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--verified-version")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / "operations.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "activate":
            import boto3
            config = json.loads(args.config.read_text())
            # The current release must already supply the runtime dependencies.
            checked_process("runtime-dependencies", [PYTHON, "-c", "import cryptography,boto3"])
            portal_key = read_key(Path("/etc/orion/portal.key"))
            client = boto3.client("secretsmanager", region_name="ap-south-1")
            recovery_path = Path("/etc/orion/recovery.key")
            if not recovery_path.exists():
                # Reuse an escrowed archive key when rebuilding the host; never silently rotate it.
                metadata = client.describe_secret(SecretId=config["archive_key_secret"])
                if metadata.get("VersionIdsToStages"):
                    archive_key = client.get_secret_value(SecretId=config["archive_key_secret"])["SecretBinary"]
                else:
                    archive_key = base64.urlsafe_b64encode(os.urandom(32))
                with recovery_path.open("xb") as out:
                    out.write(archive_key + b"\n")
            archive_key = read_key(recovery_path)
            if hmac.compare_digest(portal_key, archive_key):
                raise ValueError("Portal and archive keys must differ")
            config["key_versions"] = {
                "portal": escrow(client, config["portal_key_secret"], portal_key),
                "archive": escrow(client, config["archive_key_secret"], archive_key),
            }
            atomic_json(CONFIG, config)
            receipt = backup(config)
            print(json.dumps(receipt))
            return
        if args.action == "disable":
            subprocess.run(["systemctl", "disable", "--now", "orion-backup.timer"], check=True, capture_output=True)
            active = subprocess.run(["systemctl", "is-active", "orion-backup.service"], capture_output=True, text=True)
            if active.stdout.strip() in ("active", "activating", "deactivating"):
                raise ValueError("A backup is still active; wait before teardown")
            print(json.dumps({"timer_disabled": True}))
            return
        if args.action == "backup":
            print(json.dumps(backup(json.loads(CONFIG.read_text()))))
            return
        receipt = json.loads((ROOT / "latest.json").read_text())
        if args.action == "enable":
            if receipt["s3"]["version_id"] != args.verified_version:
                raise ValueError("Drill evidence does not match latest backup")
            subprocess.run(["systemctl", "enable", "--now", "orion-backup.timer"], check=True, capture_output=True)
            print(json.dumps({"timer_enabled": True, "verified_version": args.verified_version}))
        elif args.action == "status":
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(receipt["created_at"])).total_seconds()
            enabled = subprocess.run(["systemctl", "is-enabled", "orion-backup.timer"], capture_output=True).returncode == 0
            if age < 0 or age > 26 * 3600 or not enabled:
                raise ValueError("Backup missing/stale or timer disabled")
            print(json.dumps({"healthy": True, "age_seconds": round(age), "version_id": receipt["s3"]["version_id"]}))
        else:
            print(json.dumps(receipt))


if __name__ == "__main__":
    try:
        main()
    except SafeHostFailure as exc:
        code = str(exc) if str(exc) in FAILURE_CODES else "backup-subprocess"
        raise SystemExit("Backup host failed at " + code + " [" + exc.reason + "]; no secret output emitted.") from None
    except Exception as exc:
        raise SystemExit("Backup host operation failed: " + type(exc).__name__ + "; no secret output emitted.") from None

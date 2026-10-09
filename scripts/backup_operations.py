"""GitHub runner: immutable agent install, brief escrow grant and isolated recovery drill."""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

REGION = "ap-south-1"
POLICY = "orion-backup-escrow-bootstrap"


def temporary_policy(config):
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
         "Resource": [config["portal_key_secret"], config["archive_key_secret"]]},
        {"Effect": "Allow", "Action": ["kms:Decrypt", "kms:GenerateDataKey"], "Resource": config["escrow_kms_key"],
         "Condition": {"StringEquals": {"kms:ViaService": "secretsmanager.ap-south-1.amazonaws.com"}}},
    ]}


@contextmanager
def escrow_grant(iam, config):
    role = config["runtime_role_name"]
    # Caller has an always() cleanup step too, including cancelled workflow runs.
    iam.put_role_policy(RoleName=role, PolicyName=POLICY, PolicyDocument=json.dumps(temporary_policy(config)))
    try:
        yield
    finally:
        iam.delete_role_policy(RoleName=role, PolicyName=POLICY)


def send(ssm, instance, command):
    response = ssm.send_command(InstanceIds=[instance], DocumentName="AWS-RunShellScript",
                                Parameters={"commands": ["set -eu\numask 077\n" + command], "executionTimeout": ["1800"]},
                                TimeoutSeconds=1800)
    command_id = response["Command"]["CommandId"]
    print("SSM command:", command_id, flush=True)
    deadline = time.monotonic() + 1900
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] in ("Pending", "InProgress", "Delayed"):
            continue
        if result["Status"] != "Success":
            # Never relay SSM stderr/stdout blindly into GitHub logs.
            raise RuntimeError("SSM command failed; inspect private host journal")
        lines = result.get("StandardOutputContent", "").strip().splitlines()
        return json.loads(lines[-1])
    # A timeout does not cancel the remote command. Do not blindly retry.
    raise TimeoutError("Inspect SSM command before retrying")


def agent_command(action, *args):
    return shlex.join(["/opt/orion/current/.venv/bin/python", "/opt/orion/backup/current/scripts/backup_host.py", action, *args])


def installer(config, commit):
    """Embed only reviewed source and non-secret configuration in SSM parameters."""
    files = {name: Path(name).read_text() for name in ("orion/recovery.py", "scripts/backup_host.py")}
    service = Path("scripts/orion-backup.service").read_text()
    service = re.sub(r"^ExecStart=.*$", "ExecStart=" + agent_command("backup"), service, flags=re.M)
    service = service.replace("EnvironmentFile=/etc/orion/backup.env\n", "")
    service = service.replace("ConditionPathExists=/etc/orion/backup.env", "ConditionPathExists=/etc/orion/backup-operations.json")
    service = service.replace("WorkingDirectory=/opt/orion/current", "WorkingDirectory=/opt/orion/backup/current")
    files["scripts/orion-backup.service"] = service
    files["scripts/orion-backup.timer"] = Path("scripts/orion-backup.timer").read_text()
    payload = base64.b64encode(json.dumps({"files": files, "config": config, "commit": commit}).encode()).decode()
    # Existing commit directories must contain identical bytes. Symlink promotion changes only backup code.
    code = '''import base64,fcntl,json,os,pathlib,subprocess
p=json.loads(base64.b64decode(PAYLOAD))
root=pathlib.Path('/opt/orion/backup')
lockdir=pathlib.Path('/var/backups/orion')
lockdir.mkdir(parents=True,exist_ok=True,mode=0o700)
lease=(lockdir/'operations.lock').open('a')
fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
release=root/'releases'/p['commit']
release.mkdir(parents=True,exist_ok=True,mode=0o700)
for name,body in p['files'].items():
 target=release/name
 target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 if target.exists():
  if target.read_text()!=body: raise ValueError('Backup release mismatch')
 else:
  with target.open('x') as out: out.write(body)
  target.chmod(0o600)
for name in ('orion-backup.service','orion-backup.timer'):
 target=pathlib.Path('/etc/systemd/system')/name
 target.write_text(p['files']['scripts/'+name]); target.chmod(0o644)
config=release/'activation.json'
config.write_text(json.dumps(p['config']));config.chmod(0o600)
link=root/'next'
if link.is_symlink(): link.unlink()
link.symlink_to(release);os.replace(link,root/'current')
subprocess.run(['systemctl','daemon-reload'],check=True)
print(json.dumps({'installed':p['commit']}))
'''.replace("PAYLOAD", repr(payload))
    return "python3 - <<'ORION_INSTALL'\n" + code + "ORION_INSTALL\n"


def validate_receipt(receipt, config):
    s3 = receipt["s3"]
    if s3["bucket"] != config["backup_bucket"] or not re.fullmatch(r"daily/orion-\d{8}T\d{6}Z-[0-9a-f]{32}\.orb", s3["key"]):
        raise ValueError("Unexpected backup location")
    if not s3.get("version_id") or not re.fullmatch(r"[0-9a-f]{64}", s3["sha256"]):
        raise ValueError("Missing version/hash")
    for key in ("portal_key_secret", "archive_key_secret"):
        if receipt[key] != config[key]:
            raise ValueError("Unexpected key escrow identity")


def drill(s3, secrets, config, receipt):
    from orion.recovery import sha
    validate_receipt(receipt, config)
    with tempfile.TemporaryDirectory(prefix="orion-isolated-drill-") as tmp:
        root = Path(tmp)
        archive = root / "download.orb"
        item = receipt["s3"]
        s3.download_file(item["bucket"], item["key"], str(archive), ExtraArgs={"VersionId": item["version_id"]})
        archive.chmod(0o600)
        if sha(archive) != item["sha256"]:
            raise ValueError("Downloaded backup checksum mismatch")
        for label, arn in (("portal", config["portal_key_secret"]), ("archive", config["archive_key_secret"])):
            value = secrets.get_secret_value(SecretId=arn, VersionId=receipt["key_versions"][label])["SecretBinary"]
            path = root / (label + ".key")
            with path.open("xb") as out:
                os.chmod(path, 0o600)
                out.write(value)
        (root / "work").mkdir(mode=0o700)
        # A separate Linux network namespace has no network interfaces/routes except down loopback.
        # env -i removes AWS credentials and all other ambient job secrets from the verifier.
        command = ["sudo", "unshare", "--net", "--", "setpriv", "--reuid", str(os.getuid()), "--regid", str(os.getgid()), "--clear-groups",
                   "env", "-i", "PATH=/usr/bin:/bin",
                   sys.executable, str(Path("orion/recovery.py").resolve()), "restore", "--backup", str(archive),
                   "--backup-key-file", str(root / "archive.key"), "--portal-key-file", str(root / "portal.key"),
                   "--work-dir", str(root / "work"), "--destination", str(root / "restored")]
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=1200)
        report = json.loads(result.stdout)
        if not report.get("paper_ledgers_unchanged") or report.get("network_started") is not False:
            raise ValueError("Recovery evidence invalid")
        return {"isolated_restore_verified": True, "source": item, "key_versions": receipt["key_versions"],
                "counts": report["verified_before_sanitizing"], "verified_at": datetime.now(timezone.utc).isoformat()}


def main():
    import boto3
    os.umask(0o077)
    config = json.loads(Path(os.environ["BACKUP_OUTPUTS"]).read_text())
    action = os.environ["BACKUP_ACTION"]
    iam = boto3.client("iam")
    if action == "revoke-escrow":
        try:
            iam.delete_role_policy(RoleName=config["runtime_role_name"], PolicyName=POLICY)
        except iam.exceptions.NoSuchEntityException:
            pass
        return
    ssm, s3, secrets = [boto3.client(service, region_name=REGION) for service in ("ssm", "s3", "secretsmanager")]
    instance = os.environ["INSTANCE_ID"]
    if not re.fullmatch(r"i-[0-9a-f]+", instance):
        raise ValueError("Invalid instance ID")
    if action == "activate":
        commit = os.environ["GITHUB_SHA"]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Invalid release commit")
        send(ssm, instance, installer(config, commit))
        with escrow_grant(iam, config):
            # Allow IAM propagation; never automatically retry a timed-out SSM command.
            time.sleep(15)
            receipt = send(ssm, instance, agent_command("activate", "--config", "/opt/orion/backup/current/activation.json"))
        report = drill(s3, secrets, config, receipt)
        send(ssm, instance, agent_command("enable", "--verified-version", receipt["s3"]["version_id"]))
    elif action in ("backup-drill", "verify-latest"):
        receipt = send(ssm, instance, agent_command("backup" if action == "backup-drill" else "latest"))
        report = drill(s3, secrets, config, receipt)
    elif action in ("disable", "status"):
        # Permit recovery cleanup after deliberate application teardown.
        if action == "disable":
            ec2 = boto3.client("ec2", region_name=REGION)
            try:
                reservations = ec2.describe_instances(InstanceIds=[instance])["Reservations"]
            except ec2.exceptions.ClientError as exc:
                if exc.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                    raise
                reservations = []
            states = [i["State"]["Name"] for r in reservations for i in r["Instances"]]
            if not states or states == ["terminated"]:
                report = {"timer_disabled": True, "instance_terminated": True}
            else:
                report = send(ssm, instance, agent_command(action))
            Path("/tmp/orion-backup-disabled.json").write_text(json.dumps(report))
        else:
            report = send(ssm, instance, agent_command(action))
    else:
        raise ValueError("Unknown backup operation")
    report["instance_id"] = instance
    report["commit"] = os.environ["GITHUB_SHA"]
    if report.get("isolated_restore_verified"):
        Path("/tmp/orion-final-drill.json").write_text(json.dumps(report))
    key = f"releases/recovery-evidence/{os.environ['GITHUB_SHA']}/{os.environ['GITHUB_RUN_ID']}/{action}.json"
    s3.put_object(Bucket=os.environ["ARTIFACT_BUCKET"], Key=key, Body=json.dumps(report).encode())
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
        out.write(f"## Backup operation: {action}\n\nPrivate evidence: `{key}`\n\n```json\n{json.dumps(report, indent=2)}\n```\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit("Backup operation failed: " + type(exc).__name__ + ". Inspect SSM status; never blindly retry a timeout.") from None

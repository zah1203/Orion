"""Package tested source, publish a checksum-addressed archive, install via SSM."""

import base64
import hashlib
import os
from pathlib import Path
import re
import shlex
import tarfile
import tempfile
import time
import boto3

sha = os.environ["GITHUB_SHA"]
bucket = os.environ["ARTIFACT_BUCKET"]
instance = os.environ["INSTANCE_ID"]
if not re.fullmatch("[0-9a-f]{40}", sha) or not re.fullmatch("i-[0-9a-f]+", instance):
    raise SystemExit("Invalid deployment IDs")
s3 = boto3.client("s3", region_name="ap-south-1")
ssm = boto3.client("ssm", region_name="ap-south-1")
with tempfile.TemporaryDirectory() as tmp:
    archive = Path(tmp) / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for directory in ("orion", "scripts", "config", "tests", "examples", "docs"):
            for path in sorted(Path(directory).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    tar.add(path, arcname=str(path), recursive=False)
        for name in ("pyproject.toml", "requirements.txt"):
            tar.add(name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    key = f"releases/app/{sha}/{digest}.tar.gz"
    s3.upload_file(str(archive), bucket, key)
installer = base64.b64encode(Path("scripts/install_release.py").read_bytes()).decode()
args = shlex.join(["--bucket", bucket, "--key", key, "--sha256", digest, "--release", sha])
command = f"set -eu\ncloud-init status --wait\nprintf %s {shlex.quote(installer)} | base64 -d > /root/orion-install-release.py\n/opt/orion/deploy-venv/bin/python /root/orion-install-release.py {args}"
response = ssm.send_command(
    InstanceIds=[instance],
    DocumentName="AWS-RunShellScript",
    Parameters={"commands": [command], "executionTimeout": ["1200"]},
    TimeoutSeconds=1200,
)
id = response["Command"]["CommandId"]
print("SSM command:", id, flush=True)
for _ in range(260):
    time.sleep(5)
    try:
        result = ssm.get_command_invocation(CommandId=id, InstanceId=instance)
    except ssm.exceptions.InvocationDoesNotExist:
        continue
    if result["Status"] in ("Pending", "InProgress", "Delayed"):
        continue
    print(result.get("StandardOutputContent", ""))
    print(result.get("StandardErrorContent", ""))
    if result["Status"] != "Success":
        raise SystemExit("Installation failed: " + result["Status"])
    break
else:
    raise SystemExit("SSM polling timeout; inspect command before retrying")

"""Run as root through SSM. Install release, preserve state, leave service stopped."""

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import boto3

p = argparse.ArgumentParser()
p.add_argument("--bucket", required=True)
p.add_argument("--key", required=True)
p.add_argument("--sha256", required=True)
p.add_argument("--release", required=True)
a = p.parse_args()
if not re.fullmatch("[0-9a-f]{40}", a.release):
    raise SystemExit("Expected Git commit")
subprocess.run(["cloud-init", "status", "--wait"], check=True)
release = Path("/opt/orion/releases") / a.release
if release.exists():
    raise SystemExit("Release already exists; inspect it rather than overwriting")
with tempfile.TemporaryDirectory() as tmp:
    archive = Path(tmp) / "release.tar.gz"
    boto3.client("s3", region_name="ap-south-1").download_file(a.bucket, a.key, str(archive))
    if hashlib.sha256(archive.read_bytes()).hexdigest() != a.sha256:
        raise SystemExit("Checksum mismatch")
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            name = Path(m.name)
            if name.is_absolute() or ".." in name.parts or not (m.isfile() or m.isdir()):
                raise SystemExit("Unsafe archive member")
        release.mkdir()
        tar.extractall(release, filter="data")
subprocess.run(["python3", "-m", "venv", str(release / ".venv")], check=True)
python = str(release / ".venv/bin/python")
subprocess.run([python, "-m", "pip", "install", "-r", str(release / "requirements.txt")], check=True)
subprocess.run([python, "-m", "pip", "install", "--no-deps", str(release)], check=True)
subprocess.run([python, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=release, check=True)
# Only stop the old paper process after the new environment passes tests.
subprocess.run(["systemctl", "stop", "orion.service"], check=False)
subprocess.run(["systemctl", "stop", "orion-portal.service"], check=False)
subprocess.run(
    ["install", "-m", "644", str(release / "scripts/orion.service"), "/etc/systemd/system/orion.service"],
    check=True,
)
new = Path("/opt/orion/current.new")
new.unlink(missing_ok=True)
new.symlink_to(release)
new.replace("/opt/orion/current")
subprocess.run(
    [
        "install",
        "-m",
        "644",
        str(release / "scripts/orion-portal.service"),
        "/etc/systemd/system/orion-portal.service",
    ],
    check=True,
)
subprocess.run(["systemctl", "daemon-reload"], check=True)
print("Release installed. Service remains stopped; configure and authenticate before starting.")

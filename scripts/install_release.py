"""Run as root through SSM. Install release, preserve state, restore portal and supervise paper workers."""

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
portal_active = subprocess.run(["systemctl", "is-active", "--quiet", "orion-portal.service"]).returncode == 0
# Capture previously enabled legacy workers so they cannot race the supervisor on boot.
units = subprocess.run(["systemctl", "list-unit-files", "orion-worker@*.service", "--no-legend", "--no-pager"], capture_output=True, text=True, check=True).stdout
legacy = [line.split()[0] for line in units.splitlines() if line.split() and re.fullmatch(r"orion-worker@[a-zA-Z0-9_.\\-]+\.service", line.split()[0])]
subprocess.run(["systemctl", "stop", "orion-attention.service", "orion-supervisor.service"], check=False)
# Stop all account services before switching source/venv.
subprocess.run(["systemctl", "stop", "orion-worker@*.service"], check=False)
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
subprocess.run(
    [
        "install",
        "-m",
        "644",
        str(release / "scripts/orion-worker@.service"),
        "/etc/systemd/system/orion-worker@.service",
    ],
    check=True,
)
subprocess.run(["install", "-m", "644", str(release / "scripts/orion-supervisor.service"), "/etc/systemd/system/orion-supervisor.service"], check=True)
subprocess.run(["install", "-m", "644", str(release / "scripts/orion-attention.service"), "/etc/systemd/system/orion-attention.service"], check=True)
for unit in legacy:
    subprocess.run(["systemctl", "disable", unit], check=True)
subprocess.run(["systemctl", "daemon-reload"], check=True)
if Path("/etc/orion/portal.env").exists() and Path("/etc/orion/portal.key").exists():
    subprocess.run(["systemctl", "enable", "--now", "orion-supervisor.service", "orion-attention.service"], check=True)
if portal_active:
    subprocess.run(["systemctl", "start", "orion-portal.service"], check=True)
    import time
    import urllib.request
    from urllib.parse import urlsplit
    origin = "http://127.0.0.1:8000"
    for env_path in (Path("/etc/orion/portal.env"), Path("/etc/orion/public-web.env")):
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ORION_PORTAL_ORIGIN="):
                    origin = line.split("=", 1)[1].strip().strip("\"' ")
    health_request = urllib.request.Request("http://127.0.0.1:8000/", headers={"Host": urlsplit(origin).netloc})
    for attempt in range(30):
        try:
            with urllib.request.urlopen(health_request, timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit("Portal failed health check after deployment; inspect systemd logs")
print("Release installed. Paper worker supervisor enabled; previously running portal restored.")

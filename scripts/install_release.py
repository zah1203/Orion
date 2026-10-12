"""Run as root through SSM. Install release, preserve state, restore portal and supervise paper workers."""

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import boto3

def prepare_release(release, archive, *, promote_portal=False, run=subprocess.run,
                    current=Path("/opt/orion/current"),
                    override=Path("/etc/systemd/system/orion-portal.service.d/90-orion-release.conf")):
    """Verify reusable staged content, never overwrite it or reinstall its venv."""
    if any(p.is_symlink() for p in (release, *release.parents)):
        raise SystemExit("Linked release path refused")
    existing = release.exists()
    if existing and current.resolve() == release.resolve():
        raise SystemExit("Release is already active for workers; no services changed")
    if override.is_symlink():
        raise SystemExit("Linked portal override requires review")
    if existing and override.exists() and str(release) in override.read_text():
        managed = override.read_text().startswith("# Managed by Orion portal-only deployment\n")
        if not promote_portal or not managed:
            raise SystemExit("Release is already active; no services changed")
        # Full promotion may reuse the portal's immutable tree. The inventory and
        # tests below still run; no extraction or pip install touches active code.
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = set()
        for member in members:
            name = Path(member.name)
            if (name.is_absolute() or ".." in name.parts or not name.parts or
                    not (member.isfile() or member.isdir()) or str(name) in names or
                    name.parts[0] in (".venv", "build") or
                    any(part == "__pycache__" or part.endswith(".egg-info") for part in name.parts)):
                raise SystemExit("Unsafe archive member")
            names.add(str(name))
        if existing:
            expected = {str(Path(m.name)): m for m in members if m.isfile()}
            for name, member in expected.items():
                target = release/name
                if (any(p.is_symlink() for p in (target, *target.parents)) or
                        not target.is_file() or
                        target.read_bytes() != tar.extractfile(member).read()):
                    raise SystemExit("Existing release differs from verified archive; no files changed")
            # pip-generated outputs are outside the source inventory. Unexpected
            # source/assets, including importable bytecode, must not be reused.
            for target in release.rglob("*"):
                rel = target.relative_to(release)
                if rel.parts[0] in (".venv", "build") or rel.parts[0].endswith(".egg-info"):
                    continue
                if "__pycache__" in rel.parts:
                    continue
                if target.is_symlink() or (target.is_file() and str(rel) not in expected):
                    raise SystemExit("Unexpected existing release content; no files changed")
            if not (release/".venv/bin/python").is_file() or not (release/".venv/pyvenv.cfg").is_file():
                raise SystemExit("Staged virtual environment incomplete; no files changed")
        else:
            release.mkdir()
            tar.extractall(release, filter="data")
    python = str(release/".venv/bin/python")
    if not existing:
        run(["python3", "-m", "venv", str(release/".venv")], check=True)
        run([python, "-m", "pip", "install", "--quiet", "-r", str(release/"requirements.txt")], check=True)
        run([python, "-m", "pip", "install", "--quiet", "--no-deps", str(release)], check=True)
    # Re-run acceptance even for a matching staged tree. A previous partial
    # installation or failed test is never mistaken for a successful activation.
    run([python, "-m", "unittest", "discover", "-s", "tests"], cwd=release, check=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--mode", choices=("portal", "stage", "full"), default="portal")
    a = p.parse_args()
    if not re.fullmatch("[0-9a-f]{40}", a.release):
        raise SystemExit("Expected Git commit")
    subprocess.run(["cloud-init", "status", "--wait"], check=True)
    release = Path("/opt/orion/releases") / a.release
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "release.tar.gz"
        boto3.client("s3", region_name="ap-south-1").download_file(a.bucket, a.key, str(archive))
        if hashlib.sha256(archive.read_bytes()).hexdigest() != a.sha256:
            raise SystemExit("Checksum mismatch")
        prepare_release(release, archive, promote_portal=a.mode == "full")
    if a.mode == "stage":
        print("Release staged and tested; no services or active release pointers changed.")
        raise SystemExit(0)
    import sys
    sys.path.insert(0, str(release))
    if a.mode == "portal":
        # Import from the checksum-verified extracted release, not the SSM bootstrap.
        from scripts.portal_release import activate
        activate(a.release)
        raise SystemExit(0)
    from scripts.live_deploy_guard import acquire_deployment_lease
    # Retain the exclusive lease until this installer exits; no startup can race it.
    deployment_lease = acquire_deployment_lease()
    # Full deployments explicitly replace the managed portal-only override too.
    override = Path("/etc/systemd/system/orion-portal.service.d/90-orion-release.conf")
    if override.exists():
        if override.is_symlink() or not override.read_text().startswith("# Managed by Orion portal-only deployment\n"):
            raise SystemExit("Existing portal override requires review")
        override.unlink()
    # Only stop the old paper process after the new environment passes tests.
    portal_active = subprocess.run(["systemctl", "is-active", "--quiet", "orion-portal.service"]).returncode == 0
    # Capture previously enabled legacy workers so they cannot race the supervisor on boot.
    units = subprocess.run(["systemctl", "list-unit-files", "orion-worker@*.service", "--no-legend", "--no-pager"], capture_output=True, text=True, check=True).stdout
    legacy = [line.split()[0] for line in units.splitlines() if line.split() and re.fullmatch(r"orion-worker@[a-zA-Z0-9_.\\-]+\.service", line.split()[0])]
    subprocess.run(["systemctl", "stop", "orion-attention.service", "orion-live-notifications.service", "orion-supervisor.service"], check=False)
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
    subprocess.run(["install", "-m", "644", str(release / "scripts/orion-live-notifications.service"), "/etc/systemd/system/orion-live-notifications.service"], check=True)
    for unit in legacy:
        subprocess.run(["systemctl", "disable", unit], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    if Path("/etc/orion/portal.env").exists() and Path("/etc/orion/portal.key").exists():
        subprocess.run(["systemctl", "enable", "--now", "orion-supervisor.service", "orion-attention.service", "orion-live-notifications.service"], check=True)
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


if __name__ == "__main__":
    main()

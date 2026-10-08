"""Root-only, explicit activation after reviewed infrastructure apply. Never changes paper workers."""

import argparse
from ipaddress import ip_address, ip_network
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.request


def public_host(origin):
    match = re.fullmatch(r"https://([a-z0-9]{10}\.execute-api\.ap-south-1\.amazonaws\.com)", origin)
    if not match:
        raise ValueError("Use the exact public_web_url Terraform output, without a trailing slash")
    return match[1]


def proxy_config(origin, private_ip):
    host = public_host(origin)
    if ip_address(private_ip) not in ip_network("10.76.1.0/24"):
        raise ValueError("Expected the existing Orion instance private IPv4 address")
    return f"""# Managed by configure_public_web.py. No public listener or response cache.
limit_req_zone $binary_remote_addr zone=orion_web:10m rate=10r/s;
server {{
    listen {private_ip}:8080 default_server;
    server_name _;
    server_tokens off;
    access_log off;
    error_log /var/log/nginx/orion-error.log warn;
    client_max_body_size 32k;
    client_body_timeout 10s;
    client_header_timeout 10s;
    keepalive_timeout 15s;
    set_real_ip_from 10.76.2.0/24;
    set_real_ip_from 10.76.3.0/24;
    real_ip_header X-Orion-Viewer-IP;
    real_ip_recursive off;
    location = /_orion_health {{
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host {host};
    }}
    location / {{
        limit_req zone=orion_web burst=40 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host {host};
        proxy_set_header X-Orion-Viewer-IP $remote_addr;
        proxy_set_header X-Forwarded-For "";
        proxy_set_header Forwarded "";
        proxy_set_header Connection "";
        proxy_read_timeout 30s;
        proxy_cache off;
    }}
}}
"""


def activate(origin, private_ip):
    config = proxy_config(origin, private_ip)
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo on the Orion EC2 instance through SSM")
    os.umask(0o077)
    if private_ip not in subprocess.check_output(["hostname", "-I"], text=True).split():
        raise SystemExit("Private IP is not assigned to this host")
    release = Path("/opt/orion/current")
    if not (release / "orion/portal/mobile_web/index.html").is_file():
        raise SystemExit("Deploy the built mobile web UI before enabling public access")
    # Read-only preflight: never bootstrap an owner or silently create a new DB.
    with sqlite3.connect("file:/var/lib/orion/portal/accounts.db?mode=ro", uri=True) as db:
        if not db.execute("SELECT 1 FROM users WHERE role='owner' AND access='approved'").fetchone():
            raise SystemExit("Bootstrap your owner account before enabling public access")
    for path in ("/etc/orion/portal.env", "/etc/orion/portal.key"):
        if not Path(path).is_file():
            raise SystemExit("Missing existing portal configuration")
    subprocess.run(["apt-get", "update"], check=True)
    subprocess.run(["apt-get", "install", "-y", "nginx"], check=True)
    dropin = Path("/etc/systemd/system/orion-portal.service.d/30-public-web.conf")
    public_env = Path("/etc/orion/public-web.env")
    nginx = Path("/etc/nginx/conf.d/orion-public.conf")
    files = {
        dropin: "[Service]\nEnvironmentFile=/etc/orion/public-web.env\n",
        public_env: f"ORION_PORTAL_ORIGIN={origin}\nORION_TRUST_LOCAL_PROXY=1\n",
        nginx: config,
    }
    old = {path: path.read_bytes() if path.exists() else None for path in files}
    backup = Path("/var/backups/orion-public-web") / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    for path, content in old.items():
        if content is not None:
            (backup / path.name).write_bytes(content)
    was_active = subprocess.run(["systemctl", "is-active", "--quiet", "orion-portal"]).returncode == 0
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o600 if path == public_env else 0o644)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "restart", "orion-portal"], check=True)
        host = public_host(origin)
        for attempt in range(20):
            try:
                request = urllib.request.Request("http://127.0.0.1:8000/", headers={"Host": host})
                with urllib.request.urlopen(request, timeout=2) as response:
                    if response.status == 200 and response.headers.get("Strict-Transport-Security"):
                        break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Portal did not become ready")
        subprocess.run(["systemctl", "enable", "--now", "nginx", "orion-portal"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
    except Exception:
        for path, content in old.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "restart" if was_active else "stop", "orion-portal"], check=False)
        subprocess.run(["systemctl", "reload", "nginx"], check=False)
        raise
    print("Public proxy configured. Wait for ALB health checks, then verify:", origin)
    print("Workers were not restarted. Sign in as owner and test a pending account before sharing.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--private-ip", required=True)
    args = parser.parse_args()
    activate(args.origin, args.private_ip)

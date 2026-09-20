"""Interactive daily start; the TOTP is never passed as an argv or shell history value."""

from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import pwd
import subprocess
from zoneinfo import ZoneInfo

if os.geteuid() != 0:
    raise SystemExit("Run using sudo on the EC2 host")
config = json.loads(Path("/etc/orion/config.json").read_text())
master = json.loads(Path("/etc/orion/contracts.json").read_text())
if config["mode"] != "paper" or not config["live_inputs_enabled"]:
    raise SystemExit("Paper live-input config required")
if master.get("synthetic") or master["as_of"] != datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat():
    raise SystemExit("Refresh and verify the daily contract shortlist")
if not Path("/var/lib/orion/telegram.session").exists():
    raise SystemExit("Complete Telegram login first")
if subprocess.run(["systemctl", "is-active", "--quiet", "orion"]).returncode == 0:
    raise SystemExit("Already running; do not interrupt an active session")
code = getpass.getpass("Current Kotak TOTP: ")
if len(code) != 6 or not code.isdigit():
    raise SystemExit("Expected six digits")
user = pwd.getpwnam("orion")
path = Path("/run/orion/totp")
path.parent.mkdir(mode=0o700, exist_ok=True)
os.chown(path.parent, user.pw_uid, user.pw_gid)
os.umask(0o077)
path.write_text(code + "\n")
path.chmod(0o600)
os.chown(path, user.pw_uid, user.pw_gid)
subprocess.run(["systemctl", "reset-failed", "orion"], check=True)
try:
    subprocess.run(["systemctl", "start", "orion"], check=True)
except BaseException:
    path.unlink(missing_ok=True)
    raise
print("Start requested. Check journalctl -u orion -f for successful authentication and fresh quotes.")

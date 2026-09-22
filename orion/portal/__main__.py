"""Operator commands for the private pilot; never pass passwords/TOTP in argv."""

import argparse
import asyncio
import fcntl
import getpass
import json
import os
from pathlib import Path
import secrets
from cryptography.fernet import Fernet
from .app import defaults
from .store import Store


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description="Orion multi-user paper pilot operator")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("init-key")
    q.add_argument("--file", required=True)
    q = sub.add_parser("create-user")
    q.add_argument("--username", required=True)
    q = sub.add_parser("reset-password")
    q.add_argument("--username", required=True)
    q = sub.add_parser("telegram-login")
    q.add_argument("--username", required=True)
    q = sub.add_parser("worker")
    q.add_argument("--username", required=True)
    q.add_argument("--master", required=True)
    q.add_argument(
        "--background",
        action="store_true",
        help="Reuse encrypted broker session; never prompt; keep listening without broker",
    )
    a = p.parse_args()
    if a.command == "init-key":
        with open(a.file, "xb") as f:
            f.write(Fernet.generate_key() + b"\n")
        print("Created encryption key file. Back it up privately, separately from the database.")
        return
    key = Path(os.environ["ORION_PORTAL_KEY_FILE"])
    if key.stat().st_mode & 0o077:
        raise SystemExit("Key file must be owner-only")
    store = Store(os.environ["ORION_PORTAL_DATA"], key.read_bytes().strip())
    if a.command in ("create-user", "reset-password"):
        password = getpass.getpass("Password (12–128 characters): ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match")
        if not 12 <= len(password) <= 128:
            raise SystemExit("Password must be 12–128 characters")
        if a.command == "create-user":
            uid = store.create_user(a.username, password, defaults())
            print("Created account:", a.username, "ID:", uid)
        else:
            uid = store.by_username(a.username)
            salt = secrets.token_hex(16)
            with store.db() as db:
                db.execute(
                    "UPDATE users SET salt=?,password=? WHERE id=?",
                    (salt, store.password_hash(password, salt), uid),
                )
                db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            print("Password changed and browser sessions revoked.")
        return
    uid = store.by_username(a.username)
    # This lease prevents two feeds from driving one account and protects connection changes.
    with open(store.account_dir(uid) / "worker.lock", "a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("A worker/login operation already owns this account") from None
        creds = store.credentials(uid)
        if a.command == "telegram-login":
            from telethon.sync import TelegramClient
            from telethon.sessions import StringSession

            with TelegramClient(
                StringSession(creds.get("telegram_session", "")),
                int(creds["telegram_api_id"]),
                creds["telegram_api_hash"],
            ) as client:
                # Telethon prompts for phone, one-time code and optional 2FA locally.
                session = client.session.save()
            with store.lock(uid):
                store.save_credentials(uid, {"telegram_session": session})
            print("Telegram authorization encrypted and saved for", a.username)
            return
        from telethon.sessions import StringSession
        from ..runtime import serve

        missing = [
            k
            for k in (
                "telegram_session",
                "telegram_api_id",
                "telegram_api_hash",
                "kotak_consumer_key",
                "kotak_mobile",
                "kotak_ucc",
                "kotak_mpin",
            )
            if not creds.get(k)
        ]
        if missing:
            raise SystemExit("Missing account connection fields: " + ", ".join(missing))
        config = store.config(uid)
        if not config["channels"]:
            raise SystemExit("Configure channels first")
        config["live_inputs_enabled"] = True
        background = None
        code = None
        if a.background:
            from .background import Background

            background = Background(store, uid, a.master)
            master, _ = background.catalogue()
        else:
            master = json.loads(Path(a.master).read_text())
            code = getpass.getpass("Current Kotak TOTP for this user: ")
            if len(code) != 6 or not code.isdigit():
                raise SystemExit("Expected six digits")

        def current_config():
            store.heartbeat(uid)
            cfg = store.config(uid)
            cfg["live_inputs_enabled"] = True
            return cfg

        try:
            asyncio.run(
                serve(
                    config,
                    master,
                    str(store.account_dir(uid) / "paper.db"),
                    StringSession(creds["telegram_session"]),
                    credentials_override=creds,
                    totp_code=code,
                    config_provider=current_config,
                    account_lock=lambda: store.lock(uid),
                    background=background,
                )
            )
        except Exception as exc:
            if a.background:
                raise SystemExit("Background worker stopped: " + type(exc).__name__) from None
            raise
        finally:
            with store.db() as db:
                db.execute("DELETE FROM worker_status WHERE user_id=?", (uid,))


if __name__ == "__main__":
    main()

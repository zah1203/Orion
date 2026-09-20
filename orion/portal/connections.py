"""User-owned connection checks; no orders and no background trading worker."""

import asyncio
from contextlib import contextmanager, suppress
import fcntl
import hashlib
import json
import re
import sys
import time

from telethon import TelegramClient, errors
from telethon.sessions import StringSession


class ConnectionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@contextmanager
def connection_lease(store, uid):
    # Same lease as the CLI worker/login. Never wait on a running worker.
    with open(store.account_dir(uid) / "worker.lock", "a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConnectionError(
                "Stop the account worker or wait for the current connection check.", 409
            ) from None
        try:
            if store.user(uid)["enabled"]:
                raise ConnectionError("Pause paper entries before changing or validating connections.", 409)
            yield
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)


def telegram_client(creds, session):
    return TelegramClient(
        StringSession(session),
        int(creds["telegram_api_id"]),
        creds["telegram_api_hash"],
        timeout=10,
        request_retries=0,
        connection_retries=1,
        flood_sleep_threshold=0,
        receive_updates=False,
        device_model="Orion paper pilot",
    )


def fingerprint(creds):
    return hashlib.sha256(
        json.dumps([creds.get("telegram_api_id"), creds.get("telegram_api_hash")]).encode()
    ).hexdigest()


async def kotak_check(creds, totp):
    # Isolate SDK logging and bound the entire request, including SDK retries.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "orion.portal.kotak_check",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(json.dumps({"creds": creds, "totp": totp}).encode()), 40
        )
        if process.returncode != 0 or output.strip() != b"OK":
            raise ConnectionError(
                "Kotak authentication failed. Check the saved token, mobile, UCC, MPIN, server IP whitelist and fresh TOTP."
            )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class Connections:
    def __init__(self, store, client_factory=telegram_client, broker_check=kotak_check):
        self.store = store
        self.client_factory = client_factory
        self.broker_check = broker_check
        self.pending = {}

    def expire(self):
        for uid, item in list(self.pending.items()):
            if item["expires"] <= time.time():
                self.pending.pop(uid, None)

    def cancel(self, uid):
        self.pending.pop(uid, None)

    def limit(self, uid, action, limit):
        now = time.time()
        bucket = "connection:" + uid + ":" + action
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE at<?", (now - 900,))
            count = db.execute("SELECT count(*) FROM attempts WHERE bucket=?", (bucket,)).fetchone()[0]
            if count >= limit:
                raise ConnectionError("Too many attempts. Wait 15 minutes before trying again.", 429)
            db.execute("INSERT INTO attempts VALUES(?,?)", (bucket, now))

    def status(self, uid, owner):
        self.expire()
        values = self.store.credentials(uid)
        pending = self.pending.get(uid)
        return {
            "telegram": {
                "linked": bool(values.get("telegram_session")),
                "checked_at": values.get("telegram_checked_at"),
                "step": pending["step"] if pending and pending["owner"] == owner else "idle",
            },
            "kotak": {"checked_at": values.get("kotak_checked_at"), "worker_started": False},
        }

    def tg_credentials(self, uid):
        creds = self.store.credentials(uid)
        if not re.fullmatch(r"[1-9][0-9]{0,9}", creds.get("telegram_api_id", "")) or not re.fullmatch(
            r"[a-fA-F0-9]{32}", creds.get("telegram_api_hash", "")
        ):
            raise ConnectionError("Save a numeric Telegram API ID and a 32-character API hash first.")
        return creds

    async def telegram(self, uid, owner, action, value=""):
        self.expire()
        with connection_lease(self.store, uid):
            if action == "cancel":
                self.cancel(uid)
                return {"step": "idle"}
            self.limit(uid, "send" if action == "start" else "verify", 3 if action == "start" else 15)
            creds = self.tg_credentials(uid)
            pending = self.pending.get(uid)
            state = None
            if action in ("code", "password"):
                if not pending or pending["owner"] != owner or pending["step"] != action:
                    raise ConnectionError(
                        "This login step expired or belongs to another browser session. Start again.", 409
                    )
                state = json.loads(self.store.cipher.decrypt(pending["sealed"]))
                if state["fingerprint"] != fingerprint(creds):
                    self.cancel(uid)
                    raise ConnectionError("Telegram credentials changed. Start login again.", 409)
            if action == "start":
                if creds.get("telegram_session"):
                    raise ConnectionError(
                        "A session is already saved. Use Check connection & load channels.", 409
                    )
                self.cancel(uid)
            client = self.client_factory(
                creds,
                state["session"]
                if state
                else (creds.get("telegram_session", "") if action == "channels" else ""),
            )
            try:
                async with asyncio.timeout(30):
                    await client.connect()
                    if action == "start":
                        sent = await client.send_code_request(value)
                        state = {
                            "session": client.session.save(),
                            "phone": value,
                            "hash": sent.phone_code_hash,
                            "fingerprint": fingerprint(creds),
                        }
                        self.pending[uid] = {
                            "owner": owner,
                            "expires": time.time() + 300,
                            "step": "code",
                            "sealed": self.store.cipher.encrypt(json.dumps(state).encode()),
                        }
                        return {"step": "code"}
                    if action == "code":
                        try:
                            await client.sign_in(
                                phone=state["phone"], code=value, phone_code_hash=state["hash"]
                            )
                        except errors.SessionPasswordNeededError:
                            state["session"] = client.session.save()
                            pending["sealed"] = self.store.cipher.encrypt(json.dumps(state).encode())
                            pending["step"] = "password"
                            return {"step": "password"}
                    elif action == "password":
                        await client.sign_in(password=value)
                    if not await client.is_user_authorized():
                        self.store.save_credentials(
                            uid, {"telegram_session": None, "telegram_checked_at": None}
                        )
                        raise ConnectionError(
                            "Telegram session is no longer authorized. Connect Telegram again."
                        )
                    self.store.save_credentials(
                        uid, {"telegram_session": client.session.save(), "telegram_checked_at": time.time()}
                    )
                    self.cancel(uid)
                    if action != "channels":
                        return {"step": "linked"}
                    # Only channel titles/IDs, never message bodies or private conversations.
                    channels = []
                    async for dialog in client.iter_dialogs(limit=500):
                        if (
                            dialog.is_channel
                            and getattr(dialog.entity, "broadcast", False)
                            and not getattr(dialog.entity, "left", False)
                        ):
                            channels.append({"id": str(dialog.id), "title": dialog.name})
                    return {
                        "step": "linked",
                        "channels": channels,
                        "note": "Broadcast channels from the first 500 chats. Select a channel matching each signal format.",
                    }
            except errors.FloodWaitError as exc:
                self.cancel(uid)
                raise ConnectionError(
                    f"Telegram requests a wait of {exc.seconds} seconds. Try again afterward.", 429
                ) from None
            except (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError):
                raise ConnectionError(
                    "Incorrect login code or two-step password. Check it and try again."
                ) from None
            except errors.PhoneCodeExpiredError:
                self.cancel(uid)
                raise ConnectionError("Telegram login code expired. Start again.") from None
            except ConnectionError:
                raise
            except Exception:
                self.cancel(uid)
                raise ConnectionError(
                    "Telegram could not complete this check. Verify your details and connection, then retry."
                ) from None
            finally:
                with suppress(Exception):
                    await asyncio.wait_for(client.disconnect(), 5)

    async def kotak(self, uid, totp):
        with connection_lease(self.store, uid):
            self.limit(uid, "kotak", 5)
            creds = self.store.credentials(uid)
            required = ("kotak_consumer_key", "kotak_mobile", "kotak_ucc", "kotak_mpin")
            if not all(creds.get(k) for k in required):
                raise ConnectionError("Save the Kotak token, registered mobile, UCC and MPIN first.")
            self.store.save_credentials(uid, {"kotak_checked_at": None})
            try:
                await self.broker_check({k: creds[k] for k in required}, totp)
            except ConnectionError:
                raise
            except Exception:
                raise ConnectionError(
                    "Kotak check timed out or failed. Check connectivity and try a fresh TOTP."
                ) from None
            self.store.save_credentials(uid, {"kotak_checked_at": time.time()})
            return {
                "ok": True,
                "message": "Kotak authentication verified now. No order placed; market data and worker are not started.",
            }

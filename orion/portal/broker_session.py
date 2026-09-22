"""Encrypted, account-bound feed sessions. No TOTP seed or code is retained."""

from datetime import datetime, timedelta
import hashlib
import json
import time
import uuid
from urllib.parse import urlparse

from ..core import IST

FIELDS = ("edit_token", "edit_sid", "ucc", "sfeed_websocket_url", "feed_url")
CREDENTIALS = ("kotak_consumer_key", "kotak_mobile", "kotak_ucc", "kotak_mpin")


def fingerprint(creds):
    return hashlib.sha256(json.dumps([creds.get(k) for k in CREDENTIALS]).encode()).hexdigest()


def validate(values):
    if set(values) - set(FIELDS) or not all(values.get(k) for k in ("edit_token", "edit_sid", "ucc")):
        raise ValueError("Incomplete broker feed session")
    if any(not isinstance(v, str) or len(v) > 16384 for v in values.values()):
        raise ValueError("Invalid broker feed session")
    for k in ("sfeed_websocket_url", "feed_url"):
        if values.get(k):
            u = urlparse(values[k])
            if (
                u.scheme != "wss"
                or not u.hostname
                or not u.hostname.endswith(".kotaksecurities.com")
                or u.username
                or u.password
            ):
                raise ValueError("Unexpected broker websocket host")
    return values


def capture(client):
    values = {k: getattr(client.configuration, k) for k in FIELDS if getattr(client.configuration, k, None)}
    preferred = urlparse(values.get("sfeed_websocket_url", ""))
    # Some Kotak dynamic configurations supply HTTPS here. Use the broker's
    # explicit websocket fallback; do not guess a websocket path or allow HTTPS.
    if (
        preferred.scheme == "https"
        and preferred.hostname == "sfeed.kotaksecurities.com"
        and preferred.username is None
        and preferred.password is None
        and values.get("feed_url")
    ):
        values.pop("sfeed_websocket_url")
    return validate(values)


def save(store, uid, creds, values):
    validate(values)
    now = datetime.now(IST)
    # Local reuse policy, not a claim about Kotak's actual token lifetime.
    until = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), IST).timestamp()
    item = dict(
        user_id=uid, version=uuid.uuid4().hex, expires=until, fingerprint=fingerprint(creds), values=values
    )
    encrypted = store.cipher.encrypt(json.dumps(item).encode())
    with store.db() as db:
        db.execute(
            "INSERT INTO broker_sessions VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET ciphertext=excluded.ciphertext",
            (uid, encrypted),
        )


def load(store, uid):
    with store.db() as db:
        row = db.execute("SELECT ciphertext FROM broker_sessions WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return None
    item = json.loads(store.cipher.decrypt(row[0]))
    if item["user_id"] != uid:
        raise ValueError("Broker session ownership mismatch")
    if item["expires"] <= time.time() or item["fingerprint"] != fingerprint(store.credentials(uid)):
        return None
    validate(item["values"])
    return item

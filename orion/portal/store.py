"""Account isolation, encrypted credentials and server-side login sessions."""

from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid
from cryptography.fernet import Fernet

SECRET_FIELDS = {
    "kotak_consumer_key",
    "kotak_mobile",
    "kotak_ucc",
    "kotak_mpin",
    "telegram_api_id",
    "telegram_api_hash",
}


class Store:
    def __init__(self, root, key):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = Fernet(key)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL,password TEXT NOT NULL,settings TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS secrets(user_id TEXT PRIMARY KEY REFERENCES users(id),ciphertext BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(bucket TEXT NOT NULL,at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS attempts_bucket ON attempts(bucket,at);
            CREATE TABLE IF NOT EXISTS worker_status(user_id TEXT PRIMARY KEY,at REAL NOT NULL);
            """)
        # Fail startup on the wrong key instead of silently losing access to saved credentials.
        marker = self.root / "key-check"
        if marker.exists():
            if self.cipher.decrypt(marker.read_bytes()) != b"orion-portal-key-v1":
                raise ValueError("Wrong encryption key")
        else:
            marker.write_bytes(self.cipher.encrypt(b"orion-portal-key-v1"))
            marker.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "accounts.db", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def account_dir(self, uid):
        if not re.fullmatch("[0-9a-f]{32}", uid):
            raise ValueError("Invalid account ID")
        p = self.root / "accounts" / uid
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        return p

    @contextmanager
    def lock(self, uid):
        with open(self.account_dir(uid) / "account.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()

    def create_user(self, username, password, settings):
        username = username.strip().lower()
        if not re.fullmatch("[a-z0-9][a-z0-9_.-]{2,39}", username):
            raise ValueError("Username must be 3–40 letters, digits, dots, dashes or underscores")
        if not 12 <= len(password) <= 128:
            raise ValueError("Password must be 12–128 characters")
        uid = uuid.uuid4().hex
        salt = secrets.token_hex(16)
        with self.db() as db:
            db.execute(
                "INSERT INTO users(id,username,salt,password,settings) VALUES(?,?,?,?,?)",
                (uid, username, salt, self.password_hash(password, salt), json.dumps(settings)),
            )
        self.account_dir(uid)
        return uid

    def user(self, uid):
        with self.db() as db:
            row = db.execute("SELECT id,username,settings,enabled FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise ValueError("Unknown account")
        return dict(
            id=row["id"],
            username=row["username"],
            settings=json.loads(row["settings"]),
            enabled=bool(row["enabled"]),
        )

    def by_username(self, name):
        with self.db() as db:
            row = db.execute("SELECT id FROM users WHERE username=?", (name.lower(),)).fetchone()
        if not row:
            raise ValueError("Unknown account")
        return row["id"]

    def login(self, username, password, ip):
        username = username.strip().lower()
        now = time.time()
        buckets = [
            "user:" + hashlib.sha256(username.encode()).hexdigest(),
            "ip:" + hashlib.sha256(ip.encode()).hexdigest(),
        ]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE at<?", (now - 900,))
            for b, limit in zip(buckets, (5, 30)):
                if db.execute("SELECT count(*) FROM attempts WHERE bucket=?", (b,)).fetchone()[0] >= limit:
                    raise ValueError("Try again later")
            db.executemany("INSERT INTO attempts VALUES(?,?)", [(b, now) for b in buckets])
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        salt = row["salt"] if row else "00" * 16
        candidate = self.password_hash(password, salt)
        if not row or not hmac.compare_digest(candidate, row["password"]):
            raise ValueError("Invalid login")
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), row["id"], csrf, now + 28800),
            )
        return token, csrf

    def session(self, token):
        with self.db() as db:
            row = db.execute(
                "SELECT user_id,csrf FROM sessions WHERE token=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def credentials(self, uid):
        with self.db() as db:
            row = db.execute("SELECT ciphertext FROM secrets WHERE user_id=?", (uid,)).fetchone()
        if not row:
            return {}
        decoded = json.loads(self.cipher.decrypt(row["ciphertext"]))
        if decoded["user_id"] != uid:
            raise ValueError("Credential ownership mismatch")
        return decoded["values"]

    def save_credentials(self, uid, patch):
        values = self.credentials(uid)
        # API ID/hash replacement invalidates any existing Telegram authorization.
        if any(k in patch and patch[k] != values.get(k) for k in ("telegram_api_id", "telegram_api_hash")):
            values.pop("telegram_session", None)
        for k, v in patch.items():
            if v is None:
                values.pop(k, None)
            elif v:
                values[k] = v
        encrypted = self.cipher.encrypt(json.dumps({"user_id": uid, "values": values}).encode())
        with self.db() as db:
            db.execute(
                "INSERT INTO secrets VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET ciphertext=excluded.ciphertext",
                (uid, encrypted),
            )

    def credential_status(self, uid):
        values = self.credentials(uid)
        return {
            "saved_fields": sorted(k for k in SECRET_FIELDS if values.get(k)),
            "telegram_linked": bool(values.get("telegram_session")),
            "broker_authenticated": False,
        }

    def config(self, uid):
        user = self.user(uid)
        cfg = user["settings"].copy()
        cfg["new_entries_enabled"] = user["enabled"]
        cfg["mode"] = "paper"
        return cfg

    def heartbeat(self, uid):
        with self.db() as db:
            db.execute(
                "INSERT INTO worker_status VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET at=excluded.at",
                (uid, time.time()),
            )

    def worker_alive(self, uid):
        with self.db() as db:
            row = db.execute("SELECT at FROM worker_status WHERE user_id=?", (uid,)).fetchone()
        return bool(row and time.time() - row["at"] < 45)

    def worker_running(self, uid):
        with open(self.account_dir(uid) / "worker.lock", "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False

    def save_settings(self, uid, settings):
        with self.db() as db:
            db.execute("UPDATE users SET settings=? WHERE id=?", (json.dumps(settings), uid))

    def set_enabled(self, uid, enabled):
        with self.db() as db:
            db.execute("UPDATE users SET enabled=? WHERE id=?", (int(enabled), uid))

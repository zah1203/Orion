import asyncio
import fcntl
import tempfile
import time
import unittest
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from telethon import errors
from orion.portal.app import create_app, defaults
from orion.portal.connections import Connections, ConnectionError
from orion.portal.kotak_check import success
from orion.portal.store import Store


class FakeTelegram:
    def __init__(self, creds, session):
        self.session = SimpleNamespace(save=lambda: "SECRET-SESSION")
        self.authorized = bool(session == "SECRET-SESSION")
        self.disconnected = False
        self.password_needed = False
        self.invalid = False
        self.expired = False

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="SECRET-PHONE-HASH")

    async def sign_in(self, **kwargs):
        if self.expired:
            raise errors.PhoneCodeExpiredError(None)
        if self.invalid:
            raise errors.PhoneCodeInvalidError(None)
        if self.password_needed and "password" not in kwargs:
            raise errors.SessionPasswordNeededError(None)
        self.authorized = True

    async def is_user_authorized(self):
        return self.authorized

    async def iter_dialogs(self, limit):
        yield SimpleNamespace(
            is_channel=True,
            entity=SimpleNamespace(broadcast=True, left=False),
            id=-1001234567,
            name="Index calls",
        )
        yield SimpleNamespace(is_channel=False, entity=SimpleNamespace(), id=123, name="Private contact")
        yield SimpleNamespace(
            is_channel=True, entity=SimpleNamespace(broadcast=False), id=-1007654321, name="Group"
        )


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name, Fernet.generate_key())
        self.alice = self.store.create_user("alice", "long test password", defaults())
        self.bob = self.store.create_user("bob", "long test password", defaults())
        self.clients = []
        self.options = {}

        def factory(creds, session):
            client = FakeTelegram(creds, session)
            for key, value in self.options.items():
                setattr(client, key, value)
            self.clients.append(client)
            return client

        async def broker(creds, totp):
            self.assertEqual(totp, "123456")

        self.manager = Connections(self.store, factory, broker)
        for uid in (self.alice, self.bob):
            self.store.save_credentials(
                uid,
                {
                    "telegram_api_id": "12345",
                    "telegram_api_hash": "a" * 32,
                    "kotak_consumer_key": "secret",
                    "kotak_mobile": "+919876543210",
                    "kotak_ucc": "abc",
                    "kotak_mpin": "123456",
                },
            )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def start(self):
        return await self.manager.telegram(self.alice, "browser1", "start", "+919876543210")

    async def test_login_encrypted_pending_session_and_channels(self):
        self.assertEqual(await self.start(), {"step": "code"})
        pending = self.manager.pending[self.alice]
        self.assertNotIn(b"SECRET", pending["sealed"])
        self.assertNotIn(b"9876543210", pending["sealed"])
        self.assertNotIn("telegram_session", self.store.credentials(self.alice))
        self.assertEqual(
            await self.manager.telegram(self.alice, "browser1", "code", "12345"), {"step": "linked"}
        )
        self.assertEqual(self.store.credentials(self.alice)["telegram_session"], "SECRET-SESSION")
        self.assertNotIn("telegram_session", self.store.credentials(self.bob))
        channels = await self.manager.telegram(self.alice, "browser1", "channels")
        self.assertEqual(channels["channels"], [{"id": "-1001234567", "title": "Index calls"}])
        self.assertTrue(all(c.disconnected for c in self.clients))
        self.assertNotIn("SECRET", str(self.manager.status(self.alice, "browser1")))

    async def test_two_factor_and_wrong_code(self):
        await self.start()
        self.options["invalid"] = True
        with self.assertRaisesRegex(ConnectionError, "Incorrect"):
            await self.manager.telegram(self.alice, "browser1", "code", "00000")
        self.options = {"password_needed": True}
        self.assertEqual(
            await self.manager.telegram(self.alice, "browser1", "code", "12345"), {"step": "password"}
        )
        self.assertEqual(
            await self.manager.telegram(self.alice, "browser1", "password", "private password"),
            {"step": "linked"},
        )
        self.assertNotIn("private password", str(self.store.credentials(self.alice)))

    async def test_owner_browser_and_user_isolation(self):
        await self.start()
        for uid, browser in [(self.alice, "browser2"), (self.bob, "browser1")]:
            with self.assertRaisesRegex(ConnectionError, "another browser"):
                await self.manager.telegram(uid, browser, "code", "12345")
        self.assertEqual(self.manager.status(self.alice, "browser2")["telegram"]["step"], "idle")

    async def test_expired_and_changed_credentials(self):
        await self.start()
        self.manager.pending[self.alice]["expires"] = time.time() - 1
        with self.assertRaises(ConnectionError):
            await self.manager.telegram(self.alice, "browser1", "code", "12345")
        await self.start()
        self.store.save_credentials(self.alice, {"telegram_api_hash": "b" * 32})
        with self.assertRaisesRegex(ConnectionError, "changed"):
            await self.manager.telegram(self.alice, "browser1", "code", "12345")
        self.assertNotIn(self.alice, self.manager.pending)

    async def test_provider_expiry_and_redacted_failure(self):
        await self.start()
        self.options["expired"] = True
        with self.assertRaisesRegex(ConnectionError, "expired"):
            await self.manager.telegram(self.alice, "browser1", "code", "12345")
        self.assertNotIn(self.alice, self.manager.pending)

        async def bad(*args):
            raise RuntimeError("SECRET-BROKER-TOKEN")

        self.manager.broker_check = bad
        with self.assertRaises(ConnectionError) as result:
            await self.manager.kotak(self.alice, "123456")
        self.assertNotIn("SECRET", str(result.exception))

    async def test_rate_limit_survives_manager_restart(self):
        for _ in range(3):
            await self.start()
        replacement = Connections(self.store)
        with self.assertRaisesRegex(ConnectionError, "Too many"):
            await replacement.telegram(self.alice, "browser1", "start", "+919876543210")

    async def test_worker_lease_and_enabled_block_checks(self):
        with open(self.store.account_dir(self.alice) / "worker.lock", "a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            with self.assertRaisesRegex(ConnectionError, "worker"):
                await self.start()
        self.store.set_enabled(self.alice, True)
        with self.assertRaisesRegex(ConnectionError, "Pause"):
            await self.manager.kotak(self.alice, "123456")

    async def test_kotak_check_reset_and_no_otp_persistence(self):
        await self.manager.kotak(self.alice, "123456")
        self.assertTrue(self.manager.status(self.alice, "browser1")["kotak"]["checked_at"])
        self.assertIsNone(self.manager.status(self.bob, "browser1")["kotak"]["checked_at"])
        self.assertFalse(any("totp" in k for k in self.store.credentials(self.alice)))
        self.store.save_credentials(self.alice, {"kotak_consumer_key": "new"})
        self.assertIsNone(self.manager.status(self.alice, "browser1")["kotak"]["checked_at"])

    async def test_inflight_check_excludes_credential_update(self):
        from orion.portal.connections import connection_lease

        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(*args):
            entered.set()
            await release.wait()

        self.manager.broker_check = slow
        task = asyncio.create_task(self.manager.kotak(self.alice, "123456"))
        await entered.wait()
        try:
            with self.assertRaises(ConnectionError):
                with connection_lease(self.store, self.alice):
                    self.store.save_credentials(self.alice, {"kotak_ucc": "wrong"})
        finally:
            release.set()
            await task


class ConnectionRouteTests(unittest.TestCase):
    def test_routes_require_login_csrf_and_reject_secrets_in_errors(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(root, Fernet.generate_key(), "http://127.0.0.1:8000")
            app.state.store.create_user("alice", "long test password", defaults())
            with TestClient(app, base_url="http://127.0.0.1:8000") as client:
                headers = {"Origin": "http://127.0.0.1:8000"}
                self.assertEqual(client.get("/api/connections").status_code, 401)
                login = client.post(
                    "/api/login",
                    json={"username": "alice", "password": "long test password"},
                    headers=headers,
                )
                self.assertEqual(
                    client.post(
                        "/api/connections/kotak/verify", json={"totp": "123456"}, headers=headers
                    ).status_code,
                    403,
                )
                headers["X-CSRF-Token"] = login.json()["csrf"]
                invalid = client.post(
                    "/api/connections/kotak/verify", json={"totp": "SECRET"}, headers=headers
                )
                self.assertEqual(invalid.status_code, 422)
                self.assertNotIn("SECRET", invalid.text)
                self.assertEqual(
                    client.post(
                        "/api/connections/telegram/start",
                        json={"phone": "+919876543210", "user_id": "other"},
                        headers=headers,
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        "/api/connections/telegram/start", json={"phone": "+919876543210"}, headers=headers
                    ).status_code,
                    400,
                )

    def test_http_connection_flow_and_separate_credential_saves(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(root, Fernet.generate_key(), "http://127.0.0.1:8000")
            uid = app.state.store.create_user("alice", "long test password", defaults())
            app.state.connections.client_factory = FakeTelegram

            async def broker(creds, totp):
                assert totp == "123456"

            app.state.connections.broker_check = broker
            with TestClient(app, base_url="http://127.0.0.1:8000") as client:
                headers = {"Origin": "http://127.0.0.1:8000"}
                login = client.post(
                    "/api/login",
                    json={"username": "alice", "password": "long test password"},
                    headers=headers,
                )
                headers["X-CSRF-Token"] = login.json()["csrf"]

                def post(path, body):
                    r = client.post("/api/connections/" + path, json=body, headers=headers)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertNotIn("SECRET", r.text)
                    return r.json()

                client.put(
                    "/api/credentials",
                    json={"telegram_api_id": "12345", "telegram_api_hash": "a" * 32},
                    headers=headers,
                )
                self.assertEqual(post("telegram/start", {"phone": "+919876543210"})["step"], "code")
                self.assertEqual(post("telegram/code", {"code": "12345"})["step"], "linked")
                self.assertEqual(post("telegram/channels", {})["channels"][0]["id"], "-1001234567")
                client.put(
                    "/api/credentials",
                    json={
                        "kotak_consumer_key": "token",
                        "kotak_mobile": "+919876543210",
                        "kotak_ucc": "abc",
                        "kotak_mpin": "123456",
                    },
                    headers=headers,
                )
                post("kotak/verify", {"totp": "123456"})
                self.assertTrue(client.get("/api/connections").json()["kotak"]["checked_at"])
                self.assertEqual(app.state.store.credentials(uid)["telegram_session"], "SECRET-SESSION")
                self.assertFalse(app.state.store.user(uid)["enabled"])
                client.put("/api/credentials", json={"telegram_api_hash": None}, headers=headers)
                status = client.get("/api/connections").json()
                self.assertFalse(status["telegram"]["linked"])
                self.assertTrue(status["kotak"]["checked_at"])

    def test_kotak_success_requires_both_status_and_session(self):
        self.assertFalse(success({"error": [{"message": "failed"}]}))
        self.assertFalse(success({"data": {"status": "success"}}))
        self.assertFalse(success({"data": {"status": "failed", "token": "secret", "sid": "secret"}}))
        self.assertTrue(success({"data": {"status": "success", "token": "secret", "sid": "secret"}}))

import copy
import json
from pathlib import Path
import tempfile
import unittest
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient
from orion.core import stamp
from orion.portal.app import create_app
from orion.portal.store import Store

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8000"
PASSWORD = "correct horse test password"


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key()
        self.app = create_app(self.tmp.name, self.key, ORIGIN)
        self.store = self.app.state.store
        self.accounts = self.app.state.accounts
        cfg = json.loads((ROOT / "config/paper.json").read_text())
        self.alice = self.store.create_user("alice", PASSWORD, cfg)
        cfg = copy.deepcopy(cfg)
        cfg["max_lots"] = 1
        self.bob = self.store.create_user("bob", PASSWORD, cfg)
        self.client = TestClient(self.app, base_url=ORIGIN)
        r = self.client.post(
            "/api/login", json={"username": "alice", "password": PASSWORD}, headers={"Origin": ORIGIN}
        )
        self.assertEqual(r.status_code, 200)
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": r.json()["csrf"]}
        self.master = json.loads((ROOT / "examples/instruments.synthetic.json").read_text())
        self.events = [json.loads(x) for x in (ROOT / "examples/replay.jsonl").read_text().splitlines()]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.headers)

    def test_login_cookie_and_csrf(self):
        r = self.client.get("/api/me")
        self.assertEqual(r.json()["username"], "alice")
        self.assertNotIn("password", r.json())
        self.assertEqual(
            self.client.post("/api/control", json={"enabled": True}, headers={"Origin": ORIGIN}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/control",
                json={"enabled": True},
                headers={"Origin": "https://evil.example", "X-CSRF-Token": self.headers["X-CSRF-Token"]},
            ).status_code,
            403,
        )

    def test_no_user_id_override(self):
        self.assertEqual(self.post("/api/control", {"enabled": True, "user_id": self.bob}).status_code, 422)
        self.assertEqual(self.client.get("/api/users/" + self.bob).status_code, 404)
        self.assertFalse(self.store.user(self.bob)["enabled"])

    def test_credentials_encrypted_and_never_returned(self):
        secret = "very-private-kotak-token"
        r = self.client.put("/api/credentials", json={"kotak_consumer_key": secret}, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(secret, r.text + self.client.get("/api/me").text)
        self.assertEqual(self.store.credentials(self.alice)["kotak_consumer_key"], secret)
        self.assertEqual(self.store.credentials(self.bob), {})
        for path in Path(self.tmp.name).glob("accounts.db*"):
            self.assertNotIn(secret.encode(), path.read_bytes())
        with self.store.db() as db:
            cipher = db.execute("SELECT ciphertext FROM secrets WHERE user_id=?", (self.alice,)).fetchone()[0]
            db.execute("INSERT INTO secrets VALUES(?,?)", (self.bob, cipher))
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.store.credentials(self.bob)

    def test_wrong_encryption_key_fails_startup(self):
        with self.assertRaises(InvalidToken):
            Store(self.tmp.name, Fernet.generate_key())

    def test_clear_credentials_removes_telegram_session(self):
        self.store.save_credentials(self.alice, {"telegram_api_hash": "hash", "telegram_session": "session"})
        self.client.put("/api/credentials", json={"telegram_api_hash": None}, headers=self.headers)
        self.assertNotIn("telegram_session", self.store.credentials(self.alice))

    def test_two_users_same_signal_independent_lots_and_dedup(self):
        for uid in (self.alice, self.bob):
            self.accounts.set_enabled(uid, True)
            for event in self.events[:3]:
                self.accounts.process(uid, event, self.master, stamp(event["source_time"]), True)
        a = self.accounts.summary(self.alice)
        b = self.accounts.summary(self.bob)
        self.assertEqual(next(iter(a["state"]["positions"].values()))["lots"], 3)
        self.assertEqual(next(iter(b["state"]["positions"].values()))["lots"], 1)
        before = b["state"]
        for event in self.events[3:]:
            self.accounts.process(self.alice, event, self.master, stamp(event["source_time"]), True)
        self.assertEqual(self.accounts.summary(self.bob)["state"], before)
        alice_pnl = self.accounts.summary(self.alice)["pnl"]["totals"]
        bob_pnl = self.accounts.summary(self.bob)["pnl"]["totals"]
        self.assertEqual(alice_pnl["realized"], "785.0")
        self.assertEqual(bob_pnl["realized"], "-25")
        self.assertEqual(
            self.accounts.process(
                self.alice, self.events[0], self.master, stamp(self.events[0]["source_time"]), True
            ),
            [],
        )

    def test_pause_one_user_keeps_other_enabled_and_open_exits_work(self):
        for uid in (self.alice, self.bob):
            self.accounts.set_enabled(uid, True)
        for event in self.events[:3]:
            self.accounts.process(self.alice, event, self.master, stamp(event["source_time"]), True)
        self.assertEqual(self.post("/api/control", {"enabled": False}).status_code, 200)
        self.assertTrue(self.store.user(self.bob)["enabled"])
        event = self.events[3]
        self.accounts.process(self.alice, event, self.master, stamp(event["source_time"]), True)
        position = next(iter(self.accounts.summary(self.alice)["state"]["positions"].values()))
        self.assertEqual((position["remaining"], position["stop"]), (2, "170.5"))

    def test_pause_cancels_pending_and_new_messages(self):
        self.accounts.set_enabled(self.alice, True)
        event = self.events[0]
        self.accounts.process(self.alice, event, self.master, stamp(event["source_time"]), True)
        self.accounts.set_enabled(self.alice, False)
        p = next(iter(self.accounts.summary(self.alice)["state"]["positions"].values()))
        self.assertEqual(p["status"], "CANCELLED")
        event = {**event, "event_id": "new", "message_id": "2"}
        result = self.accounts.process(self.alice, event, self.master, stamp(event["source_time"]), True)
        self.assertEqual(result[0]["event"], "ENTRIES_PAUSED")

    def test_demo_does_not_write_ledger(self):
        result = self.post("/api/demo", {}).json()
        self.assertTrue(result["synthetic"])
        self.assertFalse((self.store.account_dir(self.alice) / "paper.db").exists())
        self.assertEqual(self.accounts.summary(self.bob)["history"], [])

    def test_restart_retains_credentials_and_independent_state(self):
        self.store.save_credentials(self.alice, {"kotak_ucc": "alice-only"})
        self.accounts.set_enabled(self.alice, True)
        for event in self.events[:3]:
            self.accounts.process(self.alice, event, self.master, stamp(event["source_time"]), True)
        app = create_app(self.tmp.name, self.key, ORIGIN)
        self.assertEqual(app.state.store.credentials(self.alice)["kotak_ucc"], "alice-only")
        self.assertTrue(app.state.accounts.summary(self.alice)["state"]["positions"])
        self.assertEqual(app.state.accounts.summary(self.bob)["state"]["positions"], {})

    def test_logout_revokes_session(self):
        token = self.client.cookies.get("orion_session")
        self.assertEqual(self.post("/api/logout", {}).status_code, 200)
        self.assertIsNone(self.store.session(token))
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_rate_limited_login(self):
        for _ in range(4):
            self.assertEqual(
                self.client.post(
                    "/api/login",
                    json={"username": "alice", "password": "incorrect"},
                    headers={"Origin": ORIGIN},
                ).status_code,
                401,
            )
        self.assertEqual(
            self.client.post(
                "/api/login", json={"username": "alice", "password": PASSWORD}, headers={"Origin": ORIGIN}
            ).status_code,
            429,
        )

    def test_unsafe_origin_and_path_rejected(self):
        with self.assertRaises(ValueError):
            create_app(self.tmp.name, self.key, "http://example.com")
        with self.assertRaises(ValueError):
            self.store.account_dir("../bob")

    def test_settings_locked_with_open_trade(self):
        self.accounts.set_enabled(self.alice, True)
        for event in self.events[:3]:
            self.accounts.process(self.alice, event, self.master, stamp(event["source_time"]), True)
        self.accounts.set_enabled(self.alice, False)
        with self.assertRaisesRegex(ValueError, "open paper trades"):
            self.accounts.save_settings(self.alice, self.store.user(self.alice)["settings"])

    def test_settings_update_only_owner(self):
        old = self.store.user(self.bob)["settings"]
        body = dict(
            paper_cash=100000,
            risk_per_trade=500,
            daily_loss_limit=1000,
            max_open_risk=1000,
            max_lots=2,
            max_open_positions=1,
            max_entries_per_day=3,
            index_channel="-100123456789",
            commodity_channel="",
            products=["BANKNIFTY"],
        )
        self.assertEqual(self.client.put("/api/settings", json=body, headers=self.headers).status_code, 200)
        self.assertEqual(self.store.user(self.alice)["settings"]["max_lots"], 2)
        self.assertEqual(self.store.user(self.bob)["settings"], old)
        self.assertEqual(
            self.client.put("/api/settings", json={**body, "mode": "live"}, headers=self.headers).status_code,
            422,
        )

    def test_security_headers_and_static_dashboard(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertIn("script-src 'self'", r.headers["content-security-policy"])
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_request_size_limit(self):
        self.assertEqual(
            self.client.put(
                "/api/credentials",
                content="x" * 33000,
                headers={**self.headers, "Content-Type": "application/json"},
            ).status_code,
            413,
        )


if __name__ == "__main__":
    unittest.main()

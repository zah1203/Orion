import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from orion.portal.app import create_app, defaults

ORIGIN = "https://abcdefghij.execute-api.ap-south-1.amazonaws.com"
PASSWORD = "a long test password for public web"
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_public_web", ROOT / "scripts/configure_public_web.py")
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class PublicWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name, Fernet.generate_key(), ORIGIN, trust_local_proxy=True)
        self.store = self.app.state.store
        self.store.create_user("alice", PASSWORD, defaults())
        self.client = TestClient(self.app, base_url=ORIGIN, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def login(self, ip="203.0.113.8", **extra):
        return self.client.post("/api/login", json={"username": "alice", "password": PASSWORD},
                                headers={"Origin": ORIGIN, "X-Orion-Viewer-IP": ip, **extra})

    def test_public_cookie_and_headers(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=strict"):
            self.assertIn(attribute, cookie)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("max-age=", response.headers["strict-transport-security"])
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_proxy_source_required_and_validated(self):
        for ip in ("", "203.0.113.8, 203.0.113.9", "bogus", "127.0.0.1:8000"):
            self.assertEqual(self.login(ip).status_code, 403)
        with TestClient(self.app, base_url=ORIGIN, client=("203.0.113.20", 50000)) as remote:
            response = remote.post("/api/login", json={"username": "alice", "password": PASSWORD},
                                   headers={"Origin": ORIGIN, "X-Orion-Viewer-IP": "203.0.113.8"})
            self.assertEqual(response.status_code, 403)

    def test_forwarded_headers_cannot_change_throttle_bucket(self):
        self.assertEqual(self.login("2001:db8::1", **{"X-Forwarded-For": "1.1.1.1"}).status_code, 200)
        with self.store.db() as db:
            buckets = [row[0] for row in db.execute("SELECT bucket FROM attempts")]
        self.assertIn("ip:" + hashlib.sha256(b"2001:db8::1").hexdigest(), buckets)
        self.assertNotIn("ip:" + hashlib.sha256(b"1.1.1.1").hexdigest(), buckets)

    def test_new_public_users_require_approval(self):
        response = self.client.post("/api/register", json={"username": "newuser", "password": PASSWORD},
                                    headers={"Origin": ORIGIN, "X-Orion-Viewer-IP": "203.0.113.9"})
        self.assertEqual(response.status_code, 201)
        uid = self.store.by_username("newuser")
        self.assertEqual(self.store.user(uid)["access"], "pending")
        response = self.client.post("/api/login", json={"username": "newuser", "password": PASSWORD},
                                    headers={"Origin": ORIGIN, "X-Orion-Viewer-IP": "203.0.113.9"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        self.assertEqual(self.client.post("/api/control", json={"enabled": True}, headers={
            "Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"],
        }).status_code, 403)

    def test_proxy_configuration_rejects_unexpected_hosts(self):
        for origin in ("http://example.com", "https://evil.example", ORIGIN + "/", ORIGIN + "\nanything"):
            with self.assertRaises(ValueError):
                config.proxy_config(origin, "10.76.1.139")
        with self.assertRaises(ValueError):
            config.proxy_config(ORIGIN, "0.0.0.0")
        rendered = config.proxy_config(ORIGIN, "10.76.1.139")
        self.assertIn("listen 10.76.1.139:8080", rendered)
        self.assertIn("proxy_set_header X-Orion-Viewer-IP $remote_addr", rendered)


if __name__ == "__main__":
    unittest.main()

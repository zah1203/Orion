import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet, InvalidToken
from cryptography.exceptions import InvalidTag
from orion import recovery as r
from orion.core import Engine, stamp
from orion.portal.store import Store
from orion.portal.broker_session import save as save_broker

ROOT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def test_safe_failure_reasons_never_expose_private_values(self):
        for exc, expected in [
            (ValueError("Unknown file in portal state; review inventory"),
             "unknown-file-in-portal-state-review-inventory"),
            (ValueError("private credential"), "unknown"),
            (FileNotFoundError("private path"), "missing-file"),
            (sqlite3.OperationalError("no such table: private_name"), "sqlite-schema"),
            (InvalidToken("private ciphertext"), "invalid-ciphertext"),
        ]:
            self.assertEqual(r.safe_reason(exc), expected)
        from scripts.backup_host import SAFE_REASONS
        self.assertTrue(set(r.SAFE_REASONS.values()).issubset(SAFE_REASONS))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.portal = self.root / "production/portal"
        self.runtime = self.root / "production/runtime"
        self.config = self.root / "config"
        self.runtime.mkdir(parents=True)
        self.config.mkdir()
        self.work = self.root / "work"
        self.output = self.root / "backups"
        self.key, self.backup_key = Fernet.generate_key(), Fernet.generate_key()
        self.store = Store(self.portal, self.key)
        self.cfg = json.loads((ROOT / "config/paper.json").read_text())
        self.uid = self.store.create_user("alice", "a safe test password", self.cfg)
        self.bob = self.store.create_user("bob", "another safe test password", self.cfg)
        self.store.save_credentials(self.uid, {"kotak_ucc": "secret-test-ucc", "telegram_session": "test-session"})
        save_broker(self.store, self.uid, self.store.credentials(self.uid),
                    {"edit_token": "secret-feed-token", "edit_sid": "test-sid", "ucc": "secret-test-ucc"})
        self.store.set_enabled(self.uid, True)
        master = json.loads((ROOT / "examples/instruments.synthetic.json").read_text())
        self.engine = Engine(str(self.store.account_dir(self.uid) / "paper.db"), self.cfg, master, True)
        events = [json.loads(x) for x in (ROOT / "examples/replay.jsonl").read_text().splitlines()]
        for event in events[:3]:
            self.engine.process(event, stamp(event["source_time"]))
        self.positions = self.engine.state()["positions"]
        self.assertTrue(self.positions)
        # Keep connections open, with committed changes only in WAL.
        self.held = sqlite3.connect(self.portal / "accounts.db")
        self.held.execute("PRAGMA wal_autocheckpoint=0")
        self.held.execute("UPDATE users SET role='owner' WHERE id=?", (self.uid,))
        self.held.commit()
        (self.portal / "contracts.json").write_text('{"as_of":"test"}')
        (self.config / "portal.key").write_bytes(self.key)
        (self.config / "portal.env").write_text("ORION_PORTAL_DATA=/var/lib/orion/portal\n")

    def tearDown(self):
        self.engine.db.close()
        self.held.close()
        self.temp.cleanup()

    def backup(self):
        return r.create(self.portal, self.runtime, self.config, self.output, self.backup_key, self.key, self.work)

    def test_catalogue_history_and_broker_exports_round_trip(self):
        names = ["contracts.backup-20260929-204032.json", "economics-2026-09-29.json",
                 "broker-exports/nse_fo.csv", "broker-exports/mcx_fo.csv",
                 "broker-exports/nse_fo.csv.receipt.json", "broker-exports/mcx_fo.csv.receipt.json"]
        for name in names:
            path = self.portal / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"historical catalogue fixture")
        archive, manifest = self.backup()
        r.restore(archive, self.root / "restored", self.backup_key, self.key, self.work)
        for name in names:
            self.assertIn("portal/" + name, manifest["files"])
            self.assertEqual((self.root / "restored/portal" / name).read_bytes(),
                             (self.portal / name).read_bytes())
        for name in ["broker-exports/credentials.json", "contracts.backup-private.json",
                     "economics-secret.json", "broker-exports/nse_fo.csv.extra"]:
            unexpected = self.portal / name
            unexpected.touch()
            with self.assertRaisesRegex(ValueError, "Unknown file"):
                r.inventory(self.portal, self.runtime, self.config)
            unexpected.unlink()
        symlink = self.portal / "broker-exports/nse_fo.csv"
        symlink.unlink()
        symlink.symlink_to(self.config / "portal.key")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            r.inventory(self.portal, self.runtime, self.config)

    def test_round_trip_accounts_positions_credentials_wal_and_no_production_changes(self):
        self.assertTrue(Path(str(self.portal / "accounts.db") + "-wal").exists())
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            path, manifest = self.backup()
            self.assertNotIn(b"secret-test-ucc", path.read_bytes())
            self.assertNotIn(self.key, path.read_bytes())
            self.assertNotIn("config/portal.key", manifest["files"])
            report = r.restore(path, self.root / "restore", self.backup_key, self.key, self.work)
        self.assertEqual(report["verified_before_sanitizing"]["accounts"], 2)
        self.assertEqual(report["verified_before_sanitizing"]["credential_records"], 1)
        self.assertEqual(report["verified_before_sanitizing"]["broker_sessions"], 1)
        self.assertGreater(report["verified_before_sanitizing"]["open_positions"], 0)
        with sqlite3.connect(self.root / "restore/portal/accounts.db") as db:
            self.assertEqual(db.execute("SELECT role,enabled FROM users WHERE id=?", (self.uid,)).fetchone(), ("owner", 0))
            cipher = db.execute("SELECT ciphertext FROM secrets WHERE user_id=?", (self.uid,)).fetchone()[0]
            self.assertEqual(json.loads(Fernet(self.key).decrypt(cipher))["values"], self.store.credentials(self.uid))
            self.assertEqual(db.execute("SELECT count(*) FROM broker_sessions").fetchone()[0], 0)
        with sqlite3.connect(self.root / "restore/portal/accounts" / self.uid / "paper.db") as db:
            recovered = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            self.assertEqual(recovered["positions"], self.positions)
        with self.assertRaisesRegex(ValueError, "Offline recovery"):
            Store(self.root / "restore/portal", self.key)
        self.assertTrue(self.store.user(self.uid)["enabled"])
        self.assertEqual(self.engine.state()["positions"], self.positions)
        self.assertEqual(self.store.credentials(self.uid)["kotak_ucc"], "secret-test-ucc")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_wrong_keys_and_tampering_fail_without_restore_output(self):
        path, _ = self.backup()
        for backup_key, portal_key, error in [(Fernet.generate_key(), self.key, InvalidTag),
                                              (self.backup_key, Fernet.generate_key(), InvalidToken)]:
            with self.assertRaises(error):
                r.restore(path, self.root / "bad", backup_key, portal_key, self.work)
            self.assertFalse((self.root / "bad").exists())
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 1
        path.write_bytes(data)
        with self.assertRaises(InvalidTag):
            r.verify(path, self.backup_key, self.key, self.work)

    def test_restore_refuses_existing_and_production_destinations(self):
        path, _ = self.backup()
        for dest in (self.portal, self.root, self.portal / "new-restore", Path("/var/lib/orion/recovery")):
            with self.assertRaises(ValueError):
                r.restore(path, dest, self.backup_key, self.key, self.work)
        self.assertTrue(self.store.user(self.uid)["enabled"])

    def test_legacy_database_and_telegram_sqlite_session_included(self):
        r.sqlite_copy(self.store.account_dir(self.uid) / "paper.db", self.runtime / "paper.db")
        with sqlite3.connect(self.runtime.parent / "telegram.session") as db:
            db.execute("CREATE TABLE sessions(secret TEXT)")
            db.execute("INSERT INTO sessions VALUES('test-private-session')")
        path, manifest = self.backup()
        self.assertEqual(manifest["counts"]["paper_databases"], 2)
        self.assertEqual(manifest["absent_optional"], [])
        self.assertIn("legacy/telegram.session", manifest["files"])
        r.verify(path, self.backup_key, self.key, self.work)

    def test_unknown_state_symlinks_and_same_keys_fail_closed(self):
        (self.portal / "unexpected.key").write_bytes(self.key)
        with self.assertRaisesRegex(ValueError, "Unknown file"):
            self.backup()
        (self.portal / "unexpected.key").unlink()
        (self.portal / "link").symlink_to(self.config / "portal.key")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.backup()
        (self.portal / "link").unlink()
        with self.assertRaisesRegex(ValueError, "separate"):
            r.create(self.portal, self.runtime, self.config, self.output, self.key, self.key, self.work)

    def test_corrupt_sqlite_and_timeout_leave_no_final_backup(self):
        with patch.object(r.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaises(TimeoutError):
                r.sqlite_copy(self.portal / "accounts.db", self.root / "deadline.db", timeout=1)
        (self.runtime / "paper.db").write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            self.backup()
        self.assertEqual(list(self.output.glob("*.orb")), [])
        self.assertEqual(list(self.work.iterdir()), [])

    def test_malicious_archive_paths_and_links_rejected(self):
        for name, kind in [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("portal/link", tarfile.SYMTYPE)]:
            archive = self.root / "evil.tar"
            with tarfile.open(archive, "w") as tar:
                item = tarfile.TarInfo(name)
                item.type = kind
                tar.addfile(item, io.BytesIO(b""))
            dest = self.root / "extract"
            dest.mkdir(exist_ok=True)
            with self.assertRaises(ValueError):
                r.unpack(archive, dest)
        self.assertFalse((self.root / "escape").exists())

    def test_manifest_checksums_detect_validly_encrypted_but_inconsistent_payload(self):
        path, _ = self.backup()
        plain = self.root / "payload.tar"
        r.decrypt(path, plain, self.backup_key)
        forged = self.root / "forged.tar"
        with tarfile.open(plain, "r:") as src, tarfile.open(forged, "w") as dst:
            for member in src:
                content = src.extractfile(member).read()
                if member.name == "portal/contracts.json":
                    content = b"different content"
                    member.size = len(content)
                dst.addfile(member, io.BytesIO(content))
        encrypted = self.root / "forged.orb"
        r.encrypt(forged, encrypted, self.backup_key)
        with self.assertRaisesRegex(ValueError, "checksum"):
            r.restore(encrypted, self.root / "forged-restore", self.backup_key, self.key, self.work)
        self.assertFalse((self.root / "forged-restore").exists())

    def test_credential_ownership_mismatch_prevents_success(self):
        with self.store.db() as db:
            cipher = db.execute("SELECT ciphertext FROM secrets WHERE user_id=?", (self.uid,)).fetchone()[0]
            db.execute("INSERT INTO secrets VALUES(?,?)", (self.bob, cipher))
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.backup()
        self.assertEqual(list(self.output.glob("*.orb")), [])

    def test_key_permissions_and_work_directory_overlap(self):
        keyfile = self.root / "recovery.key"
        keyfile.write_bytes(self.backup_key)
        keyfile.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            r.key_bytes(keyfile)
        keyfile.chmod(0o600)
        self.assertEqual(r.key_bytes(keyfile), self.backup_key)
        with self.assertRaisesRegex(ValueError, "outside source"):
            r.create(self.portal, self.runtime, self.config, self.portal / "backups",
                     self.backup_key, self.key, self.work)
        self.assertFalse((self.portal / "backups").exists())

    def test_new_account_or_ledger_during_snapshot_fails_for_retry(self):
        original = r.sqlite_copy
        changed = False

        def copying(source, target, timeout=60):
            nonlocal changed
            original(source, target, timeout)
            if not changed:
                changed = True
                self.store.create_user("charlie", "another safe test password", self.cfg)

        with patch.object(r, "sqlite_copy", side_effect=copying):
            with self.assertRaisesRegex(ValueError, "Accounts changed"):
                self.backup()
        self.assertEqual(list(self.output.glob("*.orb")), [])

    def test_cli_keeps_ciphertext_when_upload_fails(self):
        portal_key = self.root / "portal.key"
        backup_key = self.root / "recovery.key"
        for path, value in ((portal_key, self.key), (backup_key, self.backup_key)):
            path.write_bytes(value)
            path.chmod(0o600)
        argv = ["recovery", "backup", "--portal", str(self.portal), "--runtime", str(self.runtime),
                "--config", str(self.config), "--output", str(self.output), "--work-dir", str(self.work),
                "--portal-key-file", str(portal_key), "--backup-key-file", str(backup_key),
                "--bucket", "test-bucket", "--remove-local-after-upload"]
        # Inject a fake module so this pure-local test requires no AWS SDK or identity.
        with patch("sys.argv", argv), patch.dict("sys.modules", {"boto3": Mock()}), \
                patch.object(r, "upload", side_effect=RuntimeError("sensitive SDK details")):
            with self.assertRaises(SystemExit) as raised:
                r.main()
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertEqual(len(list(self.output.glob("*.orb"))), 1)

    def test_s3_versioned_readback_and_private_bucket_checks(self):
        path, _ = self.backup()
        s3 = Mock()
        s3.get_public_access_block.return_value = {"PublicAccessBlockConfiguration": {
            k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}}
        s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3.get_bucket_policy_status.return_value = {"PolicyStatus": {"IsPublic": False}}
        s3.put_object.return_value = {"VersionId": "test-version"}
        s3.get_object.return_value = {"Body": io.BytesIO(path.read_bytes())}
        result = r.upload(path, "test-bucket", "daily/", s3)
        self.assertTrue(result["ciphertext_readback_verified"])
        self.assertFalse(result["aws_restore_drill_verified"])
        self.assertEqual(s3.get_object.call_args.kwargs["VersionId"], "test-version")
        self.assertEqual(s3.put_object.call_args.kwargs["ServerSideEncryption"], "AES256")
        s3.get_object.return_value = {"Body": io.BytesIO(b"corrupt")}
        with self.assertRaisesRegex(ValueError, "readback"):
            r.upload(path, "test-bucket", "daily/", s3)
        s3.get_bucket_versioning.return_value = {}
        with self.assertRaisesRegex(ValueError, "versioning"):
            r.upload(path, "test-bucket", "daily/", s3)

    def test_live_wal_snapshot_and_restore_revokes_pilot(self):
        from orion.live.ledger import Ledger, Limits, Refused
        from orion.live.accounting import Accounting
        from orion.live.pilot import Pilot
        from datetime import datetime, timezone
        policy = dict(capital='30000', max_order_premium='2000', max_open_premium='5000',
                      daily_loss='1000', fee_reserve='10', max_trade_loss='500', max_open_risk='800',
                      max_lots=1, max_entries=3)
        pilot = Pilot(self.store)
        pilot.configure(self.uid, self.uid, policy)
        pilot.consent(self.uid, 1)
        live = Ledger(self.store.account_dir(self.uid)/'live.db', self.uid)
        try:
            live.db.execute('PRAGMA wal_autocheckpoint=0')
            live.development_resume()
            now = datetime.now(timezone.utc)
            tag = live.reserve(event='test', symbol='TESTCE', segment='nse_fo', lots=1, lot_size=10,
                limit_price='100', tick_size='.05', signal_time=now, quote_time=now, now=now,
                limits=Limits(1, 3, 2000, 1000), realized_loss=0)['tag']
            live.mark_dispatching(tag)
            live.reconcile(tag, account=self.uid, broker_id='test1', symbol='TESTCE', quantity=10,
                           status='FILLED', filled=10, average='100')
            accounting = Accounting(live, 'TESTUCC')
            accounting.bind_multiplier(tag, 1)
            accounting.record(account=self.uid, ucc='TESTUCC', trade_id='fill1', broker_id='test1',
                segment='nse_fo', symbol='TESTCE', side='BUY', quantity=10, price='100', fee='1', executed_at=now)
            with patch('socket.socket', side_effect=AssertionError('Network forbidden')):
                archive, manifest = self.backup()
                restored = self.root/'live-restored'
                report = r.restore(archive, restored, self.backup_key, self.key, self.work)
            self.assertEqual(manifest['counts']['live_databases'], 1)
            self.assertEqual(manifest['counts']['live_fills'], 1)
            self.assertTrue(report['live_ledgers_unchanged'])
            self.assertTrue(report['live_reconciliation_required'])
            self.assertFalse(report['aws_verified'])
            with sqlite3.connect(restored/'portal/accounts.db') as db:
                self.assertEqual(db.execute('SELECT enrolled,consent FROM live_pilots').fetchone(), (0, 0))
            destination = restored/'portal/accounts'/self.uid/'live.db'
            with self.assertRaises(Refused):
                Ledger(destination, self.uid)
            self.assertTrue(pilot.status(self.uid)['reviewed'])
            self.assertEqual(live.get(tag)['filled'], 10)
            self.assertEqual(self.engine.state()['positions'], self.positions)
        finally:
            live.close()


if __name__ == "__main__":
    unittest.main()

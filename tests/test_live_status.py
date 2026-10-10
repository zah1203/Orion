from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from orion.live.ledger import Ledger
from orion.live.status import account_status
import test_live_pilot as fixtures


class StatusTests(unittest.TestCase):
    def test_readonly_missing_existing_stale_and_quarantined(self):
        with tempfile.TemporaryDirectory() as temp:
            uid = 'a'*32
            self.assertEqual(account_status(temp, uid)['state'], 'not-started')
            self.assertFalse((Path(temp)/'accounts').exists())
            path = Path(temp)/'accounts'/uid/'live.db'
            ledger = Ledger(path, uid)
            self.assertEqual(account_status(temp, uid)['state'], 'not-checked')
            ledger.db.execute('CREATE TABLE live_monitor(id INTEGER, checked_at TEXT, state TEXT, exposure INTEGER, uncovered INTEGER, action TEXT)')
            now = datetime.now(timezone.utc)
            ledger.db.execute('INSERT INTO live_monitor VALUES(1,?,?,10,4,?)', (now.isoformat(), 'observed', 'sensitive-not-exposed'))
            before = ledger.db.total_changes
            current = account_status(temp, uid, now=now)
            self.assertTrue(current['fresh'])
            self.assertEqual(current['uncovered'], 4)
            self.assertNotIn('sensitive-not-exposed', json.dumps(current))
            self.assertFalse(current['order_submission_available'])
            self.assertEqual(account_status(temp, uid, now=now+timedelta(seconds=31))['state'], 'stale')
            self.assertFalse(account_status(temp, uid, now=now-timedelta(seconds=1))['fresh'])
            self.assertEqual(before, ledger.db.total_changes)
            ledger.db.execute("INSERT INTO live_incidents VALUES('synthetic')")
            self.assertEqual(account_status(temp, uid, now=now)['state'], 'review-required')
            ledger.close()
            (Path(temp)/'RECOVERY_ONLY').touch()
            self.assertEqual(account_status(temp, uid)['state'], 'recovery-quarantine')

    def test_symlink_wrong_account_and_corrupt_file_not_opened_as_healthy(self):
        with tempfile.TemporaryDirectory() as temp:
            uid = 'a'*32
            path = Path(temp)/'accounts'/uid/'live.db'
            ledger = Ledger(path, 'b'*32)
            ledger.close()
            self.assertEqual(account_status(temp, uid)['state'], 'review-required')
            path.unlink()
            path.write_bytes(b'not a database')
            self.assertEqual(account_status(temp, uid)['state'], 'review-required')
            path.unlink()
            path.symlink_to(Path(temp)/'missing')
            self.assertEqual(account_status(temp, uid)['state'], 'review-required')
            self.assertFalse((Path(temp)/'missing').exists())
            with self.assertRaises(ValueError): account_status(temp, '../escape')


class HealthAPITests(unittest.TestCase):
    setUp = fixtures.PilotTests.setUp
    tearDown = fixtures.PilotTests.tearDown
    login = fixtures.PilotTests.login

    def test_account_isolation_owner_access_and_no_ledger_initialization(self):
        path = '/api/admin/live/health/' + self.user
        self.assertEqual(self.client.get(path).status_code, 200)
        self.login('newuser')
        self.assertEqual(self.client.get(path).status_code, 403)
        response = self.client.get('/api/live/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['account']['state'], 'not-started')
        self.assertFalse(response.json()['release']['live_available'])
        self.assertFalse(list(Path(self.temp.name).rglob('live.db')))
        self.client.post('/api/logout', json={}, headers=self.headers)
        self.assertEqual(self.client.get('/api/live/health').status_code, 401)

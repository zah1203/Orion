from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_live_pilot as fixtures
from test_live_execution import report
from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.ledger import Ledger
from orion.live.monitor import ProtectionMonitor


class ControlsTests(unittest.TestCase):
    setUp = fixtures.PilotTests.setUp
    tearDown = fixtures.PilotTests.tearDown
    login = fixtures.PilotTests.login

    def make_ledger(self):
        ledger = Ledger(self.store.account_dir(self.owner)/'live.db', self.owner)
        h = ExecutionHarness(ledger, 'TESTOWNER', fixtures.LIMITS, SimulatedBroker(), enrolled=True, reviewed=True)
        ProtectionMonitor(ledger, SimpleNamespace(ucc='TESTOWNER'))
        now = datetime.now(timezone.utc)
        h.check_book(report(), report(), now)
        ledger.development_resume()
        tag = h.prepare_entry(event='synthetic', symbol='NIFTYTESTCE', segment='nse_fo', product='NRML',
            token='123', lots=1, lot_size=10, limit_price='100', stop='90', tick='.05',
            signal_time=now, quote_time=now, now=now)
        ledger.db.execute('CREATE TABLE live_worker_health(id INTEGER, checked_at TEXT, stopped INTEGER)')
        ledger.db.execute('INSERT INTO live_worker_health VALUES(1,?,0)', (now.isoformat(),))
        ledger.close()
        return tag

    def payload(self, tag):
        return dict(request_id='1'*32, entry_tag=tag, action='CANCEL', confirmation='CONFIRM LIVE ACTION')

    def test_no_worker_no_creation_no_activation(self):
        self.assertEqual(self.client.get('/api/live/controls').json()['positions'], [])
        response = self.client.post('/api/live/controls', json=self.payload('orion'+'a'*24), headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.post('/api/live/pause', json={}, headers=self.headers).status_code, 409)
        self.assertFalse(list(Path(self.temp.name).rglob('live.db')))
        self.assertEqual(self.client.put('/api/mode', json=dict(mode='live'), headers=self.headers).status_code, 409)

    def test_authenticated_queue_idempotence_pause_and_paper_isolation(self):
        tag = self.make_ledger()
        paper = self.store.account_dir(self.owner)/'paper.db'
        paper.write_bytes(b'paper untouched')
        data = self.client.get('/api/live/controls').json()
        self.assertTrue(data['positions'][0]['can_cancel'])
        self.assertFalse(data['positions'][0]['can_exit'])
        body = self.payload(tag)
        self.assertEqual(self.client.post('/api/live/controls', json=body).status_code, 403)
        first = self.client.post('/api/live/controls', json=body, headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['state'], 'PENDING')
        self.assertEqual(self.client.post('/api/live/controls', json=body, headers=self.headers).json(), first.json())
        self.assertTrue(self.client.get('/api/live/controls').json()['entries_paused'])
        self.assertEqual(self.client.post('/api/live/pause', json={}, headers=self.headers).status_code, 200)
        self.assertEqual(paper.read_bytes(), b'paper untouched')

    def test_other_user_cannot_read_or_control_owner_ledger(self):
        tag = self.make_ledger()
        self.login('newuser')
        self.assertEqual(self.client.get('/api/live/controls').json()['positions'], [])
        self.assertEqual(self.client.post('/api/live/controls', json=self.payload(tag), headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post('/api/live/controls', json=self.payload(tag) | dict(uid=self.owner), headers=self.headers).status_code, 422)

    def test_stale_worker_and_recovery_quarantine_refuse_changes(self):
        tag = self.make_ledger()
        path = self.store.account_dir(self.owner)/'live.db'
        ledger = Ledger(path, self.owner)
        ledger.db.execute('UPDATE live_worker_health SET checked_at=?', ((datetime.now(timezone.utc)-timedelta(seconds=11)).isoformat(),))
        ledger.close()
        self.assertFalse(self.client.get('/api/live/controls').json()['worker_available'])
        self.assertEqual(self.client.post('/api/live/controls', json=self.payload(tag), headers=self.headers).status_code, 409)
        (path.parent/'RECOVERY_ONLY').touch()
        self.assertEqual(self.client.get('/api/live/controls').status_code, 409)

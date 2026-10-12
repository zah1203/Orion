from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import unittest

import test_live_coordinator as fixtures
from test_live_routing import CHANNEL
from orion.live.authorization import revoke
from orion.live.ledger import Refused
from orion.live.startup import EntryCheckedSession
from orion.live.entry_worker import EntryWorker

class EntryPermissionTests(unittest.TestCase):
    setUp = fixtures.CoordinatorTests.setUp
    tearDown = fixtures.CoordinatorTests.tearDown
    message = fixtures.CoordinatorTests.message
    ready = fixtures.CoordinatorTests.ready
    account = fixtures.CoordinatorTests.account
    coordinator = fixtures.CoordinatorTests.coordinator

    def test_expiry_restart_pause_and_stop_refuse_before_network(self):
        for mutation in (
            "UPDATE entry_permission SET day='2000-01-01'",
            "UPDATE entry_permission SET worker_session='old-worker'",
            "UPDATE entry_permission SET active=0",
            "UPDATE control SET paused=1",
        ):
            with self.subTest(mutation=mutation), self.account() as a:
                c = self.coordinator(a)
                c.db.execute(mutation)
                with patch('orion.live.status.release_status', return_value={'order_submission_available': True}):
                    with self.assertRaises(Refused): c.authority.require()
                self.assertFalse(self.session.operations)
                c.db.execute('DROP TABLE live_source_health')

    def test_checked_transport_requires_exact_durable_permission(self):
        self.message()
        with self.account() as a:
            c = self.coordinator(a)
            raw = self.session
            checked = EntryCheckedSession(raw, c.authority.transport)
            a.session = checked
            a.monitor.commands.session = checked
            with patch('orion.live.status.release_status', return_value={'order_submission_available': True}):
                with self.assertRaises(Refused):
                    checked.request('place', request={'kind':'ENTRY', 'tag':'forged'})
                result = c.submit(CHANNEL+':1')
                self.assertFalse(result['fill_confirmed'])
                self.assertEqual(raw.operations.count('place'), 1)
                self.assertEqual(c.db.execute('SELECT active FROM entry_permission').fetchone()[0], 2)
                self.assertTrue(c.authority.allowed())
                c.db.execute('UPDATE execution_attempts SET at=?', ((datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat(),))
                self.assertFalse(c.authority.allowed())

    def test_pause_revokes_permission(self):
        with self.account() as a:
            c = self.coordinator(a)
            revoke(a.ledger)
            self.assertEqual(c.db.execute('SELECT active FROM entry_permission').fetchone()[0], 0)

    def test_shadow_checks_once_without_orders(self):
        self.message()
        with self.account() as a:
            self.coordinator(a)
            worker = EntryWorker(a)
            revoke(a.ledger)
            a.monitor._health('observed', 'none')
            self.assertEqual(worker.cycle(), 'checked')
            self.assertEqual(worker.cycle(), 'idle')
            self.assertNotIn('place', self.session.operations)
            self.assertFalse(a.ledger.db.execute('SELECT 1 FROM intents').fetchone())

    def test_monitoring_priority_and_sanitized_failure(self):
        self.message()
        with self.account() as a:
            self.coordinator(a)
            worker = EntryWorker(a)
            self.assertEqual(worker.cycle(), 'monitoring-first')
            a.monitor._health('observed', 'none')
            with patch.object(worker.coordinator, 'evaluate', side_effect=RuntimeError('private-secret')):
                self.assertEqual(worker.cycle(), 'refused')
            self.assertNotIn('private-secret', str(list(worker.db.iterdump())))
            self.assertEqual(worker.cycle(), 'idle')

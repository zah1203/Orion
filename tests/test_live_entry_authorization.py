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
                self.assertEqual(c.db.execute('SELECT active FROM entry_permission').fetchone()[0], 1)
                self.assertTrue(c.authority.allowed())
                c.db.execute('UPDATE execution_attempts SET at=?', ((datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat(),))
                self.assertFalse(c.authority.allowed())
                self.assertEqual(c.db.execute('SELECT active FROM entry_permission').fetchone()[0], 1)

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

    def test_legacy_permission_is_never_upgraded(self):
        with self.account() as a:
            c = self.coordinator(a)
            for scope in ('one-entry', 'session'):
                c.db.execute("UPDATE entry_permission SET scope=?,active=1", (scope,))
                with patch('orion.live.status.release_status', return_value={'order_submission_available': True}):
                    self.assertFalse(c.authority.allowed())
                    with self.assertRaises(Refused): c.authority.resume_when_flat()
                    with self.assertRaises(Refused): c.authority.resume_when_protected()
                self.assertEqual(c.db.execute('SELECT active FROM entry_permission').fetchone()[0], 0)

    def test_flat_internal_pause_resumes_same_grant_but_revocation_does_not(self):
        with self.account() as a:
            c = self.coordinator(a)
            nonce = c.db.execute('SELECT nonce FROM entry_permission').fetchone()[0]
            a.ledger.pause()
            with patch('orion.live.status.release_status', return_value={'order_submission_available': True}):
                self.assertFalse(c.authority.allowed())
                c.authority.resume_when_flat()
                self.assertTrue(c.authority.allowed())
                self.assertEqual(c.db.execute('SELECT nonce FROM entry_permission').fetchone()[0], nonce)
                revoke(a.ledger)
                with self.assertRaises(Refused): c.authority.resume_when_flat()

    def test_stale_source_cannot_resume_session(self):
        with self.account() as a:
            c = self.coordinator(a)
            a.ledger.pause()
            c.db.execute("UPDATE live_source_health SET checked_at='2000-01-01T00:00:00+00:00'")
            with patch('orion.live.status.release_status', return_value={'order_submission_available': True}):
                with self.assertRaises(Refused): c.authority.resume_when_flat()
            self.assertEqual(c.db.execute('SELECT paused FROM control').fetchone()[0], 1)

    def test_schema_migration_preserves_legacy_scope(self):
        import sqlite3
        from orion.live.authorization import install
        db = sqlite3.connect(':memory:')
        try:
            db.execute('CREATE TABLE entry_permission(id INTEGER PRIMARY KEY, nonce TEXT, worker_session TEXT, day TEXT, policy_digest TEXT, active INTEGER, reviewed_at TEXT)')
            db.execute("INSERT INTO entry_permission VALUES(1,'n','s','d','p',1,'t')")
            install(db)
            install(db)
            self.assertEqual(db.execute('SELECT active,scope FROM entry_permission').fetchone(), (1,'one-entry'))
        finally:
            db.close()

    def test_two_sequential_entries_share_grant_after_confirmed_flat_cancel(self):
        from test_live_execution import report
        from orion.live.reconciliation import Reconciliation
        self.message()
        with self.account() as a:
            c = self.coordinator(a)
            nonce = c.db.execute('SELECT nonce FROM entry_permission').fetchone()[0]
            with patch('orion.live.status.release_status', return_value={'order_submission_available': True}):
                c.submit(CHANNEL+':1')
                tag = c.db.execute('SELECT tag FROM intents').fetchone()[0]
                a.ledger.reconcile(tag, account=a.uid, broker_id='broker1', symbol='NIFTYTESTCE',
                    quantity=10, status='CANCELLED', filled=0, average='0')
                c.db.execute("UPDATE broker_commands SET status='CONFIRMED'")
                a.ledger.pause()
                now = datetime.now(timezone.utc)
                order = dict(actId='TESTOWNER', nOrdNo='broker1', exSeg='nse_fo', prod='NRML',
                    tok='123', trdSym='NIFTYTESTCE', qty=10, fldQty=0, ordSt='cancelled',
                    trnsTp='B', avgPrc='0', prc='101', trgPrc='0', prcTp='L')
                Reconciliation(a.ledger, 'TESTOWNER').compare(report(order), report(),
                    started_at=now, completed_at=now, complete=True)
                c.authority.resume_when_flat()
                self.message(message_id=2)
                original = self.session.request
                def second(operation, **kwargs):
                    if operation == 'place':
                        self.session.operations.append('place')
                        return {'broker_id':'broker2'}
                    return original(operation, **kwargs)
                self.session.request = second
                c.submit(CHANNEL+':2')
                self.assertEqual(self.session.operations.count('place'), 2)
                self.assertEqual(c.db.execute('SELECT nonce FROM entry_permission').fetchone()[0], nonce)
                self.assertEqual(c.db.execute('SELECT COUNT(DISTINCT nonce) FROM entry_dispatch_permission').fetchone()[0], 1)

    def test_second_buy_requires_confirmed_protection_and_preserves_session(self):
        from orion.live.strategy import ExitStrategy
        from orion.live.portfolio import PortfolioMonitor
        from test_live_execution import report
        self.message()
        with self.account() as a:
            c = self.coordinator(a)
            with patch('orion.live.status.release_status', return_value={'order_submission_available': True}):
                c.submit(CHANNEL+':1')
                tag = c.db.execute('SELECT tag FROM intents').fetchone()[0]
                a.ledger.reconcile(tag,account=a.uid,broker_id='broker1',symbol='NIFTYTESTCE',quantity=10,status='FILLED',filled=10,average='100')
                c.preparation.accounting.record(account=a.uid,ucc=a.session.ucc,trade_id='buy1',broker_id='broker1',
                    segment='nse_fo',symbol='NIFTYTESTCE',side='BUY',quantity=10,price='100',fee='0',executed_at=datetime.now(timezone.utc))
                c.db.execute("UPDATE broker_commands SET status='CONFIRMED'")
                c.db.execute("INSERT INTO protective_exits VALUES('stop1',?,10,'90','OPEN','stopbroker',0)",(tag,))
                c.db.execute("INSERT INTO exit_terms VALUES('stop1','89.95')")
                ExitStrategy(a.ledger,a.session).bind(tag,targets=['110','120','130'],stop_limit='89.95',target_timeout=10)
                now = datetime.now(timezone.utc)
                buy = dict(actId=a.session.ucc,nOrdNo='broker1',exSeg='nse_fo',prod='NRML',tok='123',trdSym='NIFTYTESTCE',qty=10,fldQty=10,ordSt='complete',trnsTp='B',avgPrc='100',prc='101',trgPrc='0',prcTp='L')
                stop = buy | dict(nOrdNo='stopbroker',trnsTp='S',fldQty=0,ordSt='trigger pending',avgPrc='0',prc='89.95',trgPrc='90',prcTp='SL')
                position = dict(actId=a.session.ucc,exSeg='nse_fo',prod='NRML',tok='123',trdSym='NIFTYTESTCE',cfBuyQty=0,cfSellQty=0,flBuyQty=10,flSellQty=0)
                c.preparation.reconciliation.compare(report(buy,stop),report(position),started_at=now,completed_at=now,complete=True)
                c.authority.resume_when_protected()
                nonce = c.db.execute('SELECT nonce FROM entry_permission').fetchone()[0]
                self.message(message_id=2)
                original = self.session.request
                def second(operation, **kwargs):
                    if operation == 'place':
                        self.session.operations.append('place')
                        return {'broker_id':'broker2'}
                    return original(operation, **kwargs)
                self.session.request = second
                c.submit(CHANNEL+':2')
                self.assertEqual(self.session.operations.count('place'),2)
                self.assertEqual(c.db.execute('SELECT nonce FROM entry_permission').fetchone()[0],nonce)
                a.monitor = PortfolioMonitor(a.ledger,a.session)
                a.monitor._health('observed','none')
                self.assertEqual(EntryWorker(a).cycle(),'monitoring-first')
                self.assertEqual(c.db.execute('SELECT active FROM entry_permission').fetchone()[0],1)

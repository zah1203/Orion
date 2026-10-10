import fcntl
from unittest.mock import Mock
import unittest

import test_live_routing as routing
from orion.live.ledger import Refused
from orion.live.startup import ReviewedAccountMonitor, ProtectiveSession


class StartupTests(unittest.TestCase):
    setUp = routing.RoutingTests.setUp
    tearDown = routing.RoutingTests.tearDown

    def start(self, **changes):
        args = dict(store=self.store,uid=self.owner,policy_version=1,totp='123456',
                    _session_factory=self.factory)
        return ReviewedAccountMonitor(**(args | changes))

    def fake(self, ucc='TESTOWNER'):
        self.session=Mock(ucc=ucc)
        self.factory=Mock(return_value=self.session)

    def test_reviewed_startup_keeps_lease_and_blocks_buy_before_transport(self):
        self.fake()
        paper=self.store.account_dir(self.owner)/'paper.db'
        paper.write_bytes(b'untouched paper')
        with self.start() as worker:
            self.assertIsInstance(worker.session,ProtectiveSession)
            self.assertIsNone(worker._totp)
            with self.assertRaises(Refused):
                worker.session.request('place',request={'kind':'ENTRY'})
            self.session.request.assert_not_called()
            with open(paper.parent/'worker.lock','a') as lease:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)
            worker.session.request('snapshot')
            self.session.request.assert_called_once_with('snapshot')
        self.session.close.assert_called_once()
        self.assertEqual(paper.read_bytes(),b'untouched paper')
        self.assertEqual(self.factory.call_count,1)

    def test_busy_paper_lease_does_not_login_and_discards_code(self):
        self.fake()
        worker=self.start()
        with open(self.store.account_dir(self.owner)/'worker.lock','a') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Refused):
                with worker: pass
        self.factory.assert_not_called()
        self.assertIsNone(worker._totp)
        with self.assertRaises(Refused):
            with worker: pass
        self.factory.assert_not_called()

    def test_revoked_or_stale_policy_refuses_before_login(self):
        self.fake()
        with self.assertRaises(Refused):
            with self.start(policy_version=2): pass
        self.pilot.revoke(self.owner,self.owner)
        with self.assertRaises(Refused):
            with self.start(): pass
        self.factory.assert_not_called()

    def test_reviewed_new_credentials_cannot_rebind_existing_ledger(self):
        self.fake()
        self.store.save_credentials(self.owner,{'kotak_ucc':'REPLACEMENT'})
        self.pilot.configure(self.owner,self.owner,routing.LIMITS)
        version=self.pilot.status(self.owner)['version']
        self.pilot.consent(self.owner,version)
        with self.assertRaises(Refused):
            with self.start(policy_version=version): pass
        self.factory.assert_not_called()

    def test_changed_policy_during_login_closes_session_without_worker_handoff(self):
        self.fake()
        def login(*unused):
            # Simulate a concurrent direct database change without reentering flock.
            with self.store.db() as db:
                db.execute('UPDATE live_pilots SET consent=0 WHERE uid=?',(self.owner,))
            return self.session
        self.factory.side_effect=login
        worker=self.start()
        with self.assertRaises(Refused):
            with worker: pass
        self.session.close.assert_called_once()
        self.session.request.assert_not_called()
        self.assertIsNone(worker._totp)
        self.assertIsNone(worker.lease)

    def test_login_failure_is_not_automatically_retried(self):
        self.fake()
        self.factory.side_effect=RuntimeError('synthetic sensitive text')
        worker=self.start()
        for _ in range(2):
            with self.assertRaises(Refused) as error:
                with worker: pass
            self.assertNotIn('sensitive',str(error.exception))
        self.assertEqual(self.factory.call_count,1)
        self.assertIsNone(worker._totp)
        self.assertIsNone(worker.lease)

    def test_wrong_broker_session_is_closed_before_worker_handoff(self):
        self.fake(ucc='OTHER')
        with self.assertRaises(Refused):
            with self.start(): pass
        self.session.close.assert_called_once()
        self.session.request.assert_not_called()

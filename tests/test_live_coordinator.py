from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_live_preparation as fixtures
from test_live_preflight import Session as FundingSession
from test_live_routing import CHANNEL
from orion.live.coordinator import EntryCoordinator
from orion.live.ledger import Refused


class Session(FundingSession):
    def __init__(self):
        super().__init__()
        self.operations=[]
        self.fail_place=False
        self.on_place=lambda:None
        self.after_funding=lambda:None
        self.market_change={}
    def close(self): pass
    def request(self, operation, **kwargs):
        self.operations.append(operation)
        at=datetime.now(timezone.utc).isoformat()
        if operation=='market':
            return dict(ucc=self.ucc,segment='nse_fo',status_code=1,observed_at=at,order_submission_available=False)|self.market_change
        if operation=='quotes':
            return dict(ucc=self.ucc,quotes=[dict(token='123',symbol='NIFTYTESTCE',segment='nse_fo',bid='100',ask='101',quoted_at=at,observed_at=at)])
        if operation=='funding':
            result=super().request(operation,**kwargs)
            self.after_funding()
            return result
        if operation=='place':
            self.on_place()
            if self.fail_place: raise TimeoutError('private broker response')
            return {'broker_id':'broker1'}
        raise AssertionError(operation)


class CoordinatorTests(unittest.TestCase):
    setUp=fixtures.PreparationTests.setUp
    tearDown=fixtures.PreparationTests.tearDown
    message=fixtures.PreparationTests.message
    ready=fixtures.PreparationTests.ready

    def account(self):
        from orion.live.monitor import AccountMonitor
        self.session=Session()
        return AccountMonitor(self.store,self.owner,lambda:self.session)

    def coordinator(self,a):
        c=EntryCoordinator(a)
        c.db.execute('CREATE TABLE live_source_health(id INTEGER,checked_at TEXT,state TEXT)')
        c.db.execute("INSERT INTO live_source_health VALUES(1,?,'connected')",(datetime.now(timezone.utc).isoformat(),))
        self.ready(a)
        return c

    def test_readonly_evaluation_uses_bound_market_quote_and_cash_without_mutation(self):
        self.message()
        with self.account() as a:
            c=self.coordinator(a)
            before=list(c.db.iterdump())
            result=c.evaluate(CHANNEL+':1')
            self.assertEqual(self.session.operations,['market','quotes','funding'])
            self.assertEqual(result['candidate']['quantity'],10)
            self.assertFalse(result['order_submission_available'])
            self.assertEqual(list(c.db.iterdump()),before)

    def test_real_release_gate_refuses_before_network_or_reservation(self):
        self.message()
        with self.account() as a:
            c=self.coordinator(a)
            with self.assertRaisesRegex(Refused,'activation'): c.submit(CHANNEL+':1')
            self.assertFalse(self.session.operations)
            self.assertFalse(c.db.execute('SELECT 1 FROM intents').fetchone())

    def test_atomic_send_is_once_ack_not_fill_and_policy_locked(self):
        self.message()
        with self.account() as a:
            c=self.coordinator(a)
            def committed():
                self.assertFalse(c.db.in_transaction)
                self.assertEqual(c.db.execute('SELECT status FROM intents').fetchone()[0],'DISPATCHING')
                self.assertEqual(c.db.execute('SELECT COUNT(*) FROM execution_attempts').fetchone()[0],1)
                self.assertEqual(c.db.execute('SELECT status FROM broker_commands').fetchone()[0],'SENDING')
            self.session.on_place=committed
            with patch('orion.live.coordinator.release_status',return_value={'order_submission_available':True}):
                result=c.submit(CHANNEL+':1')
                with self.assertRaises(Refused): c.submit(CHANNEL+':1')
            self.assertFalse(result['fill_confirmed'])
            self.assertEqual(self.session.operations.count('place'),1)
            self.assertEqual(c.db.execute('SELECT COUNT(*) FROM execution_attempts').fetchone()[0],1)
            self.assertEqual(c.db.execute('SELECT filled FROM intents').fetchone()[0],0)
            self.assertEqual(c.db.execute('SELECT status FROM broker_commands').fetchone()[0],'ACKNOWLEDGED')

    def test_timeout_is_unknown_pauses_and_never_resends(self):
        self.message()
        with self.account() as a:
            c=self.coordinator(a); self.session.fail_place=True
            with patch('orion.live.coordinator.release_status',return_value={'order_submission_available':True}):
                with self.assertRaises(Refused): c.submit(CHANNEL+':1')
                with self.assertRaises(Refused): c.submit(CHANNEL+':1')
            self.assertEqual(self.session.operations.count('place'),1)
            self.assertEqual(c.db.execute('SELECT status FROM intents').fetchone()[0],'UNKNOWN')
            self.assertEqual(c.db.execute('SELECT paused FROM control').fetchone()[0],1)

    def test_revocation_during_funding_prevents_any_reservation(self):
        self.message()
        with self.account() as a:
            c=self.coordinator(a)
            self.session.after_funding=lambda:self.pilot.revoke(self.owner,self.owner)
            with patch('orion.live.coordinator.release_status',return_value={'order_submission_available':True}):
                with self.assertRaises(Refused): c.submit(CHANNEL+':1')
            self.assertFalse(c.db.execute('SELECT 1 FROM intents').fetchone())
            self.assertNotIn('place',self.session.operations)

    def test_closed_missing_stale_wrong_account_and_boolean_market_refused(self):
        self.message()
        with self.account() as a:
            c=self.coordinator(a)
            for changes in (dict(status_code=2),dict(status_code=None),dict(status_code=True),dict(ucc='OTHER'),
                            dict(observed_at=(datetime.now(timezone.utc)-timedelta(seconds=6)).isoformat())):
                self.session.market_change=changes
                with self.subTest(changes=changes),self.assertRaises(Refused): c.evaluate(CHANNEL+':1')
            self.assertNotIn('quotes',self.session.operations)

    def test_source_disconnect_after_funding_prevents_prepare(self):
        self.message()
        with self.account() as a:
            c=self.coordinator(a)
            self.session.after_funding=lambda:c.db.execute("UPDATE live_source_health SET state='disconnected'")
            with self.assertRaises(Refused): c.evaluate(CHANNEL+':1')
            self.assertFalse(c.db.execute('SELECT 1 FROM intents').fetchone())

    def test_plan_binding_failure_rolls_back_all_dispatch_writes(self):
        self.message()
        with self.account() as a:
            c=self.coordinator(a)
            with patch('orion.live.coordinator.release_status',return_value={'order_submission_available':True}), \
                    patch.object(c.preparation.plans,'_bind_locked',side_effect=Refused('synthetic bind failure')):
                with self.assertRaises(Refused): c.submit(CHANNEL+':1')
            for table in ('intents','execution_attempts','execution_terms','entry_exit_plans','broker_commands'):
                self.assertFalse(c.db.execute(f'SELECT 1 FROM {table}').fetchone())
            self.assertNotIn('place',self.session.operations)

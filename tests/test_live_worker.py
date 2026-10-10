import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
import fcntl
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_live_routing as routing
import test_live_strategy as strategy
from orion.live.ledger import Refused
from orion.live.worker import serve_account

class WorkerTests(unittest.IsolatedAsyncioTestCase):
    setUp = routing.RoutingTests.setUp
    tearDown = routing.RoutingTests.tearDown

    async def test_busy_paper_slot_blocks_both_factories(self):
        session, client = Mock(), Mock()
        with open(self.store.account_dir(self.owner)/'worker.lock','a') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Refused):
                await serve_account(self.store,self.owner,lambda:self.master,session,stop=asyncio.Event(),_client_factory=client)
        session.assert_not_called(); client.assert_not_called()

    async def run_worker(self,uid,ucc,*,broker_failure=False):
        stop = asyncio.Event()
        calls = []
        source_text = self.text
        class Client:
            def add_event_handler(self,handler,*args): self.handler = handler
            async def connect(self): calls.append('telegram-connect')
            async def is_user_authorized(self): return True
            async def run_until_disconnected(self):
                await self.handler(SimpleNamespace(chat_id=int(routing.CHANNEL),id=1,raw_text=source_text,
                    message=SimpleNamespace(date=datetime.now(timezone.utc),edit_date=None,reply_to_msg_id=None)))
                calls.append('telegram-disconnect')
            async def disconnect(self): calls.append('telegram-close')
        class Session:
            def request(self,operation,**kwargs):
                calls.append(operation)
                if calls.count(operation) >= 8: stop.set()
                if broker_failure: raise Refused('synthetic broker failure')
                now=datetime.now(timezone.utc).isoformat()
                return dict(ucc=ucc,orders=[],positions=[],started_at=now,completed_at=now)
            def close(self): calls.append('broker-close')
        session=Session(); session.ucc=ucc
        await asyncio.wait_for(serve_account(self.store,uid,lambda:self.master,lambda:session,
            stop=stop,collect_trades=False,poll_seconds=.01,_client_factory=lambda _:Client()),timeout=3)
        return calls

    async def test_disconnect_keeps_book_monitoring_and_closes_resources(self):
        calls = await self.run_worker(self.owner,'TESTOWNER')
        self.assertGreater(calls.count('snapshot'),1)
        self.assertGreater(calls.index('broker-close'),calls.index('telegram-disconnect'))
        self.assertNotIn('place',calls)
        row=self.ledgers[self.owner].db.execute('SELECT * FROM live_worker_health').fetchone()
        self.assertEqual(row['stopped'],1)
        self.assertEqual(row['monitor_state'],'observed')
        self.assertEqual(self.ledgers[self.owner].db.execute('SELECT accepted FROM live_source_health').fetchone()[0],1)
        self.assertEqual(self.ledgers[self.owner].db.execute('SELECT state FROM live_signals').fetchone()[0],'INVALIDATED')
        self.assertFalse(self.ledgers[self.other].db.execute("SELECT 1 FROM sqlite_master WHERE name='live_worker_health'").fetchone())
        with open(self.store.account_dir(self.owner)/'worker.lock','a') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def test_two_account_workers_keep_separate_sessions_and_health(self):
        results = await asyncio.gather(self.run_worker(self.owner,'TESTOWNER'),self.run_worker(self.other,'TESTSECOND'))
        for uid,calls in zip((self.owner,self.other),results):
            self.assertEqual(calls.count('broker-close'),1)
            self.assertFalse(self.ledgers[uid].db.execute('SELECT 1 FROM intents').fetchone())

    async def test_broker_failure_records_review_and_never_reauthenticates(self):
        calls = await self.run_worker(self.owner,'TESTOWNER',broker_failure=True)
        row=self.ledgers[self.owner].db.execute('SELECT * FROM live_worker_health').fetchone()
        self.assertEqual(row['monitor_state'],'review-required')
        self.assertEqual(calls.count('broker-close'),1)
        self.assertTrue(self.ledgers[self.owner].db.execute('SELECT 1 FROM live_alerts').fetchone())

class ExposureWorkerTests(unittest.IsolatedAsyncioTestCase):
    setUp = strategy.StrategyTests.setUp
    tearDown = strategy.StrategyTests.tearDown

    async def test_source_failure_still_places_protection_for_confirmed_exposure(self):
        self.strategy.bind_stop_limit(self.tag,'89.95')
        stop=asyncio.Event(); calls=[]
        original=self.session.request
        def request(operation,**kwargs):
            calls.append(operation)
            if calls.count('snapshot') >= 3: stop.set()
            return original(operation,**kwargs)
        self.session.request=request
        store=SimpleNamespace(user=lambda _: {},account_dir=lambda _:self.path.parent,
            credentials=Mock(side_effect=Refused('source unavailable')),lock=lambda _:nullcontext())
        await asyncio.wait_for(serve_account(store,'a'*32,lambda:{},lambda:self.session,
            stop=stop,collect_trades=False,poll_seconds=.01),timeout=3)
        self.assertEqual(len(self.session.sdk.stops),1)
        self.assertEqual(self.session.sdk.stops[0]['trnsTp'],'S')
        self.assertEqual(self.session.sdk.stops[0]['qty'],3)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0],1)

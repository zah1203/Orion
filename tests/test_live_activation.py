import asyncio
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import errno
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_live_routing as fixtures
import test_live_pilot as api_fixtures
from orion.live.activation import (ActivationMonitor, ActivationServer, CONFIRMATION,
    preflight, release_if_flat, request_stop, send_request, snapshot)
from orion.live.ledger import Refused
from orion.portal.supervisor import Supervisor
from scripts.live_deploy_guard import acquire_deployment_lease, require_released_slots


def connections(store, uid):
    store.save_credentials(uid, dict(kotak_consumer_key='fake', kotak_mobile='fake', kotak_mpin='fake',
        telegram_session='fake', telegram_api_id='1', telegram_api_hash='fake'))


class ActivationTests(unittest.TestCase):
    setUp = fixtures.RoutingTests.setUp
    tearDown = fixtures.RoutingTests.tearDown

    def prepare(self):
        connections(self.store, self.owner)
        version = self.pilot.status(self.owner)['version']
        self.pilot.consent(self.owner, version)
        calls = []
        class Session:
            ucc = 'TESTOWNER'
            def request(self, operation, **kwargs):
                calls.append(operation)
                now = datetime.now(timezone.utc).isoformat()
                return dict(ucc=self.ucc, orders=[], positions=[], trades=[], fees_verified=False,
                    available_cash_verified=False, started_at=now, completed_at=now)
            def close(self): calls.append('close')
        factory = Mock(return_value=Session())
        return ActivationMonitor(self.store, self.owner, version, '123456', _session_factory=factory), factory, calls

    def paper(self, status=None):
        path = self.store.account_dir(self.owner)/'paper.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE state(id INTEGER, body TEXT)')
            db.execute('INSERT INTO state VALUES(1,?)', (json.dumps(dict(positions={} if status is None else {'p': dict(status=status)})),))
            db.commit()
        return path

    def test_new_ledger_created_paused_without_modifying_paper_and_slot_survives_exit(self):
        self.ledgers.pop(self.owner).close()
        path = self.store.account_dir(self.owner)/'live.db'
        path.unlink()
        paper = self.paper()
        before = paper.read_bytes()
        monitor, factory, calls = self.prepare()
        with monitor as account:
            self.assertTrue(self.store.live_slot_reserved(self.owner))
            self.assertEqual(account.ledger.db.execute('SELECT paused FROM control').fetchone()[0], 1)
            self.assertEqual(account.ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()[0], 'TESTOWNER')
            with self.assertRaises(Refused):
                account.session.request('place', request={'kind': 'ENTRY'})
            with self.assertRaises(ValueError): self.store.set_enabled(self.owner, True)
            self.assertFalse(Supervisor(self.store,'unused').wanted(self.store.user(self.owner)))
            self.assertFalse(release_if_flat(account))
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(calls, ['close'])
        self.assertEqual(paper.read_bytes(), before)
        self.assertTrue(self.store.live_slot_reserved(self.owner))
        self.assertEqual(snapshot(self.store,self.owner)['state'], 'authentication-required')

    def test_busy_paper_slot_refuses_before_login_or_reservation(self):
        monitor, factory, _ = self.prepare()
        with open(self.store.account_dir(self.owner)/'worker.lock','a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Refused):
                with monitor: pass
        factory.assert_not_called()
        self.assertFalse(self.store.live_slot_reserved(self.owner))

    def test_open_paper_or_enabled_paper_refuses_without_changes(self):
        paper = self.paper('OPEN')
        before = paper.read_bytes()
        monitor, factory, _ = self.prepare()
        with self.assertRaises(Refused):
            with monitor: pass
        factory.assert_not_called()
        self.assertEqual(paper.read_bytes(), before)
        paper.unlink()
        monitor, factory, _ = self.prepare()
        self.store.set_enabled(self.owner, True)
        with self.assertRaises(Refused):
            with monitor: pass
        self.assertTrue(self.store.user(self.owner)['enabled'])
        factory.assert_not_called()

    def test_stale_policy_refuses_before_ledger_creation(self):
        monitor, factory, _ = self.prepare()
        self.ledgers.pop(self.owner).close()
        path = self.store.account_dir(self.owner)/'live.db'; path.unlink()
        self.pilot.revoke(self.owner,self.owner)
        with self.assertRaises(Refused):
            with monitor: pass
        self.assertFalse(path.exists()); factory.assert_not_called()

    def test_stop_requires_fresh_flat_reconciliation_and_no_incident(self):
        monitor, factory, calls = self.prepare()
        with monitor as account:
            request_stop(self.store,self.owner)
            self.assertFalse(release_if_flat(account))
            account.cycle()
            account.ledger.db.execute("INSERT INTO live_incidents VALUES('synthetic')")
            self.assertFalse(release_if_flat(account))
            account.ledger.db.execute("DELETE FROM live_incidents WHERE code='synthetic'")
            self.assertTrue(release_if_flat(account))
            self.assertFalse(self.store.live_slot_reserved(self.owner))
            self.assertFalse(self.store.user(self.owner)['enabled'])
        self.assertEqual(calls,['evidence','close'])

    def test_failed_login_is_not_retried_and_keeps_paper_blocked(self):
        monitor, factory, _ = self.prepare()
        factory.side_effect = RuntimeError('sensitive fixture')
        with self.assertRaises(Refused) as error:
            with monitor: pass
        self.assertNotIn('sensitive',str(error.exception))
        self.assertIsNone(monitor._totp)
        self.assertTrue(self.store.live_slot_reserved(self.owner))
        with self.assertRaises(ValueError): self.store.set_enabled(self.owner, True)
        with self.assertRaises(Refused):
            with monitor: pass
        self.assertEqual(factory.call_count,1)

    def test_dead_transport_ends_worker_but_keeps_reservation_for_explicit_authentication(self):
        monitor, factory, calls = self.prepare()
        with monitor as account:
            factory.return_value.closed = True
            self.assertTrue(release_if_flat(account))
            self.assertTrue(self.store.live_slot_reserved(self.owner))
            with self.assertRaises(ValueError): self.store.set_enabled(self.owner, True)
        self.assertEqual(calls, ['close'])

    def test_deploy_guard_blocks_reserved_and_corrupt_state(self):
        monitor, _, _ = self.prepare()
        require_released_slots(self.store.root)
        with monitor:
            with self.assertRaises(RuntimeError): require_released_slots(self.store.root)
        root = self.store.root/'test-deploy'
        with closing(acquire_deployment_lease(root)):
            with open(root/'run/orion-live/deployment.lock','r') as startup:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(startup,fcntl.LOCK_SH | fcntl.LOCK_NB)
        target = root/'private-target'; target.write_text('untouched')
        lock = root/'run/orion-live/deployment.lock'; lock.unlink(); lock.symlink_to(target)
        with self.assertRaises(OSError): acquire_deployment_lease(root)
        self.assertEqual(target.read_text(), 'untouched')


class ActivationAPITests(unittest.TestCase):
    setUp = api_fixtures.PilotTests.setUp
    tearDown = api_fixtures.PilotTests.tearDown
    login = api_fixtures.PilotTests.login

    def prepare(self):
        connections(self.store,self.owner)
        settings = self.store.user(self.owner)['settings']
        self.store.save_settings(self.owner,settings | dict(channels={fixtures.CHANNEL:dict(profile='index',products=['NIFTY'])}))
        self.pilot.configure(self.owner,self.owner,api_fixtures.LIMITS)
        self.pilot.consent(self.owner,1)
        return dict(policy_version=1,totp='123456',confirmation=CONFIRMATION)

    def test_start_authenticated_csrf_own_account_and_release_stays_closed(self):
        payload = self.prepare()
        path = '/api/live/worker/start'
        with patch('orion.live.activation.send_request',new_callable=AsyncMock) as send:
            send.return_value = dict(state='started-paused',order_submission_available=False)
            self.assertEqual(self.client.post(path,json=payload).status_code,403)
            self.assertEqual(self.client.post(path,json=payload | dict(uid=self.user),headers=self.headers).status_code,422)
            result = self.client.post(path,json=payload,headers=self.headers)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(send.call_args.args[0]['uid'],self.owner)
            self.assertFalse(result.json()['order_submission_available'])
            self.assertEqual(self.client.put('/api/mode',json=dict(mode='live'),headers=self.headers).status_code,409)
        self.assertFalse((self.store.account_dir(self.owner)/'live.db').exists())

    def test_stop_cannot_target_another_account(self):
        with self.store.db() as db:
            db.execute('INSERT INTO live_worker_slots VALUES(?,1,1,0)',(self.owner,))
        self.login('newuser')
        result = self.client.post('/api/live/worker/stop',json=dict(confirmation='STOP ONLY AFTER VERIFIED FLAT'),headers=self.headers)
        self.assertEqual(result.status_code,409)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT stop_requested FROM live_worker_slots').fetchone()[0],0)


class ActivationIPCTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.RoutingTests.setUp
    tearDown = fixtures.RoutingTests.tearDown

    async def test_socket_failure_keeps_paper_supervision_and_unknown_file_intact(self):
        supervisor = Supervisor(self.store,'unused')
        supervisor.tick = AsyncMock(side_effect=asyncio.CancelledError)
        with patch.dict('os.environ',{'ORION_PORTAL_KEY_FILE':'unused'}), \
                patch('orion.live.activation.ActivationServer.start',new_callable=AsyncMock,side_effect=OSError('private text')), \
                patch('builtins.print') as output:
            with self.assertRaises(asyncio.CancelledError):
                await supervisor.run()
        self.assertEqual(supervisor.tick.call_count,1)
        self.assertNotIn('private text',repr(output.call_args_list))
        path=Path(self.temp.name)/'activation.sock'; path.write_text('unrelated file')
        server=ActivationServer(self.store,'unused','unused',socket_path=path)
        with self.assertRaises(Refused): await server.start()
        await server.close()
        self.assertEqual(path.read_text(),'unrelated file')

    async def test_stream_handler_validates_and_never_returns_secret_exception(self):
        server = ActivationServer(self.store,'unused','unused')
        server.launch = AsyncMock(side_effect=RuntimeError('private synthetic value'))
        reader = Mock(); reader.readline = AsyncMock(return_value=b'{"totp":"private synthetic value"}\n')
        writer = Mock(); writer.drain = AsyncMock()
        await server.handle(reader,writer)
        server.launch.assert_not_called()
        self.assertEqual(json.loads(writer.write.call_args.args[0]),{'state':'startup-refused'})
        payload=dict(uid=self.owner,policy_version=1,totp='123456',confirmation=CONFIRMATION)
        reader.readline.return_value=json.dumps(payload).encode()+b'\n'
        await server.handle(reader,writer)
        self.assertEqual(json.loads(writer.write.call_args.args[0]),{'state':'startup-refused'})
        self.assertEqual(server.launch.call_count,1)

    async def test_private_socket_sanitized_response_and_lost_connection(self):
        path = Path(self.temp.name)/'run'/'activation.sock'
        server = ActivationServer(self.store,'unused','unused',socket_path=path)
        server.launch = AsyncMock(return_value={'state':'started-paused'})
        try:
            await server.start()
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                self.skipTest('Runtime denies Unix sockets; run this integration check in CI/Linux host')
            raise
        try:
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            result = await send_request(dict(uid=self.owner,policy_version=1,totp='123456',confirmation=CONFIRMATION),socket_path=path)
            self.assertEqual(result,dict(state='started-paused',order_submission_available=False))
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b'{"totp":"sensitive fixture"}\n'); await writer.drain()
            # Validation is also in the real launch path; handler never returns raw exceptions.
            writer.close(); await writer.wait_closed()
        finally:
            await server.close()
        result = await send_request(dict(uid=self.owner,policy_version=1,totp='123456',confirmation=CONFIRMATION),socket_path=path)
        self.assertEqual(result['state'],'startup-unknown')

    async def test_real_child_pipe_framing_and_duplicate_start_no_second_process(self):
        connections(self.store,self.owner)
        version=self.pilot.status(self.owner)['version']; self.pilot.consent(self.owner,version)
        server=ActivationServer(self.store,'unused','unused',socket_path=Path(self.temp.name)/'activation.sock')
        original = asyncio.create_subprocess_exec
        observed = []
        async def child(*args,**kwargs):
            observed.append((args,kwargs['env']))
            return await original(sys.executable,'-c',
                'import json,sys,time; p=json.loads(sys.stdin.readline()); '
                'assert p["totp"]=="123456"; print(\'{"state":"started-paused"}\',flush=True); time.sleep(2)',**kwargs)
        payload=dict(uid=self.owner,policy_version=version,totp='123456',confirmation=CONFIRMATION)
        try:
            with patch('asyncio.create_subprocess_exec',side_effect=child):
                self.assertEqual(await server.launch(payload),{'state':'started-paused'})
                self.assertEqual(await server.launch(payload),{'state':'startup-refused'})
            self.assertEqual(len(observed),1)
            self.assertNotIn('123456',repr(observed))
        finally:
            await server.close()

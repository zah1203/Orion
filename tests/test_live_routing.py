from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet
from orion.live.accounting import IST
from orion.live.ledger import Ledger, Refused
from orion.live.pilot import Pilot
from orion.live.reconciliation import Reconciliation
from orion.live.routing import SignalRouter
from orion.portal.app import defaults
from orion.portal.store import Store
from test_live_pilot import LIMITS

CHANNEL = '-100123456'


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, Fernet.generate_key())
        settings = defaults() | dict(channels={CHANNEL:dict(profile='index', products=['NIFTY'])})
        self.owner = self.store.create_user('owner', 'synthetic password', settings)
        self.other = self.store.create_user('second', 'synthetic password', settings)
        self.store.bootstrap_owner('owner')
        self.pilot = Pilot(self.store)
        self.ledgers, self.routers = {}, {}
        for uid, ucc in ((self.owner,'TESTOWNER'), (self.other,'TESTSECOND')):
            self.store.save_credentials(uid, dict(kotak_ucc=ucc))
            limits = LIMITS if uid == self.owner else LIMITS | dict(max_lots=2, max_order_premium='4000')
            self.pilot.configure(self.owner, uid, limits)
            self.pilot.consent(uid, 1)
            ledger = Ledger(self.store.account_dir(uid)/'live.db', uid)
            Reconciliation(ledger, ucc)
            self.ledgers[uid] = ledger
            self.routers[uid] = SignalRouter(self.store, uid, ledger, ucc)
        self.now = datetime.now(timezone.utc)-timedelta(seconds=1)
        expiry = (self.now.astimezone(IST)+timedelta(days=2)).replace(hour=15,minute=30,second=0,microsecond=0)
        self.text = 'NIFTY '+expiry.strftime('%d %b %Y').upper()+' 24000 CE ACTION: BUY ENTRY PRICE RANGE: 100-102 SL: 90 TARGETS: 110/120/130'
        self.master = dict(as_of=self.now.astimezone(IST).date().isoformat(), contracts=[dict(product='NIFTY',strike='24000',option_type='CE',
            segment='nse_fo',expiry=expiry.date().isoformat(),expiry_at=expiry.isoformat(),symbol='NIFTYTESTCE',token='123',
            order_quantity_per_lot=10,premium_multiplier='1',tick_size='.05')])

    def tearDown(self):
        for ledger in self.ledgers.values(): ledger.close()
        self.temp.cleanup()

    def message(self, uid=None, **changes):
        args = dict(channel=CHANNEL,message_id=1,text=self.text,source_at=self.now,received_at=self.now,master=self.master)
        return self.routers[uid or self.owner].message(**(args | changes))

    def quote(self, uid=None, **changes):
        return self.routers[uid or self.owner].quote(CHANNEL+':1', **(dict(segment='nse_fo',token='123',bid='100',ask='101',
            quoted_at=datetime.now(timezone.utc),market_open=True) | changes))

    def test_two_accounts_use_separate_reviewed_limits_without_orders_or_paper_changes(self):
        for uid in (self.owner, self.other):
            paper = self.store.account_dir(uid)/'paper.db'
            paper.write_bytes(b'paper unchanged')
            self.message(uid)
        self.assertEqual(self.quote()['lots'], 1)
        self.assertEqual(self.quote(self.other)['lots'], 2)
        for uid, ledger in self.ledgers.items():
            self.assertEqual(self.quote(uid)['account'], uid)
            self.assertFalse(self.quote(uid)['order_submission_available'])
            self.assertFalse(ledger.db.execute('SELECT 1 FROM intents').fetchone())
            self.assertEqual((self.store.account_dir(uid)/'paper.db').read_bytes(), b'paper unchanged')
        self.assertNotIn(self.text, self.ledgers[self.owner].db.execute('SELECT body FROM live_signals').fetchone()[0])

    def test_duplicate_restart_edit_and_conflicting_replay_never_resurrect_candidate(self):
        self.message()
        self.assertTrue(self.message()['duplicate'])
        self.ledgers[self.owner].close()
        ledger = Ledger(self.store.account_dir(self.owner)/'live.db', self.owner)
        self.ledgers[self.owner] = ledger
        self.routers[self.owner] = SignalRouter(self.store,self.owner,ledger,'TESTOWNER')
        self.assertTrue(self.message()['duplicate'])
        with self.assertRaises(Refused): self.message(edited=True, text='')
        with self.assertRaises(Refused): self.quote()
        self.assertEqual(self.message()['state'], 'INVALIDATED')
        with self.assertRaises(Refused): self.message(text=self.text+' altered')
        self.assertEqual(self.message()['state'], 'CONFLICT')

    def test_revoke_policy_change_and_credentials_are_rechecked_before_quote(self):
        self.message()
        self.pilot.configure(self.owner,self.owner,LIMITS | dict(max_lots=2))
        with self.assertRaises(Refused): self.quote()
        self.pilot.consent(self.owner,2)
        with self.assertRaises(Refused): self.quote()  # Old candidate cannot inherit new review.
        self.message(self.other)
        self.store.save_credentials(self.other,dict(kotak_mpin='synthetic changed'))
        with self.assertRaises(Refused): self.quote(self.other)
        self.pilot.revoke(self.owner,self.owner)
        with self.assertRaises(Refused): self.message(message_id=2)

    def test_suspension_and_channel_change_block_staged_signals(self):
        self.message()
        settings = self.store.user(self.owner)['settings']
        self.store.save_settings(self.owner, settings | dict(channels={}))
        with self.assertRaises(Refused): self.quote()
        self.message(self.other)
        self.store.set_access(self.owner,self.other,'suspended')
        with self.assertRaises(Refused): self.quote(self.other)

    def test_backfill_future_reply_and_unselected_channel_are_rejected(self):
        for changes in (dict(source_at=self.now-timedelta(seconds=31)),dict(received_at=self.now+timedelta(seconds=5)),
                        dict(reply_to=2),dict(channel='-100999999'),dict(message_id=True)):
            with self.subTest(changes=changes), self.assertRaises(Refused): self.message(**changes)
        self.assertFalse(self.ledgers[self.owner].db.execute('SELECT 1 FROM live_signals').fetchone())

    def test_wrong_stale_crossed_off_tick_or_out_of_range_quote_is_rejected(self):
        self.message()
        for changes in (dict(token='other'),dict(segment='mcx_fo'),dict(quoted_at=self.now-timedelta(seconds=6)),
                        dict(quoted_at=self.now+timedelta(seconds=5)),dict(market_open=False),dict(ask='99'),
                        dict(ask='103'),dict(bid='102'),dict(bid='1'),dict(ask='101.01'),dict(ask='NaN')):
            with self.subTest(changes=changes), self.assertRaises((Refused, ValueError)): self.quote(**changes)

    def test_unverified_master_and_unsupported_signal_never_staged(self):
        for changes in (dict(master=self.master | dict(synthetic=True)),dict(master=self.master | dict(as_of='2000-01-01')),
                        dict(text=self.text+' BTST'),dict(text=self.text.replace('ACTION: BUY ENTRY PRICE RANGE: 100-102','BUY ABOVE 100'))):
            with self.subTest(changes=changes), self.assertRaises(Refused): self.message(**changes)
        self.assertFalse(self.ledgers[self.owner].db.execute('SELECT 1 FROM live_signals').fetchone())

    def test_account_or_broker_binding_mismatch_refused(self):
        with self.assertRaises(Refused): SignalRouter(self.store,self.owner,self.ledgers[self.other],'TESTSECOND')
        with self.assertRaises(Refused): SignalRouter(self.store,self.owner,self.ledgers[self.owner],'TESTSECOND')

class SourceTests(unittest.IsolatedAsyncioTestCase):
    setUp = RoutingTests.setUp
    tearDown = RoutingTests.tearDown

    async def test_default_client_disables_implicit_connection_retries(self):
        from unittest.mock import patch
        from orion.live.source import telegram_client
        with patch('telethon.TelegramClient') as client, patch('telethon.sessions.StringSession'):
            telegram_client(dict(telegram_session='synthetic',telegram_api_id=1,telegram_api_hash='synthetic'))
        self.assertFalse(client.call_args.kwargs['auto_reconnect'])
        self.assertEqual(client.call_args.kwargs['connection_retries'], 0)

    async def test_busy_paper_slot_refuses_before_client_creation(self):
        import asyncio
        import fcntl
        from unittest.mock import Mock
        from orion.live.source import serve_signals
        factory = Mock()
        with open(self.store.account_dir(self.owner)/'worker.lock', 'a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Refused):
                await serve_signals(self.store,self.owner,lambda:self.master,stop=asyncio.Event(),_client_factory=factory)
        factory.assert_not_called()

    async def test_receive_only_account_source_stages_then_invalidates_on_disconnect(self):
        import asyncio
        from types import SimpleNamespace
        from orion.live.source import serve_signals
        stop = asyncio.Event()
        calls = []
        source_text = self.text
        class Client:
            def add_event_handler(client, handler, event): client.handler = handler
            async def connect(client): calls.append('connect')
            async def is_user_authorized(client): return True
            async def run_until_disconnected(client):
                event = SimpleNamespace(chat_id=int(CHANNEL),id=11,raw_text=source_text,
                    message=SimpleNamespace(date=datetime.now(timezone.utc),edit_date=None,reply_to_msg_id=None))
                await client.handler(event)
                row = self.ledgers[self.owner].db.execute('SELECT state FROM live_signals WHERE source=?', (CHANNEL+':11',)).fetchone()
                self.assertEqual(row[0], 'PENDING')
                self.assertFalse(self.ledgers[self.other].db.execute('SELECT 1 FROM live_signals').fetchone())
                stop.set()
            async def disconnect(client): calls.append('disconnect')
        def factory(creds):
            self.assertEqual(creds['kotak_ucc'],'TESTOWNER')
            return Client()
        await serve_signals(self.store,self.owner,lambda:self.master,stop=stop,_client_factory=factory)
        self.assertEqual(calls, ['connect','disconnect'])
        self.assertEqual(self.ledgers[self.owner].db.execute('SELECT state FROM live_signals').fetchone()[0], 'INVALIDATED')
        self.assertFalse(self.ledgers[self.owner].db.execute('SELECT 1 FROM intents').fetchone())

    async def test_expired_telegram_session_disconnects_and_releases_slot(self):
        import asyncio
        import fcntl
        from orion.live.source import serve_signals
        calls = []
        class Client:
            def add_event_handler(client, handler, event): pass
            async def connect(client): calls.append('connect')
            async def is_user_authorized(client): return False
            async def disconnect(client): calls.append('disconnect')
        with self.assertRaises(Refused):
            await serve_signals(self.store,self.owner,lambda:self.master,stop=asyncio.Event(),_client_factory=lambda _:Client())
        self.assertEqual(calls, ['connect','disconnect'])
        with open(self.store.account_dir(self.owner)/'worker.lock','a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse(self.ledgers[self.owner].db.execute('SELECT 1 FROM intents').fetchone())

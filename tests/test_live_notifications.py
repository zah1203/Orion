from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from cryptography.fernet import Fernet

from orion.portal.store import Store
from orion.portal.attention import Attention
from orion.live.ledger import Ledger, Refused
from orion.live.notifications import LiveNotifications, dispatch_accounts

class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name,Fernet.generate_key())
        self.uid = self.store.create_user('notifyuser','password123456',{})
        self.other = self.store.create_user('otheruser','password123456',{})
        self.ledger = Ledger(self.store.account_dir(self.uid)/'live.db',self.uid)
        self.ledger.db.execute("INSERT INTO live_incidents VALUES('broker-command-unknown')")
        self.post = Mock(return_value={'data':{'status':'ok','id':'ticket-1'}})
        self.sender = LiveNotifications(self.store,self.ledger,_post=self.post)

    def tearDown(self):
        self.ledger.close(); self.temp.cleanup()

    def register(self,uid=None):
        Attention(self.store).register(uid or self.uid,'ExpoPushToken[synthetic]', 'synthetic-session')

    def test_no_device_or_other_account_device_never_claims_or_sends(self):
        self.assertEqual(self.sender.cycle(),'no-registered-device')
        self.register(self.other)
        self.assertEqual(self.sender.cycle(),'no-registered-device')
        self.assertEqual(self.ledger.db.execute('SELECT status FROM live_alerts').fetchone()[0],'PENDING')
        self.post.assert_not_called()

    def test_ticket_is_not_delivery_receipt_survives_restart(self):
        self.register()
        self.assertEqual(self.sender.cycle(now=1000),'receipt-pending')
        self.assertEqual(self.ledger.db.execute('SELECT status FROM live_alerts').fetchone()[0],'SENDING')
        self.assertNotIn(self.uid,str(self.post.call_args))
        self.sender = LiveNotifications(self.store,self.ledger,_post=self.post)
        self.post.return_value = {'data':{'ticket-1':{'status':'ok'}}}
        self.assertEqual(self.sender.cycle(now=1900),'provider-accepted')
        self.assertEqual(self.ledger.db.execute('SELECT status FROM live_alerts').fetchone()[0],'DELIVERED')
        self.assertTrue(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())
        self.assertEqual(self.sender.cycle(now=1901),'idle')
        self.assertEqual(self.post.call_count,2)

    def test_uncertain_send_never_retries_or_logs_provider_text(self):
        self.register(); self.post.side_effect = RuntimeError('private-token')
        self.assertEqual(self.sender.cycle(),'delivery-unknown')
        self.assertEqual(self.sender.cycle(),'idle')
        self.assertEqual(self.post.call_count,1)
        self.assertNotIn('private-token',str([tuple(r) for r in self.ledger.db.execute('SELECT * FROM live_alerts')]))

    def test_missing_receipt_rechecks_read_only_then_expires(self):
        self.register(); self.sender.cycle(now=1000)
        self.post.return_value={'data':{}}
        self.assertEqual(self.sender.cycle(now=1900),'receipt-pending')
        self.assertEqual(self.sender.cycle(now=4600),'delivery-unknown')
        self.assertEqual(self.post.call_count,2)

    def test_disable_before_send_and_mismatched_store_refused(self):
        self.register()
        Attention(self.store).disable(self.uid,'ExpoPushToken[synthetic]')
        self.assertEqual(self.sender.cycle(),'no-registered-device')
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Store(tmp,Fernet.generate_key())
            with self.assertRaises(Refused): LiveNotifications(wrong,self.ledger,_post=self.post)

    def test_unregistered_receipt_blocks_future_sends_until_registration(self):
        self.register(); self.sender.cycle(now=1000)
        self.post.return_value={'data':{'ticket-1':{'status':'error','details':{'error':'DeviceNotRegistered'}}}}
        self.assertEqual(self.sender.cycle(now=1900),'delivery-unknown')
        self.ledger.db.execute("INSERT INTO live_incidents VALUES('monitor-requires-review')")
        self.assertEqual(self.sender.cycle(now=1901),'no-registered-device')
        self.assertEqual(self.post.call_count,2)

    def test_expiry_and_device_reassignment_do_not_send_to_previous_owner(self):
        self.register()
        with self.store.db() as db: db.execute('UPDATE push_devices SET expires=1')
        self.assertEqual(self.sender.cycle(now=1000),'no-registered-device')
        self.register(); self.register(self.other)
        self.assertEqual(self.sender.cycle(),'no-registered-device')
        self.post.assert_not_called()

    def test_independent_dispatch_does_not_create_other_ledgers_or_touch_paper(self):
        self.register()
        paper = self.store.account_dir(self.uid)/'paper.db'
        paper.write_bytes(b'unchanged-paper')
        result = dispatch_accounts(self.store,_post=self.post)
        self.assertEqual(result,dict(checked=1,failed=0))
        self.assertEqual(paper.read_bytes(),b'unchanged-paper')
        self.assertFalse((self.store.account_dir(self.other)/'live.db').exists())
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0],0)

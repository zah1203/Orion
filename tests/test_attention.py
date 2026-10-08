import tempfile
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from orion.portal.app import create_app, defaults
from orion.portal.attention import Attention

PASSWORD='a long testing password'
TOKEN='ExpoPushToken[01234567890123456789]'

class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(self.tmp.name,Fernet.generate_key(),'http://127.0.0.1:8000')
        self.store=self.app.state.store
        self.uid=self.store.create_user('alice',PASSWORD,defaults())
        self.other=self.store.create_user('bob',PASSWORD,defaults())
        self.alerts=Attention(self.store)
        self.store.set_enabled(self.uid,True)
        self.store.health(self.uid,{'broker':'authentication_required'})
        self.store.heartbeat(self.uid)
        self.alerts.register(self.uid,TOKEN,'session')
    def tearDown(self):self.tmp.cleanup()
    def count(self):
        with self.store.db() as db:return db.execute('SELECT count(*) FROM push_outbox').fetchone()[0]
    def test_debounce_repeat_recovery(self):
        self.alerts.scan(1000);self.assertEqual(self.count(),0)
        self.alerts.scan(1030);self.assertEqual(self.count(),1)
        self.alerts.scan(1100);self.assertEqual(self.count(),1)
        self.alerts.scan(1930);self.assertEqual(self.count(),2)
        self.assertEqual(self.alerts.view(self.other)['incidents'],[])
        self.store.health(self.uid,{'broker':'connected'})
        self.alerts.scan(1931);self.assertEqual(self.count(),0)
        self.assertEqual(self.alerts.view(self.uid)['incidents'],[])
    def test_paused_open_positions_and_worker_stall(self):
        self.store.set_enabled(self.uid,False)
        self.alerts.scan(1000);self.assertEqual(self.alerts.view(self.uid)['incidents'],[])
        with patch.object(self.alerts,'has_open_positions',return_value=True),patch.object(self.store,'worker_alive',return_value=False):
            self.alerts.scan(1000);self.alerts.scan(1090)
            incident=self.alerts.view(self.uid)['incidents'][0]
            self.assertEqual(incident['kind'],'worker_offline')
            self.assertIn('cannot be reliably monitored',incident['message'])
            self.assertEqual(self.count(),1)
    def test_reconnect_does_not_reset_debounce(self):
        self.store.health(self.uid,{'broker':'connecting'})
        self.alerts.scan(1000)
        self.store.health(self.uid,{'broker':'reauthentication_required'})
        self.alerts.scan(1090)
        self.assertEqual(self.count(),1)
        self.assertEqual(self.alerts.view(self.uid)['incidents'][0]['kind'],'kotak_feed')
    def test_delivery_receipt_and_no_token_exposure(self):
        self.alerts.scan(1000);self.alerts.scan(1030)
        with patch.object(self.alerts,'post',return_value={'data':{'status':'ok','id':'ticket'}}) as post:
            self.alerts.dispatch(1030)
            self.assertEqual(post.call_args.args[1]['to'],TOKEN)
            self.assertNotIn('alice',post.call_args.args[1]['body'])
        with self.store.db() as db:self.assertEqual(db.execute('SELECT status FROM push_outbox').fetchone()[0],'receipt')
        with patch.object(self.alerts,'post',return_value={'data':{'ticket':{'status':'ok'}}}):self.alerts.dispatch(1930)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT status FROM push_outbox').fetchone()[0],'provider_accepted')
        self.assertNotIn(TOKEN,str(self.alerts.view(self.uid)))
    def test_bad_device_removal_and_retry(self):
        self.alerts.scan(1000);self.alerts.scan(1030)
        with patch.object(self.alerts,'post',side_effect=OSError('secret provider failure')):self.alerts.dispatch(1030)
        self.assertEqual(self.alerts.view(self.uid)['devices_with_errors'],1)
        with patch.object(self.alerts,'post',return_value={'data':{'status':'error','details':{'error':'DeviceNotRegistered'}}}):self.alerts.dispatch(1150)
        self.assertEqual(self.alerts.view(self.uid)['registered_devices'],0)
        self.assertEqual(self.count(),0)
    def test_owner_receives_alert_without_duplicate_for_own_account(self):
        self.store.bootstrap_owner('bob')
        self.alerts.register(self.other,'ExpoPushToken[abcdefghijklmnopqr]','other-session')
        self.alerts.scan(1000);self.alerts.scan(1030)
        self.assertEqual(self.count(),2)
        self.assertEqual(len(self.alerts.view(self.other,owner=True)['incidents']),1)
    def test_logout_and_shared_device_clear_queued_messages(self):
        self.alerts.scan(1000);self.alerts.scan(1030)
        self.alerts.register(self.other,TOKEN,'other-session')
        self.assertEqual(self.count(),0)
        self.assertEqual(self.alerts.view(self.uid)['registered_devices'],0)
        self.store.logout('other-session')
        self.assertEqual(self.alerts.view(self.other)['registered_devices'],0)
    def test_api_isolation_and_validation(self):
        with TestClient(self.app,base_url='http://127.0.0.1:8000') as client:
            token=client.post('/api/mobile/login',json={'username':'bob','password':PASSWORD}).json()['token']
            h={'Authorization':'Bearer '+token}
            self.alerts.scan(1000)
            self.assertEqual(client.get('/api/attention',headers=h).json()['incidents'],[])
            self.assertEqual(client.put('/api/push-device',headers=h,json={'token':'http://evil.test'}).status_code,422)
            self.assertEqual(client.put('/api/push-device',headers=h,json={'token':TOKEN,'user_id':self.uid}).status_code,422)
            self.assertEqual(client.put('/api/push-device',headers=h,json={'token':TOKEN}).status_code,200)
            client.post('/api/logout',headers=h,json={})
            self.assertEqual(self.alerts.view(self.other)['registered_devices'],0)
    def test_expired_native_logout_revokes_device(self):
        with TestClient(self.app,base_url='http://127.0.0.1:8000') as client:
            token=client.post('/api/mobile/login',json={'username':'bob','password':PASSWORD}).json()['token']
            h={'Authorization':'Bearer '+token}
            client.put('/api/push-device',headers=h,json={'token':TOKEN})
            with self.store.db() as db:db.execute('UPDATE sessions SET expires=0')
            self.assertEqual(client.post('/api/logout',headers=h,json={}).status_code,200)
            self.assertEqual(self.alerts.view(self.other)['registered_devices'],0)

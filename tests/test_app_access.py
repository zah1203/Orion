import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from orion.portal.app import create_app, defaults
from orion.portal.supervisor import Supervisor

ORIGIN = 'http://127.0.0.1:8000'
PASSWORD = 'a long test password'

class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name, Fernet.generate_key(), ORIGIN)
        self.store = self.app.state.store
        self.owner = self.store.create_user('owner', PASSWORD, defaults())
        self.store.bootstrap_owner('owner')
        self.user = self.store.create_user('other', PASSWORD, defaults())
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.headers = self.login('owner')
    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()
    def login(self, username):
        r = self.client.post('/api/mobile/login', json=dict(username=username,password=PASSWORD))
        self.assertEqual(r.status_code, 200)
        return {'Authorization': 'Bearer '+r.json()['token']}
    def test_pending_approval_and_isolation(self):
        r=self.client.post('/api/mobile/register',json=dict(username='newbie',password=PASSWORD))
        self.assertEqual(r.status_code,201)
        h=self.login('newbie')
        self.assertEqual(self.client.get('/api/me',headers=h).json()['access'],'pending')
        self.assertEqual(self.client.put('/api/credentials',headers=h,json={'kotak_consumer_key':'test'}).status_code,403)
        uid=self.store.by_username('newbie')
        self.assertEqual(self.client.put(f'/api/admin/users/{uid}/access',headers=h,json={'access':'approved'}).status_code,403)
        self.assertEqual(self.client.put(f'/api/admin/users/{uid}/access',headers=self.headers,json={'access':'approved'}).status_code,200)
        self.assertEqual(self.client.get('/api/me',headers=h).json()['access'],'approved')
        self.assertEqual(self.client.get(f'/api/admin/users/{self.owner}',headers=h).status_code,403)
        self.assertEqual(self.client.get('/api/admin/users',headers=h).status_code,403)
    def test_live_rejected_and_credentials_excluded(self):
        self.store.save_credentials(self.user,{'kotak_consumer_key':'never-expose-this'})
        h=self.login('other')
        self.assertEqual(self.client.put('/api/mode',headers=h,json={'mode':'live'}).status_code,409)
        r=self.client.get(f'/api/admin/users/{self.user}',headers=self.headers)
        self.assertEqual(r.status_code,200)
        self.assertNotIn('never-expose-this',r.text)
        self.assertNotIn('credentials',r.json())
        self.assertEqual(self.store.config(self.user)['mode'],'paper')
    def test_suspension_blocks_existing_session_and_worker_entries(self):
        h=self.login('other')
        self.store.set_enabled(self.user,True)
        self.client.put(f'/api/admin/users/{self.user}/access',headers=self.headers,json={'access':'suspended'})
        self.assertFalse(self.store.config(self.user)['new_entries_enabled'])
        self.assertEqual(self.client.post('/api/control',headers=h,json={'enabled':True}).status_code,403)
        self.assertEqual(self.client.get('/api/me',headers=h).status_code,200)
        self.assertEqual(self.client.post('/api/logout',headers=h,json={}).status_code,200)
        self.assertEqual(self.client.get('/api/me',headers=h).status_code,401)
    def test_owner_cannot_be_promoted_or_suspended_by_payload(self):
        self.assertEqual(self.client.post('/api/mobile/register',json={'username':'hacker','password':PASSWORD,'role':'owner'}).status_code,422)
        self.assertEqual(self.client.put(f'/api/admin/users/{self.owner}/access',headers=self.headers,json={'access':'suspended'}).status_code,409)
        with self.assertRaises(ValueError): self.store.bootstrap_owner('other')
    def test_browser_cannot_use_mobile_token_as_cookie(self):
        h=self.login('other')
        self.client.cookies.set('orion_session',h['Authorization'][7:])
        self.assertEqual(self.client.get('/api/me').status_code,401)
        self.assertEqual(self.client.post('/api/control',headers={**h,'Origin':'https://evil.test'},json={'enabled':True}).status_code,403)
    def test_multiple_channels_and_risk_do_not_replace_selection(self):
        h=self.login('other')
        channels=[dict(id='-100123456'+str(i),name='Channel '+str(i),profile='commodity',products=['GOLDM']) for i in range(3)]
        self.assertEqual(self.client.put('/api/channels',headers=h,json={'channels':channels}).status_code,200)
        body={k:defaults()[k] for k in ['risk_per_trade','daily_loss_limit','max_open_risk','max_lots','max_open_positions','max_entries_per_day']}
        self.assertEqual(self.client.put('/api/risk',headers=h,json=body).status_code,200)
        self.assertEqual(len(self.store.config(self.user)['channels']),3)
        self.assertEqual(self.client.put('/api/channels',headers=h,json={'channels':channels+channels}).status_code,422)
    def test_supervisor_keeps_open_positions_when_suspended(self):
        worker=Supervisor(self.store,'unused')
        cfg=defaults();cfg['channels']={'-100123456':{'products':['GOLDM']}}
        self.store.save_settings(self.user,cfg)
        self.store.save_credentials(self.user,{k:'test' for k in ['telegram_session','telegram_api_id','telegram_api_hash','kotak_consumer_key','kotak_mobile','kotak_ucc','kotak_mpin']})
        self.store.set_access(self.owner,self.user,'suspended')
        with patch.object(worker.accounts,'active_positions',return_value=True):
            self.assertTrue(worker.wanted(self.store.user(self.user)))
        with patch.object(worker.accounts,'active_positions',return_value=False):
            self.assertFalse(worker.wanted(self.store.user(self.user)))
    def test_owner_activity_pagination_and_empty_account(self):
        r=self.client.get(f'/api/admin/users/{self.user}/activity',headers=self.headers)
        self.assertEqual(r.json(),{'events':[],'next_cursor':None})
        engine=self.app.state.accounts.engine(self.user,{'synthetic':False,'contracts':[]})
        with engine.db:
            for i in range(105):
                engine.db.execute('INSERT INTO audit(at,event,body) VALUES(?,?,?)',('2026-10-08T00:00:00+00:00','TEST',json.dumps({'number':i})))
        engine.db.close()
        data=self.client.get(f'/api/admin/users/{self.user}/activity',headers=self.headers).json()
        self.assertEqual(len(data['events']),100)
        older=self.client.get(f'/api/admin/users/{self.user}/activity?before='+str(data['next_cursor']),headers=self.headers).json()
        self.assertEqual(len(older['events']),5)
        self.assertIsNone(older['next_cursor'])
        self.assertEqual(self.client.get(f'/api/admin/users/{self.user}/activity',headers=self.login('other')).status_code,403)

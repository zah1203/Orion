from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from orion.portal.app import create_app, defaults
from orion.live.pilot import Pilot
from orion.live.ledger import Refused

LIMITS = dict(capital='30000', max_order_premium='2000', max_open_premium='5000', daily_loss='1000',
              fee_reserve='10', max_trade_loss='500', max_open_risk='800', max_lots=1, max_entries=3, stop_gap_ticks=1, target_timeout_seconds=10)
ORIGIN = 'http://127.0.0.1:8000'
PASSWORD = 'safe synthetic password'


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, Fernet.generate_key(), ORIGIN)
        self.store = self.app.state.store
        self.owner = self.store.create_user('owner', PASSWORD, defaults())
        self.user = self.store.create_user('newuser', PASSWORD, defaults())
        self.other = self.store.create_user('other', PASSWORD, defaults())
        self.store.bootstrap_owner('owner')
        for uid, ucc in ((self.owner,'TESTOWNER'), (self.user,'TESTNEW'), (self.other,'TESTOTHER')):
            self.store.save_credentials(uid, dict(kotak_ucc=ucc, kotak_consumer_key='fake-key',
                                                 kotak_mobile='fake-mobile', kotak_mpin='fake-pin'))
        self.pilot = Pilot(self.store)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.login('owner')

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login(self, name):
        result = self.client.post('/api/login', json=dict(username=name,password=PASSWORD), headers={'Origin':ORIGIN})
        self.headers = {'Origin':ORIGIN, 'X-CSRF-Token':result.json()['csrf']}

    def test_two_account_limit_and_distinct_identity(self):
        self.pilot.configure(self.owner, self.owner, LIMITS)
        self.pilot.configure(self.owner, self.user, LIMITS | dict(daily_loss='500'))
        with self.assertRaises(Refused):
            self.pilot.configure(self.owner, self.other, LIMITS)
        self.assertEqual(self.pilot.status(self.user)['limits']['daily_loss'], '500')
        self.assertEqual(self.pilot.status(self.owner)['limits']['daily_loss'], '1000')
        self.pilot.revoke(self.owner, self.user)
        self.store.save_credentials(self.other, {'kotak_ucc':'TESTOWNER'})
        with self.assertRaises(Refused):
            self.pilot.configure(self.owner, self.other, LIMITS)

    def test_review_version_and_credential_change_invalidate_consent(self):
        self.pilot.configure(self.owner, self.user, LIMITS)
        self.pilot.consent(self.user, 1)
        self.assertTrue(self.pilot.status(self.user)['reviewed'])
        self.pilot.configure(self.owner, self.user, LIMITS | dict(max_lots=2))
        with self.assertRaises(Refused):
            self.pilot.consent(self.user, 1)
        self.pilot.consent(self.user, 2)
        self.store.save_credentials(self.user, {'kotak_consumer_key':'changed-fake-key'})
        self.assertFalse(self.pilot.status(self.user)['reviewed'])

    def test_api_owner_csrf_and_review_never_enable_trading(self):
        path = '/api/admin/live/pilot/' + self.user
        self.assertEqual(self.client.put(path, json={'limits':LIMITS}, headers={'Origin':ORIGIN}).status_code,403)
        self.assertEqual(self.client.put(path, json={'limits':LIMITS}, headers=self.headers).status_code,200)
        self.login('newuser')
        self.assertEqual(self.client.put(path, json={'limits':LIMITS}, headers=self.headers).status_code,403)
        response = self.client.post('/api/live/pilot/review', json={'version':1, 'confirmation':'REVIEW PILOT LIMITS'}, headers=self.headers)
        self.assertTrue(response.json()['reviewed'])
        self.assertFalse(response.json()['live_available'])
        self.assertEqual(self.client.put('/api/mode', json={'mode':'live'}, headers=self.headers).status_code,409)
        self.assertFalse(list(Path(self.temp.name).rglob('live.db')))

    def test_reviewed_new_user_can_probe_only_own_credentials(self):
        self.login('newuser')
        body = {'totp':'123456', 'confirmation':'READ ONLY CHECK'}
        with patch('orion.live.probe.run_probe', new_callable=AsyncMock, return_value={'live_available':False}) as probe:
            self.assertEqual(self.client.post('/api/live/probe', json=body, headers=self.headers).status_code,403)
            self.pilot.configure(self.owner, self.user, LIMITS)
            self.pilot.consent(self.user, 1)
            self.assertEqual(self.client.post('/api/live/probe', json=body, headers=self.headers).status_code,200)
            self.assertEqual(probe.call_args.args[0]['kotak_ucc'], 'TESTNEW')
        self.pilot.revoke(self.owner, self.user)
        self.assertFalse(self.pilot.status(self.user)['reviewed'])

    def test_suspension_revokes_pilot_and_keeps_paper_settings(self):
        before = self.store.user(self.user)['settings']
        self.pilot.configure(self.owner, self.user, LIMITS)
        self.pilot.consent(self.user, 1)
        self.store.set_access(self.owner, self.user, 'suspended')
        self.assertFalse(self.pilot.status(self.user)['enrolled'])
        self.assertEqual(self.store.user(self.user)['settings'], before)

    def test_five_preferences_persist_without_enrollment_or_policy_changes(self):
        values = dict(capital='200000', max_trade_loss='10000', daily_loss='15000', max_lots=5, max_entries=15)
        before = self.store.user(self.owner)['settings']
        path = '/api/admin/live/risk-preferences/' + self.owner
        result = self.client.put(path, json={'limits': values}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()['applied'])
        self.assertEqual(self.client.get(path).json()['preferences'], values)
        self.assertFalse(self.pilot.status(self.owner)['enrolled'])
        self.assertEqual(self.store.user(self.owner)['settings'], before)
        self.assertFalse(list(Path(self.temp.name).rglob('live.db')))
        self.pilot.configure(self.owner, self.owner, LIMITS)
        self.pilot.consent(self.owner, 1)
        policy = self.pilot.status(self.owner)
        self.client.put(path, json={'limits': values | dict(max_lots=4)}, headers=self.headers)
        self.assertEqual(self.pilot.status(self.owner), policy)
        self.assertEqual(Pilot(self.store).preferences(self.owner, self.owner)['preferences']['max_lots'], 4)

    def test_preferences_require_owner_csrf_and_valid_five_values(self):
        values = dict(capital='200000', max_trade_loss='10000', daily_loss='15000', max_lots=5, max_entries=15)
        path = '/api/admin/live/risk-preferences/' + self.user
        self.assertEqual(self.client.put(path, json={'limits': values}).status_code, 403)
        for invalid in (values | dict(max_lots=True), values | dict(max_lots=1.5),
                        values | dict(daily_loss='500'), values | dict(capital='NaN'),
                        values | dict(fee_reserve='100'), {'capital':'200000'}):
            self.assertEqual(self.client.put(path, json={'limits':invalid}, headers=self.headers).status_code, 409)
        self.assertIsNone(self.client.get(path).json()['preferences'])
        self.login('newuser')
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.put(path, json={'limits':values}, headers=self.headers).status_code, 403)

    def test_simple_policy_derives_caps_and_invalidates_old_review(self):
        values = dict(capital='200000', max_trade_loss='10000', daily_loss='15000',
                      max_lots=5, max_entries=15, fee_reserve='10',
                      stop_gap_ticks=1, target_timeout_seconds=10)
        path = '/api/admin/live/simple-policy/' + self.user
        before = self.store.user(self.user)['settings']
        self.assertEqual(self.client.put(path, json={'limits':values}).status_code, 403)
        result = self.client.put(path, json={'limits':values}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        limits = result.json()['limits']
        self.assertEqual(limits['max_order_premium'], '200000')
        self.assertEqual(limits['max_open_premium'], '200000')
        self.assertEqual(limits['max_open_risk'], '15000')
        self.assertFalse(result.json()['order_submission_available'])
        self.pilot.consent(self.user, 1)
        self.client.put(path, json={'limits':values | dict(max_lots=4)}, headers=self.headers)
        self.assertFalse(self.pilot.status(self.user)['reviewed'])
        self.assertEqual(self.pilot.status(self.user)['version'], 2)
        self.assertEqual(self.store.user(self.user)['settings'], before)
        self.assertFalse(list(Path(self.temp.name).rglob('live.db')))
        prefs = self.pilot.preferences(self.owner, self.user)
        self.assertEqual(prefs['execution'], {k:values[k] for k in ('fee_reserve', 'stop_gap_ticks', 'target_timeout_seconds')})
        self.assertEqual(self.pilot.preferences(self.owner, self.owner)['execution'], {})
        for invalid in ({k:v for k,v in values.items() if k != 'fee_reserve'},
                        values | dict(max_trade_loss='16000'), values | dict(stop_gap_ticks=True),
                        values | dict(max_open_premium='1')):
            self.assertEqual(self.client.put(path, json={'limits':invalid}, headers=self.headers).status_code, 409)
        self.assertEqual(self.pilot.status(self.user)['version'], 2)
        self.login('newuser')
        self.assertEqual(self.client.put(path, json={'limits':values}, headers=self.headers).status_code, 403)

import unittest
from unittest.mock import patch
import test_live_pilot as fixtures
from orion.live.authorization import CONFIRMATION

class EntryApiTests(unittest.TestCase):
    setUp = fixtures.PilotTests.setUp
    tearDown = fixtures.PilotTests.tearDown
    login = fixtures.PilotTests.login

    def test_closed_gate_and_csrf_never_create_ledger(self):
        body = dict(policy_version=1, confirmation=CONFIRMATION)
        self.assertEqual(self.client.post('/api/live/entries/authorize', json=body).status_code, 403)
        self.assertEqual(self.client.post('/api/live/entries/authorize', json=body, headers=self.headers).status_code, 409)
        self.assertFalse((self.store.account_dir(self.owner)/'live.db').exists())
        self.assertFalse(self.client.get('/api/live/entries').json()['release_available'])

    def test_authorization_always_targets_authenticated_account(self):
        self.login('newuser')
        body = dict(policy_version=1, confirmation=CONFIRMATION)
        with patch('orion.live.authorization.authorize', return_value={'authorized':True}) as authorize:
            self.assertEqual(self.client.post('/api/live/entries/authorize', json=body, headers=self.headers).status_code, 200)
            self.assertEqual(authorize.call_args.args[1], self.user)
            self.assertEqual(self.client.post('/api/live/entries/authorize', json=body|{'uid':self.owner}, headers=self.headers).status_code, 422)

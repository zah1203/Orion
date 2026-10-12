import base64
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_live_pilot as fixtures
from test_live_execution import report
from orion.live.cash import CONFIRMATION
from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.ledger import Ledger
from orion.live.monitor import ProtectionMonitor


class FinanceTests(unittest.TestCase):
    setUp = fixtures.PilotTests.setUp
    tearDown = fixtures.PilotTests.tearDown
    login = fixtures.PilotTests.login

    def make_ledger(self):
        self.pilot.configure(self.owner,self.owner,fixtures.LIMITS)
        self.pilot.consent(self.owner,1)
        ledger = Ledger(self.store.account_dir(self.owner)/'live.db',self.owner)
        h = ExecutionHarness(ledger,'TESTOWNER',fixtures.LIMITS,SimulatedBroker(),enrolled=True,reviewed=True)
        ProtectionMonitor(ledger,SimpleNamespace(ucc='TESTOWNER'))
        now = datetime.now(timezone.utc)
        h.check_book(report(),report(),now)
        ledger.db.execute('CREATE TABLE live_worker_health(id INTEGER,checked_at TEXT,stopped INTEGER)')
        ledger.db.execute('INSERT INTO live_worker_health VALUES(1,?,0)',(now.isoformat(),))
        ledger.close()

    def payload(self):
        return dict(opening_cash='1000',confirmation=CONFIRMATION)

    def test_no_worker_no_ledger_no_orders_and_no_paper_change(self):
        paper=self.store.account_dir(self.owner)/'paper.db'
        paper.write_bytes(b'paper untouched')
        self.assertIsNone(self.client.get('/api/live/finance').json()['latest_cash_review'])
        self.assertEqual(self.client.post('/api/live/finance/cash',json=self.payload(),headers=self.headers).status_code,409)
        self.assertFalse(list(Path(self.temp.name).rglob('live.db')))
        self.assertEqual(paper.read_bytes(),b'paper untouched')
        self.assertEqual(self.client.put('/api/mode',json={'mode':'live'},headers=self.headers).status_code,409)

    def test_cash_review_authenticated_bound_idempotent_and_never_resumes(self):
        self.make_ledger()
        path='/api/live/finance/cash'
        self.assertEqual(self.client.post(path,json=self.payload()).status_code,403)
        response=self.client.post(path,json=self.payload(),headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(response.json()['order_submission_available'])
        self.assertTrue(self.client.post(path,json=self.payload(),headers=self.headers).json()['duplicate'])
        self.assertEqual(self.client.post(path,json=self.payload()|dict(opening_cash='1200'),headers=self.headers).status_code,409)
        self.assertEqual(self.client.get('/api/live/finance').json()['latest_cash_review']['opening_cash'],'1000')
        ledger=Ledger(self.store.account_dir(self.owner)/'live.db',self.owner)
        self.assertEqual(ledger.db.execute('SELECT paused FROM control').fetchone()[0],1)
        self.assertFalse(ledger.db.execute('SELECT 1 FROM broker_commands').fetchone())
        ledger.close()
        self.login('newuser')
        self.assertIsNone(self.client.get('/api/live/finance').json()['latest_cash_review'])
        self.assertEqual(self.client.post(path,json=self.payload(),headers=self.headers).status_code,409)
        self.assertEqual(self.client.post(path,json=self.payload()|dict(uid=self.owner),headers=self.headers).status_code,422)

    def test_credential_change_invalidates_review_and_recovery_stays_quarantined(self):
        self.make_ledger()
        self.store.save_credentials(self.owner,dict(kotak_mpin='changed'))
        self.assertEqual(self.client.post('/api/live/finance/cash',json=self.payload(),headers=self.headers).status_code,409)
        (self.store.account_dir(self.owner)/'RECOVERY_ONLY').touch()
        self.assertEqual(self.client.get('/api/live/finance').status_code,409)

    def test_historical_fee_review_needs_no_worker_or_renewed_trading_consent(self):
        self.make_ledger()
        ledger=Ledger(self.store.account_dir(self.owner)/'live.db',self.owner)
        ledger.db.execute('UPDATE live_worker_health SET stopped=1')
        ledger.close()
        self.pilot.revoke(self.owner,self.owner)
        with patch('orion.live.finance.review_statement',return_value={'order_submission_available':False}) as review:
            body=dict(proof_text='synthetic original note',statement={},confirmation='I REVIEWED THE BROKER STATEMENT')
            response=self.client.post('/api/live/finance/statement',json=body,headers=self.headers)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(review.call_args.kwargs['actor'],self.owner)
            self.assertEqual(review.call_args.args[0].ucc,'TESTOWNER')

    def test_statement_private_body_requires_auth_and_never_echoes_proof(self):
        body=dict(proof_text='private statement '+('x'*40000),statement={},confirmation='I REVIEWED THE BROKER STATEMENT')
        path='/api/live/finance/statement'
        self.assertEqual(self.client.post(path,json=body,headers={'Origin':fixtures.ORIGIN}).status_code,403)
        # The larger authenticated statement limit does not change other routes.
        self.assertEqual(self.client.post('/api/live/finance/cash',json=body,headers=self.headers).status_code,413)
        with patch('orion.live.finance.review_fees', return_value={'order_submission_available':False}) as review:
            response=self.client.post(path,json=body,headers=self.headers)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(review.call_args.kwargs['proof'],body['proof_text'].encode())
            self.assertNotIn('private statement',response.text)
        response=self.client.post(path,json=body|dict(proof_base64=base64.b64encode(b'original').decode()),headers=self.headers)
        self.assertEqual(response.status_code,409)
        self.assertNotIn('private statement',response.text)
        self.client.post(path,json=body,headers=self.headers)
        self.assertEqual(self.client.post(path,json=body,headers=self.headers).status_code,429)

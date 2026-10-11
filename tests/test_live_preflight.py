from datetime import datetime, timedelta, timezone
import unittest

from orion.live.funding import FIELDS
from orion.live.ledger import Refused
from orion.live.preflight import funding_preflight, MARGIN_FIELDS
import test_live_entry_risk as risk_tests
from test_live_routing import CHANNEL


class Session:
    ucc = 'TESTOWNER'
    def __init__(self, callback=lambda:None, changes=None):
        self.callback, self.changes, self.calls = callback, changes or {}, []
    def request(self, operation, **kwargs):
        self.calls.append((operation,kwargs))
        self.callback()
        at = datetime.now(timezone.utc).isoformat()
        request = kwargs['request']
        return dict(ucc=self.ucc,token=kwargs['token'],symbol=request['symbol'],
            quantity=request['quantity'],price=request['price'],started_at=at,completed_at=at,
            rms_before_at=at,rms_after_at=at,reported_limits=dict.fromkeys(FIELDS,'0'),
            reported_margin=dict.fromkeys(MARGIN_FIELDS,'0'),broker_reports_shortfall=False,
            available_cash_verified=False,fees_verified=False,order_submission_available=False,
            private='synthetic secret') | self.changes


class PreflightTests(unittest.TestCase):
    setUp = risk_tests.EntryRiskTests.setUp
    tearDown = risk_tests.EntryRiskTests.tearDown
    prepare = risk_tests.EntryRiskTests.prepare
    message = risk_tests.EntryRiskTests.message
    reserve = risk_tests.EntryRiskTests.reserve

    def check(self,h,session):
        return funding_preflight(self.routers[self.owner],session,CHANNEL+':1',
            accounting=h.accounting,marks={},segment='nse_fo',token='123',bid='100',ask='101',
            quoted_at=datetime.now(timezone.utc),market_open=True)

    def test_integrated_read_is_one_shot_without_mutation_or_authority(self):
        h=self.prepare(); session=Session(); before=list(h.ledger.db.iterdump())
        result=self.check(h,session)
        self.assertEqual([c[0] for c in session.calls],['funding'])
        self.assertEqual(session.calls[0][1]['request']['quantity'],10)
        self.assertEqual(result['candidate']['risk']['candidate_premium'],'1020')
        self.assertIn('entries-paused',result['blockers'])
        self.assertIn('atomic-entry-authorization',result['blockers'])
        self.assertFalse(result['available_cash_verified'])
        self.assertFalse(result['order_submission_available'])
        self.assertNotIn('secret',str(result))
        self.assertEqual(list(h.ledger.db.iterdump()),before)
        self.assertEqual(h.broker.orders,[])

    def test_revocation_during_read_invalidates_result(self):
        h=self.prepare(); session=Session(lambda:self.pilot.revoke(self.owner,self.owner))
        with self.assertRaisesRegex(Refused,'reviewed account policy'): self.check(h,session)
        self.assertEqual(len(session.calls),1)

    def test_channel_change_during_read_invalidates_result(self):
        h=self.prepare()
        def change():
            settings=self.store.user(self.owner)['settings']
            self.store.save_settings(self.owner,settings | dict(channels={}))
        with self.assertRaisesRegex(Refused,'channel'): self.check(h,Session(change))

    def test_paused_change_during_read_invalidates_result(self):
        h=self.prepare()
        with self.assertRaisesRegex(Refused,'changed during'):
            self.check(h,Session(h.ledger.development_resume))

    def test_new_reservation_during_read_invalidates_even_same_quantity(self):
        h=self.prepare(); h.ledger.development_resume()
        with self.assertRaisesRegex(Refused,'changed during'):
            self.check(h,Session(lambda:self.reserve(h)))

    def test_wrong_session_or_outer_transaction_rejected_before_io(self):
        h=self.prepare(); session=Session(); session.ucc='TESTSECOND'
        with self.assertRaisesRegex(Refused,'account mismatch'): self.check(h,session)
        session.ucc='TESTOWNER'
        with h.ledger.transaction(),self.assertRaisesRegex(Refused,'Committed'):
            self.check(h,session)
        self.assertEqual(session.calls,[])

    def test_changed_identity_size_price_stale_or_elevated_response_rejected(self):
        h=self.prepare()
        old=(datetime.now(timezone.utc)-timedelta(seconds=6)).isoformat()
        for changes in (dict(ucc='OTHER'),dict(quantity=True),dict(quantity=20),dict(price='102'),
                        dict(token='999'),dict(symbol='OTHER'),dict(rms_before_at=old),
                        dict(available_cash_verified=True),dict(fees_verified=True),
                        dict(reported_limits={}),dict(reported_margin={}),
                        dict(broker_reports_shortfall=True)):
            with self.subTest(changes=changes),self.assertRaises(Refused):
                self.check(h,Session(changes=changes))

    def test_broker_shortfall_is_blocker_never_permission(self):
        h=self.prepare()
        result=self.check(h,Session(changes=dict(broker_reports_shortfall=True,
            reported_margin=dict.fromkeys(MARGIN_FIELDS,'0') | dict(insufFund='100'))))
        self.assertIn('broker-reported-funding-shortfall',result['blockers'])
        self.assertFalse(result['order_submission_available'])

    def test_failure_never_retries_or_exposes_private_errors(self):
        h=self.prepare()
        def fail(): raise RuntimeError('synthetic secret')
        session=Session(fail)
        with self.assertRaises(Refused) as error: self.check(h,session)
        self.assertNotIn('secret',str(error.exception))
        self.assertEqual(len(session.calls),1)

    def test_reconsented_new_policy_and_edited_source_invalidate_result(self):
        from test_live_routing import LIMITS
        h=self.prepare()
        def change_policy():
            self.pilot.configure(self.owner,self.owner,LIMITS | dict(max_lots=2))
            self.pilot.consent(self.owner,2)
        with self.assertRaisesRegex(Refused,'policy or channel changed'):
            self.check(h,Session(change_policy))
        # Fresh source under the new policy; invalidate it during the read.
        h.ledger.db.execute('DELETE FROM live_signals')
        self.message()
        def edit():
            try: self.message(edited=True)
            except Refused: pass
        with self.assertRaisesRegex(Refused,'Pending source'):
            self.check(h,Session(edit))

    def test_funding_cannot_outlive_final_policy_recheck(self):
        from unittest.mock import patch
        h=self.prepare()
        with patch('orion.live.preflight.time.monotonic',side_effect=[0,1,6]):
            with self.assertRaisesRegex(Refused,'expired during policy'):
                self.check(h,Session())

    def test_private_process_and_kotak_adapter_feed_the_same_preflight(self):
        from orion.live.session import ProcessSession
        from orion.live.startup import ProtectiveSession
        from test_live_kotak import CREDS
        h=self.prepare()
        with ProcessSession(CREDS | dict(kotak_ucc='TESTOWNER'),'123456',
                            _factory=OwnerFundingSDK) as session:
            result=self.check(h,ProtectiveSession(session))
        self.assertEqual(result['reported_limits']['Net'],'19.409999999999997')
        self.assertFalse(result['available_cash_verified'])
        self.assertFalse(result['order_submission_available'])


from test_live_funding import FundingSDK


class OwnerFundingSDK(FundingSDK):
    """Synthetic owner authentication; no network and no real account data."""
    def __init__(self,creds):
        super().__init__()
        self.configuration.ucc=creds['kotak_ucc']
    def totp_login(self,**kwargs):
        result=super().totp_login(**kwargs)
        result['data']['ucc']=self.configuration.ucc
        return result

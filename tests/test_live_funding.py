from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import unittest

from orion.live.funding import FIELDS
from orion.live.kotak import KotakSession, TransportFailure
from orion.live.ledger import Refused
from orion.live.session import ProcessSession
from test_live_kotak import CREDS
from test_live_margin import MarginSDK, REQUEST


def limits(**changes):
    return dict.fromkeys(FIELDS,'0') | dict(stat='Ok',stCode=200,EntityId='',
        TimeStamp=str(int(datetime.now(timezone.utc).timestamp()*1000)),
        CollateralValue='38.19',MarginUsed='18.78',Net='19.409999999999997',
        UnrealizedMtomPrsnt='-0.24',private='synthetic secret') | changes


class FundingSDK(MarginSDK):
    def limits(self):
        self.calls.append('limits')
        return limits()


class FundingTests(unittest.TestCase):
    def test_candidate_check_bracketed_without_cash_or_fee_authority(self):
        sdk=FundingSDK()
        value=KotakSession(sdk,CREDS,'123456').funding(REQUEST,'123')
        self.assertEqual(sdk.calls[0],'limits')
        self.assertEqual(sdk.calls[-1],'limits')
        self.assertEqual(len(sdk.calls),3)
        self.assertEqual(sdk.calls[1]['quantity'],'10')
        self.assertEqual(value['reported_limits']['Net'],'19.409999999999997')
        self.assertEqual(value['reported_limits']['UnrealizedMtomPrsnt'],'-0.24')
        self.assertFalse(value['available_cash_verified'])
        self.assertFalse(value['fees_verified'])
        self.assertFalse(value['order_submission_available'])
        self.assertNotIn('secret',str(value))
        self.assertNotIn('EntityId',str(value))

    def test_invalid_candidate_does_not_read_or_place(self):
        sdk=FundingSDK(); session=KotakSession(sdk,CREDS,'123456')
        for request,token in ((replace(REQUEST,kind='EXIT'),'123'),(REQUEST,'0'),
                              (replace(REQUEST,quantity=1),'123')):
            with self.assertRaises(Refused): session.funding(request,token)
        self.assertEqual(sdk.calls,[])

    def test_moving_funds_or_backward_clock_rejected_without_retry(self):
        base=limits()
        for second in (base | dict(Net='18'),base | dict(TimeStamp=str(int(base['TimeStamp'])-1))):
            sdk=FundingSDK(); responses=iter([base,second]); calls=[]
            def read(): calls.append('read'); return next(responses)
            sdk.limits=read
            session=KotakSession(sdk,CREDS,'123456')
            with self.assertRaises(TransportFailure): session.funding(REQUEST,'123')
            self.assertEqual(len(calls),2)
            self.assertTrue(session.closed)
            with self.assertRaises(TransportFailure): session.funding(REQUEST,'123')
            self.assertEqual(len(calls),2)

    def test_untrusted_identity_stale_missing_and_invalid_numbers_rejected(self):
        old=str(int((datetime.now(timezone.utc)-timedelta(seconds=6)).timestamp()*1000))
        future=str(int((datetime.now(timezone.utc)+timedelta(seconds=6)).timestamp()*1000))
        for changes in (dict(EntityId='OTHER'),dict(TimeStamp=old),dict(TimeStamp=future),
                        dict(TimeStamp=None),dict(Net='NaN'),dict(Net=True),
                        dict(CollateralValue=None),dict(CollateralValue='-1'),
                        dict(BrokeragePrsnt='1e-99'),dict(errMsg='synthetic secret')):
            sdk=FundingSDK(); sdk.limits=lambda:limits(**changes)
            session=KotakSession(sdk,CREDS,'123456')
            with self.subTest(changes=changes),self.assertRaises(TransportFailure) as error:
                session.funding(REQUEST,'123')
            self.assertNotIn('secret',str(error.exception))
            self.assertTrue(session.closed)
            self.assertEqual(sdk.calls,[])

    def test_reported_shortfall_is_not_discarded_or_promoted_to_permission(self):
        sdk=FundingSDK(); original=sdk.margin_required
        def margin(**kwargs):
            result=original(**kwargs); result['data']['insufFund']='100'; return result
        sdk.margin_required=margin
        result=KotakSession(sdk,CREDS,'123456').funding(REQUEST,'123')
        self.assertTrue(result['broker_reports_shortfall'])
        self.assertFalse(result['order_submission_available'])

    def test_bounded_private_process_returns_only_sanitized_funding_evidence(self):
        with ProcessSession(CREDS,'123456',_factory=FundingSDK) as session:
            result=session.request('funding',request=asdict(REQUEST),token='123')
        self.assertEqual(result['ucc'],'TESTUCC')
        self.assertEqual(result['quantity'],10)
        self.assertFalse(result['fees_verified'])
        self.assertNotIn('secret',str(result))

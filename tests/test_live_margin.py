from dataclasses import asdict, replace
import unittest

from orion.live.kotak import KotakSession, OrderRequest, TransportFailure
from orion.live.ledger import Refused
from orion.live.session import ProcessSession
from test_live_kotak import FakeSDK, CREDS

REQUEST = OrderRequest(tag='estimate',symbol='TESTCE',quantity=10,lot_size=10,tick='.05',price='100',kind='ENTRY')

class MarginSDK(FakeSDK):
    def margin_required(self, **kwargs):
        self.calls.append(kwargs)
        return dict(data=dict(stat='Ok',stCode=200,rmsVldtd='OK',avlCash='10000',totMrgnUsd='2000',
            mrgnUsd='1000',ordMrgn='1000',reqdMrgn='0',avlMrgn='0',insufFund='0',private='must not escape'))

class MarginTests(unittest.TestCase):
    def test_exact_candidate_read_does_not_place_or_certify_cash(self):
        sdk = MarginSDK()
        result = KotakSession(sdk,CREDS,'123456').margin(REQUEST,'123')
        self.assertEqual(sdk.calls,[dict(exchange_segment='nse_fo',product='NRML',order_type='L',
            transaction_type='B',instrument_token='123',price='100',quantity='10')])
        self.assertFalse(result['available_cash_verified'])
        self.assertFalse(result['order_submission_available'])
        self.assertNotIn('private',str(result))

    def test_invalid_candidate_refused_before_api(self):
        sdk = MarginSDK(); session = KotakSession(sdk,CREDS,'123456')
        for request, token in ((replace(REQUEST,quantity=1),'123'),(replace(REQUEST,kind='EXIT'),'123'),
                               (REQUEST,'0'),(REQUEST,'bad/url')):
            with self.assertRaises(Refused): session.margin(request,token)
        self.assertEqual(sdk.calls,[])

    def test_missing_nonfinite_or_error_response_rejected_without_secret_output(self):
        for change in (dict(avlCash='NaN'),dict(avlCash=None),dict(stCode=True),dict(rmsVldtd='NO'),
                       dict(errMsg='secret'),dict(ordMrgn='-1')):
            sdk = MarginSDK(); base = sdk.margin_required()['data']; sdk.calls.clear()
            sdk.margin_required = lambda **kwargs: dict(data=base | change)
            session = KotakSession(sdk,CREDS,'123456')
            with self.assertRaises(TransportFailure) as error: session.margin(REQUEST,'123')
            self.assertNotIn('secret',str(error.exception))
            self.assertTrue(session.closed)

    def test_private_process_margin_check_preserves_no_authority(self):
        with ProcessSession(CREDS,'123456',_factory=MarginSDK) as session:
            result = session.request('margin',request=asdict(REQUEST),token='123')
            self.assertEqual(result['ucc'],'TESTUCC')
            self.assertEqual(result['quantity'],10)
            self.assertFalse(result['fees_verified'])
            self.assertNotIn('private',str(result))

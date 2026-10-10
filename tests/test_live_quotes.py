from datetime import datetime, timezone
import copy
import unittest

from orion.live.kotak import KotakSession, TransportFailure
from orion.live.ledger import Refused
from orion.live.session import ProcessSession
from test_live_kotak import FakeSDK, CREDS

REQUEST = [dict(token='123',symbol='NIFTYTESTCE')]


def quote():
    return dict(exchange_token='123', display_symbol='NIFTYTESTCE', exchange='nse_fo',
        lstup_time=str(int(datetime.now(timezone.utc).timestamp())),
        depth=dict(buy=[dict(price='100',quantity='10')],sell=[dict(price='101',quantity='20')]))


class QuoteSDK(FakeSDK):
    def quotes(self, **kwargs):
        self.calls.append(kwargs)
        return [quote()]


class QuoteTests(unittest.TestCase):
    def test_adapter_maps_exact_tokens_and_never_certifies_open_market(self):
        sdk = QuoteSDK()
        value = KotakSession(sdk, CREDS, '123456').quotes(REQUEST)
        self.assertEqual(sdk.calls, [dict(instrument_tokens=[dict(exchange_segment='nse_fo',instrument_token='123')],quote_type='all')])
        self.assertEqual(value['quotes'][0]['bid'],'100')
        self.assertFalse(value['market_open_verified'])
        self.assertFalse(value['order_submission_available'])

    def test_malformed_requests_do_not_call_broker(self):
        sdk = QuoteSDK()
        session = KotakSession(sdk,CREDS,'123456')
        for request in ([], REQUEST*2, REQUEST*51, [dict(token='bad/url',symbol='NIFTYTESTCE')], [dict(token='123')]):
            with self.assertRaises(Refused): session.quotes(request)
        self.assertEqual(sdk.calls, [])

    def test_missing_stale_crossed_or_wrong_instrument_evidence_rejected(self):
        base = quote()
        for response in ([], [base,base], {'Error':'sensitive'}, [base | dict(exchange='mcx_fo')],
                         [base | dict(exchange_token='999')], [base | dict(display_symbol='OTHER')],
                         [base | dict(lstup_time='0')], [base | dict(depth={})],
                         [base | dict(depth=dict(buy=[dict(price='102',quantity=1)],sell=[dict(price='101',quantity=1)]))]):
            sdk = QuoteSDK()
            sdk.quotes = lambda **kwargs: copy.deepcopy(response)
            session = KotakSession(sdk, CREDS, '123456')
            with self.assertRaises(TransportFailure) as error: session.quotes(REQUEST)
            self.assertNotIn('sensitive', str(error.exception))
            self.assertTrue(session.closed)

    def test_bounded_process_returns_only_normalized_depth(self):
        with ProcessSession(CREDS,'123456',_factory=QuoteSDK) as session:
            value = session.request('quotes',instruments=REQUEST)
            self.assertEqual(value['ucc'],'TESTUCC')
            self.assertEqual(value['quotes'][0]['ask'],'101')
            self.assertNotIn('depth',value['quotes'][0])
            self.assertFalse(value['market_open_verified'])

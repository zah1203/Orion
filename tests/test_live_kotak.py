from dataclasses import asdict, replace
from datetime import timedelta
import logging
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from orion.live.kotak import KotakSession, OrderRequest, TransportFailure, make_client
from orion.live.ledger import Refused
from orion.live.session import ProcessSession

CREDS = dict(kotak_ucc='TESTUCC', kotak_mobile='synthetic-mobile',
             kotak_mpin='synthetic-pin', kotak_consumer_key='synthetic-token')
ROUTE = 'https://mis.kotaksecurities.com'
REQUEST = OrderRequest(tag='oriontest', symbol='TESTCE', quantity=10, lot_size=10,
                       tick='.05', price='100', kind='ENTRY')


class FakeSDK:
    def __init__(self, creds=None):
        self.configuration = SimpleNamespace(ucc='TESTUCC', base_url=ROUTE)
        self.calls = []
        self.response = dict(stat='Ok', stCode=200, nOrdNo='broker1')
        self.fail = None

    def totp_login(self, **kwargs):
        return dict(data=dict(status='success', token='fake', sid='fake', ucc='TESTUCC'))

    def totp_validate(self, **kwargs):
        return dict(data=dict(status='success', token='fake', sid='fake', kType='Trade', baseUrl=ROUTE))

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        return self.response

    def cancel_order(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def order_report(self):
        return dict(stat='Ok', stCode=200, data=[])

    positions = order_report


class HungSDK(FakeSDK):
    def place_order(self, **kwargs):
        # Only a marker in a disposable directory; no network or brokerage call.
        Path(self.marker).write_text('accepted')
        time.sleep(2)
        Path(self.marker).write_text('continued-after-timeout')
        return self.response


def hung_factory(creds):
    sdk = HungSDK()
    sdk.marker = creds['marker']
    return sdk


class NoisySDK(FakeSDK):
    def place_order(self, **kwargs):
        print('synthetic-secret-stdout', flush=True)
        os.write(2, b'synthetic-secret-stderr')
        raise RuntimeError('synthetic-secret-exception')


class KotakTests(unittest.TestCase):
    def test_exact_mapping_and_ack_is_not_fill(self):
        sdk = FakeSDK()
        session = KotakSession(sdk, CREDS, '123456')
        self.assertEqual(session.place(REQUEST), 'broker1')
        self.assertEqual(sdk.calls, [dict(exchange_segment='nse_fo', product='NRML',
            price='100', order_type='L', quantity='10', validity='DAY', trading_symbol='TESTCE',
            transaction_type='B', amo='NO', disclosed_quantity='0', trigger_price='0', tag='oriontest')])
        session.place(replace(REQUEST, kind='STOP', price='89.95', trigger='90'))
        self.assertEqual(sdk.calls[-1]['order_type'], 'SL')
        self.assertEqual(sdk.calls[-1]['transaction_type'], 'S')

    def test_malformed_orders_make_no_sdk_call(self):
        sdk = FakeSDK()
        session = KotakSession(sdk, CREDS, '123456')
        for changes in [dict(quantity=True), dict(quantity=11), dict(price='NaN'), dict(price='0'),
                        dict(price='100.01'), dict(tick='0'), dict(trigger='90'),
                        dict(kind='MKT'), dict(kind='STOP', trigger='90', price='91'), dict(tag='a\nsecret')]:
            with self.subTest(changes=changes), self.assertRaises(Refused):
                session.place(replace(REQUEST, **changes))
        self.assertEqual(sdk.calls, [])

    def test_errors_malformed_ack_and_timeout_are_never_retried(self):
        for value in [None, {}, {'Error': 'sensitive'}, {'Error Message':'sensitive'},
                      dict(stat='Ok', stCode=200, nOrdNo=''), dict(stat='Ok', stCode=200, nOrdNo=True),
                      dict(stat='Not_Ok', stCode=400, nOrdNo='broker1')]:
            sdk = FakeSDK(); sdk.response = value
            session = KotakSession(sdk, CREDS, '123456')
            for _ in range(2):
                with self.assertRaises(TransportFailure) as error:
                    session.place(REQUEST)
                self.assertNotIn('sensitive', str(error.exception))
            self.assertEqual(len(sdk.calls), 1)
        sdk = FakeSDK(); sdk.fail = TimeoutError('sensitive')
        session = KotakSession(sdk, CREDS, '123456')
        with self.assertRaises(TransportFailure):
            session.place(REQUEST)
        self.assertEqual(len(sdk.calls), 1)

    def test_session_identity_routing_expiry_and_cancel_identity(self):
        for change in ('ucc', 'base_url', 'expiry', 'day'):
            sdk = FakeSDK(); session = KotakSession(sdk, CREDS, '123456')
            if change == 'ucc': sdk.configuration.ucc = 'OTHER'
            if change == 'base_url': sdk.configuration.base_url = 'https://example.org'
            if change == 'expiry': session.started -= 6*3600+1
            if change == 'day': session.day -= timedelta(days=1)
            with self.assertRaises(TransportFailure): session.place(REQUEST)
            self.assertFalse(sdk.calls)
        sdk = FakeSDK(); session = KotakSession(sdk, CREDS, '123456')
        with self.assertRaises(TransportFailure): session.cancel('different-order')
        self.assertEqual(len(sdk.calls), 1)
        with self.assertRaises(TransportFailure):
            KotakSession(FakeSDK(), CREDS | {'kotak_ucc':'OTHER'}, '123456')

    def test_snapshot_explicitly_does_not_certify_cash_or_history(self):
        session = KotakSession(FakeSDK(), CREDS, '123456')
        self.assertEqual(session.snapshot()['orders'], [])
        self.assertFalse(session.snapshot()['historical_complete'])
        self.assertFalse(session.snapshot()['available_cash_verified'])

    def test_process_contains_sdk_errors_and_closes(self):
        with ProcessSession(CREDS, '123456', _factory=NoisySDK) as session:
            with self.assertRaises(TransportFailure) as error:
                session.request('place', request=asdict(REQUEST))
            self.assertNotIn('synthetic-secret', str(error.exception))
            self.assertFalse(session.child.is_alive())
            with self.assertRaises(TransportFailure):
                session.request('place', request=asdict(REQUEST))

    def test_timeout_kills_reaps_and_prevents_late_sdk_continuation(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp)/'accepted'
            with ProcessSession(CREDS | {'marker':str(marker)}, '123456', _factory=hung_factory) as session:
                session.timeout = .3
                with self.assertRaises(TransportFailure):
                    session.request('place', request=asdict(REQUEST))
                self.assertFalse(session.child.is_alive())
                self.assertEqual(marker.read_text(), 'accepted')
                self.assertIsNotNone(session.child.exitcode)
                self.assertFalse(Path(session.temp.name).exists())

    def test_real_pinned_sdk_uses_mock_http_no_redirect_or_retry(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(307, headers={'location':'https://example.org/steal'}, json={})
        with patch.dict(os.environ, {'NEO_LOG_FILE_ENABLED':'false'}):
            logging.disable(logging.CRITICAL)
            try:
                client = make_client(CREDS, transport=httpx.MockTransport(handler))
                client.configuration.edit_token = 'fake'
                client.configuration.edit_sid = 'fake'
                client.configuration.base_url = ROUTE
                client.configuration.ucc = 'TESTUCC'
                # Authentication is separately covered with FakeSDK; only the
                # installed SDK's actual place_order encoding runs in this case.
                response = client.place_order(**REQUEST.parameters())
                self.assertEqual(len(requests), 1)
                self.assertNotIn('example.org', str(requests[0].url))
                self.assertFalse(client.api_client.rest_client.session.follow_redirects)
                from orion.live.kotak import acknowledgement
                with self.assertRaises(TransportFailure): acknowledgement(response)
                client.api_client.rest_client.session.close()
            finally:
                logging.disable(logging.NOTSET)


if __name__ == '__main__':
    unittest.main()

"""Offline-only MCX transport tests: synthetic SDKs and mock HTTP, no account."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from neo_api_client.websocket.feed import SFeedMarketStatus

from orion.live.kotak import KotakSession, OrderRequest, TransportFailure, make_client
from orion.live.ledger import Refused
from orion.live.market import read
from orion.live.quotes import instruments
from orion.live.session import ProcessSession
from test_live_kotak import CREDS, ROUTE, FakeSDK
from test_live_market import Socket


MCX = OrderRequest('commodity', 'SYNTHETICMCXCE', 10, 10, '.05', '100', 'ENTRY',
                   segment='mcx_fo')


def quote(segment, token='123', symbol='SYNTHETICCE'):
    return dict(exchange_token=token, display_symbol=symbol, exchange=segment,
        lstup_time=str(int(datetime.now(timezone.utc).timestamp())),
        depth=dict(buy=[dict(price='100', quantity='10')],
                   sell=[dict(price='101', quantity='20')]))


class SegmentSDK(FakeSDK):
    def quotes(self, **kwargs):
        self.calls.append(kwargs)
        return [quote(row['exchange_segment'], row['instrument_token'])
                for row in kwargs['instrument_tokens']]

    def create_websocket(self, **kwargs):
        self.calls.append(kwargs)
        return Socket([SFeedMarketStatus(exchange_segment='nse_fo', status_code=2, status='closed'),
                       SFeedMarketStatus(exchange_segment='mcx_fo', status_code=1, status='open')])

    def limits(self):
        self.calls.append('limits')
        raise AssertionError('Unverified MCX funding must not call limits')

    def margin_required(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError('Unverified MCX funding must not call margin')


class MCXTransportTests(unittest.TestCase):
    def test_segment_is_appended_without_reinterpreting_positional_trigger(self):
        legacy = OrderRequest('stop', 'TESTCE', 10, 10, '.05', '89.95', 'STOP', '90')
        self.assertEqual(legacy.segment, 'nse_fo')
        self.assertEqual(legacy.parameters()['trigger_price'], '90')
        explicit = OrderRequest('stop', 'TESTCE', 10, 10, '.05', '89.95', 'STOP', '90', 'mcx_fo')
        self.assertEqual(explicit.parameters()['exchange_segment'], 'mcx_fo')

    def test_mcx_transport_preserves_nrml_and_only_limit_or_stop_limit(self):
        sdk = SegmentSDK()
        session = KotakSession(sdk, CREDS, '123456')
        for request, order_type, side in ((replace(MCX, kind='EXIT'), 'L', 'S'),
                (replace(MCX, kind='STOP', price='89.95', trigger='90'), 'SL', 'S')):
            self.assertEqual(session.place(request), 'broker1')
            self.assertEqual(sdk.calls[-1]['exchange_segment'], 'mcx_fo')
            self.assertEqual(sdk.calls[-1]['product'], 'NRML')
            self.assertEqual(sdk.calls[-1]['order_type'], order_type)
            self.assertEqual(sdk.calls[-1]['transaction_type'], side)
            self.assertEqual(sdk.calls[-1]['amo'], 'NO')

    def test_mcx_entry_is_refused_before_sdk_while_pure_encoding_is_available(self):
        params = MCX.parameters()
        self.assertEqual(params['exchange_segment'], 'mcx_fo')
        self.assertEqual(params['product'], 'NRML')
        self.assertEqual(params['order_type'], 'L')
        self.assertEqual(params['transaction_type'], 'B')
        sdk = SegmentSDK()
        session = KotakSession(sdk, CREDS, '123456')
        with self.assertRaisesRegex(Refused, 'MCX entry submission remains disabled'):
            session.place(MCX)
        self.assertEqual(sdk.calls, [])
        self.assertFalse(session.closed)
        self.assertEqual(session.place(replace(MCX, kind='EXIT')), 'broker1')

    def test_direct_process_ipc_cannot_submit_mcx_entry(self):
        with ProcessSession(CREDS, '123456', _factory=SegmentSDK) as session:
            with self.assertRaises(TransportFailure):
                session.request('place', request=asdict(MCX))
            self.assertTrue(session.closed)

    def test_invalid_or_ambiguous_segments_never_reach_sdk(self):
        sdk = SegmentSDK()
        session = KotakSession(sdk, CREDS, '123456')
        for segment in ('MCX', 'mcx', 'MCX_FO', 'mcx_fo ', 'nse_cm', '', None, True, []):
            with self.subTest(segment=segment):
                with self.assertRaises(Refused): session.place(replace(MCX, segment=segment))
                with self.assertRaises(Refused): session.market(segment)
                with self.assertRaises(Refused):
                    session.quotes([dict(segment=segment, token='123', symbol='SYNTHETICCE')])
        self.assertEqual(sdk.calls, [])

    def test_same_token_and_symbol_in_different_segments_stay_distinct(self):
        requested = [dict(segment=segment, token='123', symbol='SYNTHETICCE')
                     for segment in ('nse_fo', 'mcx_fo')]
        self.assertEqual(set(instruments(requested)), {('nse_fo', '123'), ('mcx_fo', '123')})
        sdk = SegmentSDK()
        result = KotakSession(sdk, CREDS, '123456').quotes(requested)
        self.assertEqual(sdk.calls, [dict(instrument_tokens=[
            dict(exchange_segment='nse_fo', instrument_token='123'),
            dict(exchange_segment='mcx_fo', instrument_token='123')], quote_type='all')])
        self.assertEqual([row['segment'] for row in result['quotes']], ['nse_fo', 'mcx_fo'])
        self.assertFalse(result['market_open_verified'])
        self.assertFalse(result['order_submission_available'])

    def test_explicit_and_legacy_nse_duplicate_is_rejected(self):
        sdk = SegmentSDK()
        with self.assertRaises(Refused):
            KotakSession(sdk, CREDS, '123456').quotes([
                dict(token='123', symbol='SYNTHETICCE'),
                dict(segment='nse_fo', token='123', symbol='SYNTHETICCE')])
        self.assertEqual(sdk.calls, [])

    def test_response_cannot_substitute_exchange_or_confuse_colliding_tokens(self):
        requested = [dict(segment=segment, token='123', symbol='SYNTHETICCE')
                     for segment in ('nse_fo', 'mcx_fo')]
        for response in ([quote('nse_fo'), quote('nse_fo')],
                         [quote('mcx_fo'), quote('mcx_fo')],
                         [quote('nse_fo'), quote('mcx_fo', symbol='OTHER')],
                         [quote('nse_fo'), quote('bse_fo')],
                         [quote('nse_fo'), quote(None)]):
            sdk = SegmentSDK()
            sdk.quotes = lambda **kwargs: response
            session = KotakSession(sdk, CREDS, '123456')
            with self.assertRaises(TransportFailure): session.quotes(requested)
            self.assertTrue(session.closed)

    def test_legacy_request_never_discovers_or_falls_back_to_mcx(self):
        sdk = SegmentSDK()
        sdk.quotes = lambda **kwargs: [quote('mcx_fo')]
        with self.assertRaises(TransportFailure):
            KotakSession(sdk, CREDS, '123456').quotes([dict(token='123', symbol='SYNTHETICCE')])

    def test_stale_mcx_quote_does_not_disable_independent_book_reads(self):
        sdk = SegmentSDK()
        sdk.quotes = lambda **kwargs: [quote('mcx_fo') | dict(lstup_time='0')]
        session = KotakSession(sdk, CREDS, '123456')
        value = session.quotes([dict(segment='mcx_fo', token='123', symbol='SYNTHETICCE')])
        self.assertTrue(value['quotes_unavailable'])
        self.assertEqual(value['quotes'], [])
        self.assertFalse(value['order_submission_available'])
        self.assertEqual(session.snapshot()['orders'], [])

    def test_only_matching_typed_market_event_is_observed(self):
        sdk = SegmentSDK()
        value = KotakSession(sdk, CREDS, '123456').market('mcx_fo')
        self.assertEqual(value['segment'], 'mcx_fo')
        self.assertEqual(value['status_code'], 1)
        self.assertFalse(value['order_submission_available'])
        self.assertEqual(len(sdk.calls), 1)
        self.assertEqual(sdk.calls[0]['max_connect_retries'], 0)
        self.assertEqual(sdk.calls[0]['max_reconnect_attempts'], 0)
        self.assertTrue(sdk.calls[0]['verify_ssl'])

    def test_nse_status_cannot_make_mcx_market_available(self):
        ws = Socket([SFeedMarketStatus(exchange_segment='nse_fo', status_code=1, status='open'),
                     dict(exchange_segment='mcx_fo', status_code=1)])
        sdk = SegmentSDK()
        sdk.create_websocket = lambda **kwargs: ws
        session = KotakSession(sdk, CREDS, '123456')
        value = session.market('mcx_fo')
        self.assertEqual(value['segment'], 'mcx_fo')
        self.assertTrue(value['market_unavailable'])
        self.assertIsNone(value['status_code'])
        self.assertTrue(ws.closed)
        self.assertEqual(session.snapshot()['orders'], [])

    def test_invalid_market_segment_is_rejected_before_socket_creation(self):
        calls = []
        with self.assertRaises(Refused):
            read(SimpleNamespace(create_websocket=lambda **kwargs: calls.append(kwargs)), segment=None)
        self.assertEqual(calls, [])

    def test_mcx_margin_and_funding_fail_closed_before_any_account_read(self):
        sdk = SegmentSDK()
        session = KotakSession(sdk, CREDS, '123456')
        for method in (session.margin, session.funding):
            with self.assertRaisesRegex(Refused, 'unverified'): method(MCX, '123')
        self.assertEqual(sdk.calls, [])
        self.assertFalse(session.closed)
        self.assertFalse(session.snapshot()['available_cash_verified'])

    def test_process_boundary_preserves_exact_segment(self):
        with ProcessSession(CREDS, '123456', _factory=SegmentSDK) as session:
            market = session.request('market', segment='mcx_fo')
            self.assertEqual((market['segment'], market['status_code']), ('mcx_fo', 1))
            quotes = session.request('quotes', instruments=[
                dict(segment='mcx_fo', token='123', symbol='SYNTHETICCE')])
            self.assertEqual(quotes['quotes'][0]['segment'], 'mcx_fo')
            self.assertEqual(session.request('place', request=asdict(replace(MCX, kind='EXIT'))),
                             {'broker_id': 'broker1'})
            deadline = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
            self.assertEqual(session.request('modify', broker_id='broker1', deadline=deadline,
                request=asdict(replace(MCX, kind='STOP', price='89.95', trigger='90'))),
                {'broker_id': 'broker1'})

    def test_pinned_sdk_encodes_mcx_as_explicit_exchange_without_network(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=dict(stat='Ok', stCode=200, nOrdNo='123456789'))
        with patch.dict(os.environ, {'NEO_LOG_FILE_ENABLED': 'false'}):
            client = make_client(CREDS, transport=httpx.MockTransport(handler))
            try:
                client.configuration.edit_token = 'fake'
                client.configuration.edit_sid = 'fake'
                client.configuration.base_url = ROUTE
                client.configuration.ucc = 'TESTUCC'
                client.place_order(**replace(MCX, kind='EXIT').parameters())
                self.assertEqual(len(requests), 1)
                payload = json.loads(parse_qs(requests[0].content.decode())['jData'][0])
                self.assertEqual(payload['es'], 'mcx_fo')
                self.assertEqual(payload['pc'], 'NRML')
                self.assertEqual(payload['pt'], 'L')
                self.assertFalse(client.api_client.rest_client.session.follow_redirects)
            finally:
                client.api_client.rest_client.session.close()


if __name__ == '__main__':
    unittest.main()

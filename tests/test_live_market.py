import unittest
from types import SimpleNamespace
from neo_api_client.websocket.feed import SFeedMarketStatus
from orion.live.market import read
from orion.live.ledger import Refused
from orion.live.session import ProcessSession
from test_live_kotak import FakeSDK, CREDS


class Socket:
    def __init__(self,events): self.events=iter(events); self.closed=False; self.subscribed=False
    async def __aenter__(self): return self
    async def __aexit__(self,*args): self.closed=True
    async def subscribe_exchange(self): self.subscribed=True
    def __aiter__(self): return self
    async def __anext__(self):
        try:return next(self.events)
        except StopIteration:raise StopAsyncIteration


class MarketSDK(FakeSDK):
    def create_websocket(self,**kwargs):
        return Socket([SFeedMarketStatus(exchange_segment='nse_fo',status_code=1,status='Market open')])


class NoStatusSDK(FakeSDK):
    def create_websocket(self,**kwargs): return Socket([])


class MarketTests(unittest.TestCase):
    def test_process_boundary_supports_market_and_retains_rest_on_no_status(self):
        with ProcessSession(CREDS,'123456',_factory=MarketSDK) as session:
            result=session.request('market')
            self.assertEqual(result['ucc'],'TESTUCC')
            self.assertEqual(result['status_code'],1)
            self.assertFalse(result['order_submission_available'])
        with ProcessSession(CREDS,'123456',_factory=NoStatusSDK) as session:
            result=session.request('market')
            self.assertTrue(result['market_unavailable'])
            self.assertIsNone(result['status_code'])
            session.request('snapshot')

    def test_typed_nse_event_only_and_no_reconnection(self):
        ws=Socket([{'status_code':1},SFeedMarketStatus(exchange_segment='bse_cm',status_code=1,status='open'),
                   SFeedMarketStatus(exchange_segment='nse_fo',status_code=2,status='closed')])
        calls=[]
        def create(**kw): calls.append(kw); return ws
        value=read(SimpleNamespace(create_websocket=create))
        self.assertEqual(value['status_code'],2)
        self.assertTrue(ws.closed);self.assertTrue(ws.subscribed)
        self.assertEqual(calls[0]['max_connect_retries'],0)
        self.assertEqual(calls[0]['max_reconnect_attempts'],0)
        self.assertTrue(calls[0]['verify_ssl'])

    def test_no_event_is_unavailable_not_open(self):
        ws=Socket([])
        with self.assertRaises(Refused):read(SimpleNamespace(create_websocket=lambda **kw:ws))
        self.assertTrue(ws.closed)

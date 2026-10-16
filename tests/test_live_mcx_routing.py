"""Synthetic MCX attestations exercise offline routing, not verified market data."""
from copy import deepcopy
import hashlib
import json
from datetime import datetime, timedelta, timezone
import unittest

from orion.live.accounting import IST
from orion.live.commodity import review_contract, validate_saved_contract
from orion.live.ledger import Refused
import test_live_routing
CHANNEL = test_live_routing.CHANNEL


class CommodityRoutingTests(unittest.TestCase):
    setUp = test_live_routing.RoutingTests.setUp
    tearDown = test_live_routing.RoutingTests.tearDown
    message = test_live_routing.RoutingTests.message
    quote = test_live_routing.RoutingTests.quote

    def fixture(self):
        settings = self.store.user(self.owner)['settings']
        self.store.save_settings(self.owner, settings | dict(channels={CHANNEL:dict(profile='commodity', products=['CRUDEOIL'])}))
        expiry = (self.now.astimezone(IST)+timedelta(days=2)).replace(hour=22, minute=0, second=0, microsecond=0)
        self.text = 'CRUDEOIL '+expiry.strftime('%d %b %Y').upper()+' 6000 CE ACTION: BUY ENTRY PRICE RANGE: 100-102 SL: 90 TARGETS: 110/120/130'
        self.master = dict(synthetic=False, economics_reused=False,
            as_of=self.now.astimezone(IST).date().isoformat(), verified_at=self.now.isoformat(),
            source_sha256=hashlib.sha256(json.dumps({'mcx_fo':'b'*64}, sort_keys=True).encode()).hexdigest(), sources={'mcx_fo':'b'*64},
            economics=dict(verified_on=self.now.astimezone(IST).date().isoformat(), source='synthetic test evidence',
                products={'CRUDEOIL':dict(verified_lot_sizes=[1], premium_rupees_per_quantity_point='100',
                    price_units='synthetic barrel', delivery_units='synthetic lot', expiry_time_ist='22:00:00')}),
            contracts=[dict(product='CRUDEOIL', strike='6000', option_type='CE', segment='mcx_fo',
                expiry=expiry.date().isoformat(), expiry_at=expiry.isoformat(), symbol='CRUDETEST', token='123',
                order_quantity_per_lot=1, premium_multiplier='100', tick_size='.1')])
        # Explicit synthetic test budget; never user limits or defaults.
        from test_live_pilot import LIMITS
        self.pilot.configure(self.owner, self.owner, LIMITS | dict(capital='50000', max_order_premium='50000',
            max_open_premium='50000', max_trade_loss='5000', max_open_risk='10000', daily_loss='10000', max_lots=2))
        self.pilot.consent(self.owner, 2)

    def test_exact_lot_money_and_broker_units_without_order(self):
        self.fixture(); self.message()
        candidate = self.quote(segment='mcx_fo')
        self.assertEqual(candidate['lots'], 2)
        self.assertEqual(candidate['quantity'], 2)
        self.assertEqual(candidate['premium_multiplier'], '100')
        self.assertEqual(candidate['quantity_multiplier'], '1E+2')
        self.assertFalse(candidate['order_submission_available'])
        self.assertIn('mcx-broker-cash-and-settlement-verification', candidate['blockers'])
        self.assertFalse(self.ledgers[self.owner].db.execute('SELECT 1 FROM intents').fetchone())
        with self.assertRaises(Refused): self.quote(segment='nse_fo')

    def test_missing_stale_reused_or_changed_economics_rejected(self):
        self.fixture()
        for mutate in [lambda m:m.pop('economics'), lambda m:m.update(economics_reused=True),
                       lambda m:m['economics'].update(verified_on='2000-01-01'),
                       lambda m:m.update(source_sha256='not a hash'),
                       lambda m:m['economics']['products']['CRUDEOIL'].update(premium_rupees_per_quantity_point='1'),
                       lambda m:m['economics']['products']['CRUDEOIL'].update(verified_lot_sizes=[True])]:
            master=deepcopy(self.master); mutate(master)
            with self.assertRaises(Refused): self.message(master=master)

    def test_expiry_day_and_product_substitution_rejected(self):
        self.fixture()
        m=deepcopy(self.master)
        c=m['contracts'][0]
        c['expiry_at']=self.now.astimezone(IST).replace(hour=22, minute=0).isoformat()
        c['expiry']=self.now.astimezone(IST).date().isoformat()
        with self.assertRaises(Refused): review_contract(c,m,self.now)
        with self.assertRaises(Refused): self.message(text=self.text.replace('CRUDEOIL','CRUDEOILM'))
        with self.assertRaises(Refused): self.message(text=self.text+' BTST')
        with self.assertRaises(Refused): self.message(text=self.text.replace('ACTION: BUY ENTRY PRICE RANGE: 100-102','BUY ABOVE 100'))

    def test_saved_contract_identity_and_review_day_cannot_change(self):
        self.fixture()
        c=deepcopy(self.master['contracts'][0])
        c['commodity_evidence']=review_contract(c,self.master,self.now)
        validate_saved_contract(c,self.now)
        for field,value in [('token','124'),('premium_multiplier','1'),('tick_size','.05'),('order_quantity_per_lot',10)]:
            changed=deepcopy(c); changed[field]=value
            with self.assertRaises(Refused): validate_saved_contract(changed,self.now)
        with self.assertRaises(Refused): validate_saved_contract(c,self.now+timedelta(days=1))


if __name__ == '__main__': unittest.main()

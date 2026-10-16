"""Synthetic economics only; these are not approved product specifications."""
import unittest
from decimal import Decimal, localcontext

from orion.live.contract_economics import ContractEconomics
from orion.live.ledger import Refused


class ContractEconomicsTests(unittest.TestCase):
    def test_lot_money_is_not_broker_quantity(self):
        e = ContractEconomics(1, '100', '.1')
        self.assertEqual(e.broker_quantity(2), 2)
        self.assertEqual(e.premium('50', 2), Decimal('10000'))
        self.assertEqual(e.stop_risk('50', '45', 2), Decimal('1000'))
        self.assertEqual(e.rupees_per_quantity_point, Decimal('100'))
        self.assertEqual(e.gap_price(3), Decimal('.3'))

    def test_quantity_and_lot_representations_agree(self):
        for quantity, multiplier in [(25, '25'), (10, '100'), (1, '30')]:
            e = ContractEconomics(quantity, multiplier, '.05')
            self.assertEqual(e.premium('50.05', 3),
                             Decimal('50.05') * e.broker_quantity(3) * e.rupees_per_quantity_point)

    def test_decimal_context_does_not_round_premium(self):
        e = ContractEconomics(1, '100', '.1')
        with localcontext() as ctx:
            ctx.prec = 3
            self.assertEqual(e.premium('12345.6', 2), Decimal('2469120'))

    def test_no_defaults_or_fractional_lots(self):
        for c in [{}, {'order_quantity_per_lot': 1}]:
            with self.assertRaises(Refused):
                ContractEconomics.from_contract(c)
        for lot in [0, -1, True, 1.5, '1', 100001]:
            with self.assertRaises(Refused):
                ContractEconomics(lot, '100', '.1')
        e = ContractEconomics(1, '100', '.1')
        for lots in [0, True, 1.5, 1000001]:
            with self.assertRaises(Refused):
                e.broker_quantity(lots)

    def test_invalid_or_unrepresentable_factors(self):
        for value in ['0', '-1', 'NaN', 'Infinity', True, '0.000000001']:
            with self.assertRaises(Refused):
                ContractEconomics(1, value, '.1')
            with self.assertRaises(Refused):
                ContractEconomics(1, '100', value)
        for quantity, multiplier in [(3, '100'), (100000, '.00000001')]:
            with self.assertRaises(Refused):
                ContractEconomics(quantity, multiplier, '.1')

    def test_ticks_and_long_stop_direction(self):
        e = ContractEconomics(1, '100', '.1')
        with self.assertRaises(Refused):
            e.premium('50.05', 1)
        for stop in ['50', '51', '0', '49.95']:
            with self.assertRaises(Refused):
                e.stop_risk('50', stop, 1)
        for gap in [0, True, 1.5]:
            with self.assertRaises(Refused):
                e.gap_price(gap)


if __name__ == '__main__':
    unittest.main()

"""Offline contract-unit arithmetic, deliberately disconnected from Live routing.

Input validation is NOT verification of broker economics, an admission decision,
funds evidence, or permission to submit orders. No product defaults are provided.
The normalized catalogue's premium_multiplier is PER LOT; accounting's multiplier
is PER BROKER QUANTITY UNIT. Never pass one directly as the other.
"""
from dataclasses import dataclass
from decimal import Decimal, Inexact, localcontext

from .accounting import bounded_amount
from .ledger import Refused, positive_int


@dataclass(frozen=True)
class ContractEconomics:
    order_quantity_per_lot: int
    premium_rupees_per_lot_point: Decimal
    tick_size: Decimal

    def __post_init__(self):
        positive_int(self.order_quantity_per_lot)
        if self.order_quantity_per_lot > 100000:
            raise Refused('Broker lot quantity exceeds supported bound')
        for name in ('premium_rupees_per_lot_point', 'tick_size'):
            value = bounded_amount(getattr(self, name))
            if not value:
                raise Refused('Positive contract economics required')
            object.__setattr__(self, name, value)
        # The existing accounting API admits at most eight decimal places.
        # Refuse an unrepresentable factor, rather than silently rounding money.
        self.rupees_per_quantity_point

    @classmethod
    def from_contract(cls, contract):
        try:
            return cls(contract['order_quantity_per_lot'], contract['premium_multiplier'],
                       contract['tick_size'])
        except (KeyError, TypeError):
            raise Refused('Explicit normalized contract economics required') from None

    @property
    def rupees_per_quantity_point(self):
        with localcontext() as ctx:
            ctx.prec = 80
            ctx.traps[Inexact] = True
            try:
                factor = self.premium_rupees_per_lot_point / self.order_quantity_per_lot
            except Inexact:
                raise Refused('Non-terminating quantity multiplier is unsupported') from None
            return bounded_amount(factor.normalize())

    def broker_quantity(self, lots):
        positive_int(lots)
        quantity = lots * self.order_quantity_per_lot
        if quantity > 1000000:
            raise Refused('Order quantity exceeds adapter bound')
        return quantity

    def aligned_price(self, value):
        price = bounded_amount(value)
        with localcontext() as ctx:
            ctx.prec = 80
            if price % self.tick_size:
                raise Refused('Price is off the contract tick grid')
        return price

    def premium(self, price, lots):
        self.broker_quantity(lots)
        price = self.aligned_price(price)
        with localcontext() as ctx:
            ctx.prec = 80
            return price * self.premium_rupees_per_lot_point * lots

    def stop_risk(self, entry, stop, lots):
        entry, stop = self.aligned_price(entry), self.aligned_price(stop)
        if not 0 < stop < entry:
            raise Refused('Long option stop must be positive and below entry')
        with localcontext() as ctx:
            ctx.prec = 80
            return self.premium(entry-stop, lots)

    def gap_price(self, ticks):
        positive_int(ticks)
        with localcontext() as ctx:
            ctx.prec = 80
            return self.tick_size * ticks

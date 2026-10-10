"""Strict Kotak SDK adapter. No portal/worker constructs this adapter yet.

Only the isolated session process may use it with a real SDK client. The caller
must commit a dispatch intent before calling place/cancel; this layer never
retries, infers fills from acknowledgements, or grants permission to trade.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from urllib.parse import urlsplit

from .accounting import IST, bounded_amount
from .ledger import Refused, positive_int
from .readonly import auth, account_match, envelope, ProbeFailure
from .reconciliation import normalize_orders, normalize_positions, text_field


class TransportFailure(Refused):
    """Intentionally contains no SDK error text, credentials or responses."""


@dataclass(frozen=True)
class OrderRequest:
    tag: str
    symbol: str
    quantity: int
    lot_size: int
    tick: str
    price: str
    kind: str
    trigger: str = '0'

    def parameters(self):
        if not isinstance(self.tag, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', self.tag):
            raise Refused('Invalid order tag')
        text_field(self.symbol)
        positive_int(self.quantity); positive_int(self.lot_size)
        if self.quantity > 1000000 or self.quantity % self.lot_size:
            raise Refused('Whole-lot order required')
        tick, price, trigger = (bounded_amount(v) for v in (self.tick, self.price, self.trigger))
        if not tick or not price or price % tick or trigger % tick:
            raise Refused('Tick-aligned positive limit required')
        if self.kind not in ('ENTRY', 'STOP', 'EXIT'):
            raise Refused('Unsupported order purpose')
        if (self.kind == 'STOP' and not 0 < price <= trigger) or (self.kind != 'STOP' and trigger):
            raise Refused('Invalid protective limit/trigger')
        # NSE NRML pilot only. No market, AMO, margin or caller-defined SDK kwargs.
        return dict(exchange_segment='nse_fo', product='NRML', price=format(price, 'f'),
                    order_type='SL' if self.kind == 'STOP' else 'L',
                    quantity=str(self.quantity), validity='DAY', trading_symbol=self.symbol,
                    transaction_type='B' if self.kind == 'ENTRY' else 'S', amo='NO',
                    disclosed_quantity='0', trigger_price=format(trigger, 'f'), tag=self.tag)


def acknowledgement(response, expected=None):
    try:
        value = envelope(response)
        if type(value.get('stCode')) is not int or value.get('errMsg') not in (None, ''):
            raise ValueError
        oid = value.get('nOrdNo')
        if not isinstance(oid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', oid):
            raise ValueError
        if expected is not None and oid != expected:
            raise ValueError
        return oid
    except (ProbeFailure, ValueError, TypeError):
        raise TransportFailure('Broker outcome unknown; reconciliation required') from None


def safe_route(value):
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or
                not url.hostname.endswith('.kotaksecurities.com') or url.username or url.password or
                url.port not in (None, 443) or url.query or url.fragment):
            raise ValueError
    except (ValueError, TypeError):
        raise TransportFailure('Broker route rejected') from None


class KotakSession:
    def __init__(self, client, creds, totp):
        self.client = client
        self.ucc = text_field(creds.get('kotak_ucc'))
        self.closed = True
        self.started = time.monotonic()
        self.day = datetime.now(IST).date()
        try:
            if not isinstance(totp, str) or not re.fullmatch(r'\d{6}', totp):
                raise ValueError
            first = auth(client.totp_login(mobile_number=creds['kotak_mobile'], ucc=self.ucc, totp=totp))
            account_match(first.get('ucc'), self.ucc)
            second = auth(client.totp_validate(mpin=creds['kotak_mpin']))
            account_match(client.configuration.ucc, self.ucc)
            if second.get('ucc') is not None:
                account_match(second['ucc'], self.ucc)
            if second.get('kType') != 'Trade':
                raise ValueError
            self.route = second.get('baseUrl')
            safe_route(self.route)
            if client.configuration.base_url != self.route:
                raise ValueError
            self.closed = False
        except Exception:
            raise TransportFailure('Broker authentication rejected') from None

    def _check(self):
        if (self.closed or datetime.now(IST).date() != self.day or
                not 0 <= time.monotonic()-self.started <= 6*3600 or
                self.client.configuration.ucc != self.ucc or
                self.client.configuration.base_url != self.route):
            self.closed = True
            raise TransportFailure('Broker session expired or changed')

    def place(self, request):
        self._check()
        params = request.parameters()
        try:
            return acknowledgement(self.client.place_order(**params))
        except Exception:
            self.closed = True
            raise TransportFailure('Broker outcome unknown; reconciliation required') from None

    def cancel(self, oid):
        self._check()
        text_field(oid)
        try:
            return acknowledgement(self.client.cancel_order(order_id=oid, amo='NO'), expected=oid)
        except Exception:
            self.closed = True
            raise TransportFailure('Broker cancellation unknown; reconciliation required') from None

    def snapshot(self):
        """Sanitized current books, not proof of historical completeness or cash."""
        self._check()
        started = datetime.now(timezone.utc)
        clock = time.monotonic()
        try:
            orders = normalize_orders(self.client.order_report(), self.ucc)
            positions = normalize_positions(self.client.positions(), self.ucc)
            self._check()
            if time.monotonic()-clock > 20:
                raise ValueError
            return dict(ucc=self.ucc, started_at=started.isoformat(), completed_at=datetime.now(timezone.utc).isoformat(),
                        orders=[dict(broker_id=oid, **row) for oid, row in orders.items()],
                        positions=[dict(instrument=key, quantity=qty) for key, qty in positions.items()],
                        historical_complete=False, available_cash_verified=False)
        except Exception:
            self.closed = True
            raise TransportFailure('Broker snapshot rejected') from None

    def quotes(self, requested):
        """Account-session-bound depth reads; no market-open assertion or retry."""
        from .quotes import instruments, normalize_quotes
        expected = instruments(requested)
        self._check()
        try:
            response = self.client.quotes(instrument_tokens=[dict(exchange_segment='nse_fo', instrument_token=token)
                                          for token in expected], quote_type='all')
            self._check()
            return dict(ucc=self.ucc, quotes=normalize_quotes(response, expected, now=datetime.now(timezone.utc)),
                        market_open_verified=False, order_submission_available=False)
        except Exception:
            self.closed = True
            raise TransportFailure('Broker quote evidence rejected') from None

    def evidence(self):
        """Collect stable books plus trades and RMS fields, without certifying cash.

        Book reads bracket the trade/position reads. A moving or inconsistent
        snapshot is rejected; it cannot authorize a new order. No raw SDK response
        or undocumented fields leave this boundary.
        """
        from .trades import normalize_trades
        self._check()
        started, clock = datetime.now(timezone.utc), time.monotonic()
        try:
            before = normalize_orders(self.client.order_report(), self.ucc)
            trades = self.client.trade_report()
            positions = normalize_positions(self.client.positions(), self.ucc)
            limits = envelope(self.client.limits())
            after = normalize_orders(self.client.order_report(), self.ucc)
            self._check()
            completed = datetime.now(timezone.utc)
            if before != after or time.monotonic()-clock > 20:
                raise ValueError
            # Net includes RMS adjustments/collateral. It is never available cash.
            from .readonly import number
            rms_net = str(number(limits.get('Net')))
            return dict(ucc=self.ucc, started_at=started.isoformat(), completed_at=completed.isoformat(),
                        orders=[dict(broker_id=oid, **row) for oid, row in after.items()],
                        positions=[dict(instrument=key, quantity=qty) for key, qty in positions.items()],
                        trades=normalize_trades(trades, self.ucc, after, completed_at=completed),
                        rms_net=rms_net, fees_verified=False, available_cash_verified=False,
                        historical_complete=False)
        except Exception:
            self.closed = True
            raise TransportFailure('Broker evidence rejected') from None


def make_client(creds, *, transport=None):
    """Pinned SDK factory; called inside the private child, never on import.

    HTTP MockTransport injection is used in tests. Production HTTPTransport has
    zero retries. Redirects could replay a POST or leak headers and are disabled.
    """
    from importlib.metadata import version
    import httpx
    from neo_api_client import NeoAPI
    if version('kotakneoapi') != '3.0.7':
        raise TransportFailure('Unreviewed broker SDK version')
    transport = transport if transport is not None else httpx.HTTPTransport(retries=0)
    client = NeoAPI(consumer_key=creds['kotak_consumer_key'], environment='prod',
                    transport=transport, timeout=10, http2=False)
    client.api_client.rest_client.session.follow_redirects = False
    return client

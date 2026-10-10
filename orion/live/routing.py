"""Account-bound signal admission; no broker transport or entry authority.

The production feed/launcher must supply authenticated message/quote provenance.
This component deliberately cannot convert a candidate into an order. It retains
only normalized contract/price data and a source digest, never raw channel text.
"""
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, localcontext
import hashlib
import json
import re

from ..core import parse_signal, resolve
from .accounting import timestamp, bounded_amount
from .ledger import Refused, amount
from .pilot import validate_limits
from .reconciliation import text_field


def reviewed_policy(store, uid, ucc):
    """Read current server policy inside the caller's account lock.

    A successful read is only configuration consent, never trade activation.
    Paper monetary settings, enabled state and cash are not imported.
    """
    with store.db() as db:
        db.execute('BEGIN')
        user = db.execute('SELECT access,settings FROM users WHERE id=?', (uid,)).fetchone()
        policy = db.execute('SELECT * FROM live_pilots WHERE uid=?', (uid,)).fetchone()
        if (not user or user['access'] != 'approved' or not policy or not policy['enrolled'] or
                policy['consent'] != policy['version'] or
                policy['ucc_hash'] != hashlib.sha256(text_field(ucc).encode()).hexdigest()):
            raise Refused('Current reviewed account policy required')
        settings = json.loads(user['settings'])
        channels = settings.get('channels', {})
        if not isinstance(channels, dict):
            raise Refused('Invalid selected channels')
        return dict(version=policy['version'], limits=validate_limits(json.loads(policy['limits'])),
                    channels=channels)


class SignalRouter:
    def __init__(self, store, uid, ledger, ucc):
        if ledger.account != uid:
            raise Refused('Signal ledger account mismatch')
        row = ledger.db.execute('SELECT ucc FROM reconciliation_account').fetchone()
        if not row or row[0] != ucc:
            raise Refused('Signal broker account mismatch')
        self.store, self.uid, self.ledger, self.ucc = store, uid, ledger, ucc
        self.db = ledger.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS live_signals(
            source TEXT PRIMARY KEY, digest TEXT NOT NULL, policy_version INTEGER NOT NULL,
            channel TEXT NOT NULL, channel_digest TEXT NOT NULL, signal_time TEXT NOT NULL,
            received_at TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL)''')

    def _channel(self, policy, channel):
        selected = policy['channels'].get(channel)
        if (not isinstance(selected, dict) or selected.get('profile') != 'index' or
                not isinstance(selected.get('products'), list)):
            raise Refused('Selected index channel required')
        digest = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
        return selected, digest

    def message(self, *, channel, message_id, text, source_at, received_at, master,
                edited=False, reply_to=None):
        """Reject backfill, edits, replies, unsupported signals and changed replay.

        Initial pilot admission accepts explicit-expiry NSE range signals only.
        Above/cross signals need a separately reviewed slippage/crossing policy;
        neither that policy nor overnight permission is copied from Paper.
        """
        now = datetime.now(timezone.utc)
        source_at, received_at = timestamp(source_at), timestamp(received_at)
        if (not isinstance(channel, str) or not re.fullmatch(r'-100[0-9]{4,16}', channel) or
                type(message_id) is not int or not 0 < message_id < 2**63 or
                type(edited) is not bool):
            raise Refused('Original bounded channel message required')
        if edited or reply_to is not None:
            with self.store.lock(self.uid), self.ledger.transaction():
                self.db.execute("UPDATE live_signals SET state='INVALIDATED' WHERE source=?",
                                (channel + ':' + str(message_id),))
            raise Refused('Edits and replies cannot enter Live routing')
        if not isinstance(text, str) or not 0 < len(text) <= 16384:
            raise Refused('Bounded source text required')
        if not 0 <= (received_at-source_at).total_seconds() <= 30 or not 0 <= (now-received_at).total_seconds() <= 5:
            raise Refused('Fresh source message required')
        source = channel + ':' + str(message_id)
        digest = hashlib.sha256((source_at.isoformat()+'\n'+text).encode()).hexdigest()
        with self.store.lock(self.uid):
            policy = reviewed_policy(self.store, self.uid, self.ucc)
            selected, channel_digest = self._channel(policy, channel)
            with self.ledger.transaction():
                old = self.db.execute('SELECT * FROM live_signals WHERE source=?', (source,)).fetchone()
                if old:
                    if old['digest'] != digest:
                        self.db.execute("UPDATE live_signals SET state='CONFLICT' WHERE source=?", (source,))
                    else:
                        return dict(source=source, duplicate=True, state=old['state'], order_submission_available=False)
            if old:
                raise Refused('Source message changed; candidate invalidated')
            try:
                signal = parse_signal(text, source_at.isoformat())
                if (signal['product'] not in selected['products'] or signal['product'] not in ('NIFTY','BANKNIFTY') or
                        not signal['expiry'] or signal['overnight'] or signal['entry_mode'] != 'range'):
                    raise ValueError
                contract = resolve(signal, master, now)
                if contract['segment'] != 'nse_fo' or amount(contract['premium_multiplier']) != 1:
                    raise ValueError
                lot = contract['order_quantity_per_lot']
                if type(lot) is not int or not 0 < lot <= 100000:
                    raise ValueError
                tick = bounded_amount(contract['tick_size'])
                if not tick or any(bounded_amount(v) % tick for v in [signal['entry_low'], signal['entry_high'], signal['stop'], *signal['targets']]):
                    raise ValueError
                text_field(contract['token']); text_field(contract['symbol'])
                if (datetime.fromisoformat(contract['expiry_at'])-now).total_seconds() <= 1800:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise Refused('Unsupported signal or unverified contract') from None
            contract = {k:contract[k] for k in ('product','strike','option_type','segment','expiry','expiry_at','symbol','token','order_quantity_per_lot','premium_multiplier','tick_size')}
            body = json.dumps(dict(signal=signal, contract=contract), sort_keys=True)
            with self.ledger.transaction():
                self.db.execute("INSERT INTO live_signals VALUES(?,?,?,?,?,?,?,?, 'PENDING')",
                    (source, digest, policy['version'], channel, channel_digest, source_at.isoformat(), received_at.isoformat(), body))
            return dict(source=source, duplicate=False, state='PENDING', order_submission_available=False)

    def quote(self, source, *, segment, token, bid, ask, quoted_at, market_open):
        """Size a candidate conservatively; no reservation, dispatch or resume.

        Even a matching quote is held behind the release's unresolved real-entry
        gates. This result is not a trusted funds assertion or an execution permit.
        """
        now, at = datetime.now(timezone.utc), timestamp(quoted_at)
        if market_open is not True or not 0 <= (now-at).total_seconds() <= 5:
            raise Refused('Fresh open-market quote required')
        bid, ask = bounded_amount(bid), bounded_amount(ask)
        if not 0 < bid <= ask or (ask-bid)/ask > amount('.02'):
            raise Refused('Invalid or excessive quote spread')
        with self.store.lock(self.uid):
            policy = reviewed_policy(self.store, self.uid, self.ucc)
            with self.ledger.transaction():
                row = self.db.execute('SELECT * FROM live_signals WHERE source=?', (source,)).fetchone()
                if not row or row['state'] != 'PENDING':
                    raise Refused('Pending source candidate required')
                _, channel_digest = self._channel(policy, row['channel'])
                if row['policy_version'] != policy['version'] or row['channel_digest'] != channel_digest:
                    raise Refused('Signal policy or channel changed')
                source_at = datetime.fromisoformat(row['signal_time'])
                if not 0 <= (now-source_at).total_seconds() <= 30 or at < source_at:
                    raise Refused('Signal expired or quote predates signal')
                value = json.loads(row['body'])
                signal, contract, limits = value['signal'], value['contract'], policy['limits']
                if (segment, token) != (contract['segment'], contract['token']):
                    raise Refused('Quote instrument mismatch')
                if (datetime.fromisoformat(contract['expiry_at'])-now).total_seconds() <= 1800:
                    raise Refused('Contract near expiry')
                tick = amount(contract['tick_size'])
                if bid % tick or ask % tick or not amount(signal['entry_low']) <= ask <= amount(signal['entry_high']):
                    raise Refused('Quote outside tick-aligned entry range')
                with localcontext() as ctx:
                    ctx.prec = 80
                    lot = contract['order_quantity_per_lot']
                    fee = amount(limits['fee_reserve'])
                    premium = min(amount(limits[k]) for k in ('capital','max_order_premium','max_open_premium'))-fee
                    risk = min(amount(limits[k]) for k in ('max_trade_loss','max_open_risk','daily_loss'))-fee
                    lots = min(limits['max_lots'], int((premium/(ask*lot)).to_integral_value(rounding=ROUND_FLOOR)),
                               int((risk/((ask-amount(signal['stop']))*lot)).to_integral_value(rounding=ROUND_FLOOR)))
                if lots <= 0:
                    raise Refused('No whole lot fits reviewed policy')
                return dict(account=self.uid, source=source, policy_version=policy['version'],
                    symbol=contract['symbol'], token=token, segment=segment, lots=lots,
                    quantity=lots*lot, lot_size=lot, limit_price=str(ask), tick=str(tick),
                    stop=signal['stop'], targets=signal['targets'],
                    order_submission_available=False,
                    blockers=['verified-cash-and-charges','current-ledger-risk-and-reservations',
                              'atomic-entry-authorization','production-feed-provenance'])

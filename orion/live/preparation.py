"""Atomic entry reservation; deliberately provides no broker dispatch authority."""
from datetime import datetime, timezone

from .accounting import Accounting
from .cash import CashAllocation
from .entry_plans import EntryPlans
from .execution import install_execution_tables
from .ledger import Limits, Refused
from .reconciliation import Reconciliation
from .routing import reviewed_policy


class EntryPreparation:
    def __init__(self, account):
        self.account = account
        self._leased()
        self.ledger, self.db = account.ledger, account.ledger.db
        self.ucc = account.session.ucc
        if self.ledger.account != account.uid:
            raise Refused('Preparation account mismatch')
        install_execution_tables(self.ledger)
        self.accounting = Accounting(self.ledger, self.ucc)
        self.cash = CashAllocation(self.accounting)
        self.reconciliation = Reconciliation(self.ledger, self.ucc)
        self.plans = EntryPlans(account.store, account.uid, account.monitor)

    def _leased(self):
        a = self.account
        if a.ledger is None or a.session is None or a.lease is None or a.lease.closed:
            raise Refused('Active account worker lease required')
        if hasattr(self, 'ledger') and (a.ledger is not self.ledger or a.session.ucc != self.ucc):
            raise Refused('Preparation session changed')

    def prepare(self, source, *, marks, **quote):
        self._leased()
        with self.account.store.lock(self.account.uid), self.ledger.transaction():
            return self._prepare_locked(source, marks=marks, **quote)

    def _prepare_locked(self, source, *, marks, expected=None, **quote):
        self._leased()
        if not self.db.in_transaction:
            raise Refused('Atomic preparation transaction required')
        candidate = self.plans.router._quote_locked(source, accounting=self.accounting,
                                                   marks=marks, cash=self.cash, **quote)
        if expected is not None and candidate != expected:
            raise Refused('Candidate changed before atomic reservation')
        policy = reviewed_policy(self.account.store, self.account.uid, self.ucc)
        limits = policy['limits']
        signal = self.db.execute('SELECT signal_time FROM live_signals WHERE source=?', (source,)).fetchone()
        row = self.ledger.reserve(event=source, symbol=candidate['symbol'], segment=candidate['segment'],
            lots=candidate['lots'], lot_size=candidate['lot_size'], limit_price=candidate['limit_price'],
            tick_size=candidate['tick'], signal_time=datetime.fromisoformat(signal[0]),
            quote_time=quote['quoted_at'], now=datetime.now(timezone.utc),
            limits=Limits(limits['max_lots'], limits['max_entries'], limits['max_open_premium'], limits['daily_loss']),
            realized_loss=candidate['risk']['loss_used'])
        tag = row['tag']
        self.reconciliation.bind_instrument(tag, product='NRML', token=candidate['token'])
        self.accounting.bind_multiplier(tag, 1)
        self.db.execute('INSERT INTO execution_terms VALUES(?,?,?,?,?)',
            (tag, candidate['stop'], candidate['tick'], candidate['lot_size'], limits['fee_reserve']))
        self.plans._bind_locked(tag)
        self.db.execute("UPDATE live_signals SET state='RESERVED' WHERE source=?", (source,))
        return dict(tag=tag, state='PREPARED', quantity=candidate['quantity'],
                    policy_version=policy['version'], order_submission_available=False)

    def abandon(self, tag):
        self._leased()
        with self.account.store.lock(self.account.uid), self.ledger.transaction():
            row = self.ledger.abandon_prepared(tag)
            self.db.execute("UPDATE live_signals SET state='INVALIDATED' WHERE source=?", (row['event'],))
            return dict(tag=tag, state='ABANDONED', order_submission_available=False)

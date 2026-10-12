"""Leased signal-to-entry coordinator, still behind the production release gate.

Only evaluate() is available in this release. submit() has no user override;
tests exercise its atomic handoff with synthetic sessions and a patched gate.
Activation additionally requires account-owned, one-shot entry permission.
"""
from datetime import datetime, timezone
import json
import sqlite3
import time

from .accounting import IST, timestamp
from .ledger import Refused
from .marks import collect_marks
from .preflight import funding_preflight
from .preparation import EntryPreparation
from . import status
from .authorization import EntryAuthority


class EntryCoordinator:
    def __init__(self, account):
        self.account = account
        self.preparation = EntryPreparation(account)
        self.ledger, self.db = account.ledger, account.ledger.db
        self.authority = EntryAuthority(account)

    def _source(self, source):
        self.preparation._leased()
        row = self.db.execute('SELECT * FROM live_signals WHERE source=?', (source,)).fetchone()
        try:
            health = self.db.execute('SELECT * FROM live_source_health WHERE id=1').fetchone()
            if (not row or row['state'] != 'PENDING' or not health or health['state'] != 'connected'
                    or not 0 <= (datetime.now(timezone.utc)-timestamp(datetime.fromisoformat(health['checked_at']))).total_seconds() <= 30):
                raise ValueError
        except (ValueError, TypeError, sqlite3.Error):
            raise Refused('Current connected signal source required') from None
        return json.loads(row['body'])['contract']

    def _market(self, evidence, *, started):
        try:
            at = timestamp(datetime.fromisoformat(evidence['observed_at']))
            if (evidence['ucc'] != self.account.session.ucc or evidence['segment'] != 'nse_fo'
                    or type(evidence['status_code']) is not int or evidence['status_code'] != 1
                    or evidence.get('market_unavailable') or evidence.get('order_submission_available') is not False
                    or at < started or not 0 <= (datetime.now(timezone.utc)-at).total_seconds() <= 5):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise Refused('Fresh broker NSE open-status notification required') from None

    def _evaluate(self, source):
        self.last_stage = 'signal-source'
        if self.db.in_transaction:
            raise Refused('Committed coordinator boundary required')
        a, p = self.account, self.preparation
        with a.store.lock(a.uid):
            contract = self._source(source)
        start, clock = datetime.now(timezone.utc), time.monotonic()
        # No account or SQLite lock is held across evidence collection.
        try:
            self.last_stage = 'market-status'
            market = a.session.request('market')
            self._market(market, started=start)
            self.last_stage = 'entry-quote'
            received = a.session.request('quotes', instruments=[dict(token=contract['token'],symbol=contract['symbol'])])
            if received.get('ucc') != a.session.ucc or len(received.get('quotes', [])) != 1:
                raise Refused('Complete account-bound entry quote required')
            q = received['quotes'][0]
            at = timestamp(datetime.fromisoformat(q['quoted_at']))
            observed = timestamp(datetime.fromisoformat(q['observed_at']))
            if ((q['token'],q['symbol'],q['segment']) != (contract['token'],contract['symbol'],'nse_fo')
                    or not start <= observed <= datetime.now(timezone.utc) or at > observed):
                raise Refused('Entry quote identity or collection time changed')
            quote = dict(segment='nse_fo',token=q['token'],bid=q['bid'],ask=q['ask'],quoted_at=at,market_open=True)
            self.last_stage = 'risk-marks'
            marks = collect_marks(self.ledger,a.session)
            self.last_stage = 'cash-risk-and-margin'
            report = funding_preflight(p.plans.router,a.session,source,accounting=p.accounting,
                                      cash=p.cash,marks=marks,**quote)
        except Refused:
            raise
        except Exception:
            raise Refused('Entry evidence unavailable; no order submitted') from None
        with a.store.lock(a.uid), self.ledger.transaction():
            self.last_stage = 'final-recheck'
            self._source(source)
            self._market(market,started=start)
            if time.monotonic()-clock > 5 or report['broker_reports_shortfall']:
                raise Refused('Entry evidence expired or broker funding shortfall')
            current = p.plans.router._quote_locked(source,accounting=p.accounting,cash=p.cash,marks=marks,**quote)
            if current != report['candidate']:
                raise Refused('Entry candidate changed after broker checks')
        return report, quote, marks, market, start, clock

    def evaluate(self, source):
        """Collect actual session evidence without reserving or submitting."""
        report, *_ = self._evaluate(source)
        return dict(report, market_basis='fresh-sfeed-open-notification', order_submission_available=False)

    def submit(self, source):
        """Gated activation path: fresh evaluation, atomic intent, one send only.

        There is no restart/resend API. Any durable DISPATCHING/SENDING/UNKNOWN
        outcome is retained for reconciliation. Release acceptance stays required.
        """
        if status.release_status()['order_submission_available'] is not True:
            raise Refused('Production entry activation is unavailable in this release')
        with self.account.store.lock(self.account.uid):
            self.authority.require()
        report, quote, marks, market, started, clock = self._evaluate(source)
        a, p = self.account, self.preparation
        with a.store.lock(a.uid):
            with self.ledger.transaction():
                self._source(source)
                self._market(market,started=started)
                if time.monotonic()-clock > 5 or status.release_status()['order_submission_available'] is not True:
                    raise Refused('Entry authorization expired')
                self.authority.require()
                value = p._prepare_locked(source,marks=marks,expected=report['candidate'],**quote)
                # Reconciliation covered all pre-existing state before reserve.
                # These are our own writes in that same transaction, not a new
                # broker snapshot. Never rewrite the reconciliation fingerprint.
                at = datetime.now(timezone.utc)
                self.db.execute("UPDATE intents SET status='DISPATCHING' WHERE tag=? AND status='PREPARED'", (value['tag'],))
                self.db.execute('INSERT INTO execution_attempts VALUES(?,?,?)',
                    (value['tag'],at.isoformat(),at.astimezone(IST).date().isoformat()))
                self.authority.bind(value['tag'])
            # Commit before network I/O. Keep account policy/pause serialized
            # until the bounded one-shot handoff has recorded its outcome.
            oid = a.monitor.commands.place('ENTRY',value['tag'])
            return dict(tag=value['tag'],broker_id=oid,state='ACKNOWLEDGED',fill_confirmed=False)

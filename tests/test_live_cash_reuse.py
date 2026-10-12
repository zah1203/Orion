"""Synthetic book/trade evidence only; no real broker cash certification."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
import unittest

import test_live_preparation as fixtures
from test_live_execution import report
from orion.live.cash import CONFIRMATION, REUSE_CONFIRMATION, REUSE_MODEL
from orion.live.ledger import Refused
from orion.live.routing import reviewed_policy


class CashReuseTests(unittest.TestCase):
    setUp = fixtures.PreparationTests.setUp
    tearDown = fixtures.PreparationTests.tearDown
    message = fixtures.PreparationTests.message
    account = fixtures.PreparationTests.account
    ready = fixtures.PreparationTests.ready
    prepare = fixtures.PreparationTests.prepare

    def balance(self, p):
        with p.ledger.transaction():
            return p.cash.balance(policy=reviewed_policy(self.store, p.account.uid, p.ucc), now=datetime.now(timezone.utc))

    def flat_trade(self, p, price='110'):
        tag = self.prepare(p)['tag']
        now = datetime.now(timezone.utc)
        day = now.astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
        p.db.execute("UPDATE intents SET status='FILLED',broker_id='buy1',filled=10,average='100' WHERE tag=?", (tag,))
        p.db.execute("INSERT INTO protective_exits VALUES('exit1',?,10,'0','FILLED','sell1',10)", (tag,))
        p.db.execute("INSERT INTO exit_terms VALUES('exit1',?)", (price,))
        p.db.execute("CREATE TABLE broker_fill_evidence(segment TEXT,trade_day TEXT,trade_id TEXT,observed_at TEXT,fee_status TEXT,PRIMARY KEY(segment,trade_day,trade_id))")
        for trade, oid, side, value, at in [('a','buy1','BUY','100',now-timedelta(milliseconds=2)), ('b','sell1','SELL',price,now-timedelta(milliseconds=1))]:
            p.db.execute("INSERT INTO fill_history VALUES('nse_fo',?,?,?,?,?,10,?,'0',?)", (day,trade,oid,tag,side,value,at.isoformat()))
            p.db.execute("INSERT INTO broker_fill_evidence VALUES('nse_fo',?,?,?,'unverified')", (day,trade,now.isoformat()))
        buy = dict(actId=p.ucc,nOrdNo='buy1',exSeg='nse_fo',prod='NRML',tok='123',trdSym='NIFTYTESTCE',qty=10,fldQty=10,ordSt='complete',trnsTp='B',avgPrc='100',prc='101',trgPrc='0',prcTp='L')
        sell = buy | dict(nOrdNo='sell1',trnsTp='S',avgPrc=price,prc=price)
        p.reconciliation.compare(report(buy,sell), report(), started_at=now, completed_at=now, complete=True)
        return tag

    def test_reuse_restores_principal_but_not_profit_and_keeps_fees(self):
        self.message()
        with self.account() as a, patch.object(fixtures, 'CONFIRMATION', REUSE_CONFIRMATION):
            p = self.ready(a)
            self.flat_trade(p)
            value = self.balance(p)
            self.assertEqual(value['model'], REUSE_MODEL)
            self.assertEqual(value['sale_credit'], '1010')
            self.assertEqual(value['remaining'], '29990')
            self.assertFalse(value['available_cash_verified'])
            self.assertEqual(self.balance(p), value)  # Reading twice never credits twice.

    def test_losses_reduce_recycled_capital(self):
        self.message()
        with self.account() as a, patch.object(fixtures, 'CONFIRMATION', REUSE_CONFIRMATION):
            p = self.ready(a)
            self.flat_trade(p, '90')
            self.assertEqual(self.balance(p)['remaining'], '29880')

    def test_legacy_review_never_silently_changes_model(self):
        with self.account() as a:
            p = self.ready(a)
            self.assertEqual(self.balance(p)['model'], 'opening-only')
            with self.assertRaises(Refused):
                p.cash.review(actor=a.uid,policy=reviewed_policy(self.store,a.uid,p.ucc),opening_cash='30000',confirmation=REUSE_CONFIRMATION)

    def test_incomplete_or_stale_evidence_cannot_credit_cash(self):
        self.message()
        with self.account() as a, patch.object(fixtures, 'CONFIRMATION', REUSE_CONFIRMATION):
            p = self.ready(a)
            self.flat_trade(p)
            p.db.execute("DELETE FROM broker_fill_evidence WHERE trade_id='b'")
            with self.assertRaises(Refused): self.balance(p)
            p.db.execute("UPDATE reconciliation_state SET checked_at='2000-01-01T00:00:00+00:00'")
            with self.assertRaises(Refused): self.balance(p)

    def test_cash_model_cannot_change_or_top_up_after_review(self):
        with self.account() as a, patch.object(fixtures, 'CONFIRMATION', REUSE_CONFIRMATION):
            p = self.ready(a)
            args = dict(actor=a.uid,policy=reviewed_policy(self.store,a.uid,p.ucc),opening_cash='30000',confirmation=REUSE_CONFIRMATION)
            self.assertTrue(p.cash.review(**args)['duplicate'])
            for change in (dict(confirmation=CONFIRMATION),dict(opening_cash='31000')):
                with self.assertRaises(Refused): p.cash.review(**(args|change))

    def test_portal_can_consume_unchanged_worker_snapshot_without_worker_identity(self):
        with self.account() as a, patch.object(fixtures, 'CONFIRMATION', REUSE_CONFIRMATION):
            p = self.ready(a)
            # Portal review uses another Ledger instance, but never gets an entry lease.
            p.ledger.session = 'portal-reader'
            self.assertEqual(self.balance(p)['remaining'], '30000')

    def test_cross_segment_or_missing_sell_history_is_rejected(self):
        self.message()
        with self.account() as a, patch.object(fixtures, 'CONFIRMATION', REUSE_CONFIRMATION):
            p = self.ready(a)
            self.flat_trade(p)
            p.db.execute("UPDATE fill_history SET segment='mcx_fo' WHERE side='SELL'")
            with self.assertRaises(Refused): self.balance(p)

    def test_pending_purchase_keeps_full_cash_reservation(self):
        self.message()
        with self.account() as a, patch.object(fixtures, 'CONFIRMATION', REUSE_CONFIRMATION):
            p = self.ready(a)
            self.prepare(p)
            now = datetime.now(timezone.utc)
            p.reconciliation.compare(report(), report(), started_at=now, completed_at=now, complete=True)
            value = self.balance(p)
            self.assertEqual(value['sale_credit'], '0')
            self.assertEqual(value['remaining'], '28980')

    def test_existing_schema_migrates_to_opening_only(self):
        from orion.live.cash import CashAllocation
        with self.account() as a:
            p = self.ready(a)
            p.db.execute('ALTER TABLE daily_cash_allocations DROP COLUMN model')
            CashAllocation(p.accounting)
            self.assertEqual(self.balance(p)['model'], 'opening-only')

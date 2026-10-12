from datetime import datetime, timedelta, timezone
import unittest

import test_live_preparation as preparation_fixtures
from test_live_execution import report
from orion.live.cash import CONFIRMATION
from orion.live.ledger import Refused
from orion.live.preparation import EntryPreparation
from orion.live.routing import reviewed_policy


class CashTests(unittest.TestCase):
    setUp = preparation_fixtures.PreparationTests.setUp
    tearDown = preparation_fixtures.PreparationTests.tearDown
    message = preparation_fixtures.PreparationTests.message
    account = preparation_fixtures.PreparationTests.account
    ready = preparation_fixtures.PreparationTests.ready
    prepare = preparation_fixtures.PreparationTests.prepare

    def balance(self, p, now=None, policy=None):
        with p.ledger.transaction():
            return p.cash.balance(policy=policy or reviewed_policy(self.store, p.account.uid, p.ucc),
                                  now=now or datetime.now(timezone.utc))

    def test_review_is_required_before_any_atomic_preparation(self):
        self.message()
        with self.account() as a:
            p = EntryPreparation(a)
            now = datetime.now(timezone.utc)
            p.reconciliation.compare(report(),report(),started_at=now,completed_at=now,complete=True)
            p.ledger.development_resume()
            with self.assertRaisesRegex(Refused, 'daily cash review'): self.prepare(p)
            self.assertFalse(p.db.execute('SELECT 1 FROM intents').fetchone())

    def test_pending_full_premium_reserved_and_abandon_releases_it(self):
        self.message()
        with self.account() as a:
            p = self.ready(a)
            before = self.balance(p)
            tag = self.prepare(p)['tag']
            pending = self.balance(p)
            self.assertEqual(pending['purchase_debits'], '1010')
            self.assertEqual(pending['fee_hold'], '10')
            self.assertEqual(pending['remaining'], '28980')
            self.assertFalse(pending['available_cash_verified'])
            p.abandon(tag)
            self.assertEqual(self.balance(p)['remaining'], before['remaining'])

    def test_filled_entries_and_profitable_sales_never_replenish_daily_cash(self):
        self.message()
        with self.account() as a:
            p = self.ready(a)
            tag = self.prepare(p)['tag']
            p.db.execute("UPDATE intents SET status='FILLED',filled=10,broker_id='broker1',average='100' WHERE tag=?", (tag,))
            # The funding model retains acquisition limits regardless of sale
            # proceeds. Full book/position consistency is independently checked
            # by Accounting and Reconciliation before any preparation.
            balance = self.balance(p)
            self.assertEqual(balance['remaining'], '28980')
            p.db.execute("INSERT INTO fill_history VALUES('nse_fo',?,'sale1','sell1',?,'SELL',10,'200','0',?)",
                         (balance['day'],tag,datetime.now(timezone.utc).isoformat()))
            self.assertEqual(self.balance(p)['remaining'], '28980')

    def test_partial_cancel_releases_only_unfilled_purchase_keeps_fees(self):
        self.message()
        with self.account() as a:
            p = self.ready(a)
            tag = self.prepare(p)['tag']
            p.db.execute("UPDATE intents SET status='PARTIAL',filled=4 WHERE tag=?", (tag,))
            self.assertEqual(self.balance(p)['purchase_debits'], '1010')
            p.db.execute("UPDATE intents SET status='CANCELLED' WHERE tag=?", (tag,))
            self.assertEqual(self.balance(p)['purchase_debits'], '404')
            self.assertEqual(self.balance(p)['fee_hold'], '10')
            day=self.balance(p)['day']
            p.db.execute("INSERT INTO fill_history VALUES('nse_fo',?,'buy1','broker1',?,'BUY',4,'100','30',?)",
                         (day,tag,datetime.now(timezone.utc).isoformat()))
            self.assertEqual(self.balance(p)['fee_hold'], '30')
            p.db.execute("INSERT INTO fee_corrections VALUES('note','nse_fo',?,'buy1','50',?)",
                         (day,datetime.now(timezone.utc).isoformat()))
            self.assertEqual(self.balance(p)['fee_hold'], '50')

    def test_other_accounts_cash_cannot_be_used_in_risk_assessment(self):
        from orion.live.accounting import Accounting
        from orion.live.cash import CashAllocation
        from orion.live.entry_risk import assess
        other=CashAllocation(Accounting(self.ledgers[self.other],'TESTSECOND'))
        with self.account() as a:
            p=self.ready(a)
            with p.ledger.transaction(), self.assertRaisesRegex(Refused,'Cash risk account mismatch'):
                assess(p.ledger,p.accounting,{},dict(account=a.uid),cash=other,
                       now=datetime.now(timezone.utc),marks={},policy={})

    def test_review_replay_cannot_top_up_reset_cross_account_or_change_policy(self):
        with self.account() as a:
            p = self.ready(a)
            policy = reviewed_policy(self.store,a.uid,p.ucc)
            args = dict(actor=a.uid,policy=policy,opening_cash='30000',confirmation=CONFIRMATION)
            self.assertTrue(p.cash.review(**args)['duplicate'])
            for change in (dict(opening_cash='29000'),dict(actor=self.other),dict(confirmation='yes'),
                           dict(policy=policy | dict(version=2))):
                with self.subTest(change=change), self.assertRaises(Refused): p.cash.review(**(args|change))
            with self.assertRaises(Refused): self.balance(p, policy=policy | dict(version=2))
            with self.assertRaises(Refused): self.balance(p, now=datetime.now(timezone.utc)+timedelta(days=1))
            self.assertEqual(p.db.execute('SELECT COUNT(*) FROM daily_cash_allocations').fetchone()[0],1)

    def test_cash_limit_blocks_whole_lot_without_partial_reservation(self):
        self.message()
        with self.account() as a:
            p = EntryPreparation(a)
            now = datetime.now(timezone.utc)
            p.reconciliation.compare(report(),report(),started_at=now,completed_at=now,complete=True)
            p.cash.review(actor=a.uid,policy=reviewed_policy(self.store,a.uid,p.ucc),opening_cash='100',confirmation=CONFIRMATION)
            now = datetime.now(timezone.utc)
            p.reconciliation.compare(report(),report(),started_at=now,completed_at=now,complete=True)
            p.ledger.development_resume()
            with self.assertRaisesRegex(Refused,'No whole lot'): self.prepare(p)
            self.assertFalse(p.db.execute('SELECT 1 FROM intents').fetchone())

    def test_review_requires_fresh_full_book_and_leaves_entries_paused(self):
        with self.account() as a:
            p = EntryPreparation(a)
            args = dict(actor=a.uid,policy=reviewed_policy(self.store,a.uid,p.ucc),opening_cash='1000',confirmation=CONFIRMATION)
            with self.assertRaises(Refused): p.cash.review(**args)
            now = datetime.now(timezone.utc)
            p.reconciliation.compare(report(),report(),started_at=now,completed_at=now,complete=True)
            p.cash.review(**args)
            self.assertEqual(p.db.execute('SELECT paused FROM control').fetchone()[0],1)
            p.db.execute("UPDATE reconciliation_state SET checked_at=?", ((now-timedelta(seconds=40)).isoformat(),))
            with self.assertRaises(Refused): p.ledger.development_resume()

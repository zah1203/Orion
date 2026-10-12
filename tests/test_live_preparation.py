from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_live_routing as routing
from test_live_execution import report
from orion.live.monitor import AccountMonitor
from orion.live.preparation import EntryPreparation
from orion.live.ledger import Refused
from orion.live.cash import CONFIRMATION
from orion.live.routing import reviewed_policy


class PreparationTests(unittest.TestCase):
    setUp = routing.RoutingTests.setUp
    tearDown = routing.RoutingTests.tearDown
    message = routing.RoutingTests.message

    def account(self, uid=None):
        uid = uid or self.owner
        ucc = 'TESTOWNER' if uid == self.owner else 'TESTSECOND'
        return AccountMonitor(self.store, uid, lambda:SimpleNamespace(ucc=ucc, close=lambda:None))

    def ready(self, account):
        preparation = EntryPreparation(account)
        now = datetime.now(timezone.utc)
        preparation.reconciliation.compare(report(), report(), started_at=now, completed_at=now, complete=True)
        preparation.cash.review(actor=account.uid, policy=reviewed_policy(self.store, account.uid, account.session.ucc),
            opening_cash='30000', confirmation=CONFIRMATION)
        now = datetime.now(timezone.utc)
        preparation.reconciliation.compare(report(), report(), started_at=now, completed_at=now, complete=True)
        preparation.ledger.development_resume()
        return preparation

    def prepare(self, preparation):
        return preparation.prepare(routing.CHANNEL+':1', marks={}, segment='nse_fo', token='123',
            bid='100', ask='101', quoted_at=datetime.now(timezone.utc), market_open=True)

    def test_atomic_reservation_and_exits_without_order_authority(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            result = self.prepare(p)
            self.assertFalse(result['order_submission_available'])
            self.assertEqual(result['quantity'], 10)
            self.assertEqual(account.ledger.get(result['tag'])['status'], 'PREPARED')
            self.assertEqual(p.db.execute('SELECT state FROM live_signals').fetchone()[0], 'RESERVED')
            self.assertEqual(p.db.execute('SELECT stop_limit FROM entry_exit_plans').fetchone()[0], '89.95')
            self.assertFalse(p.db.execute('SELECT 1 FROM broker_commands').fetchone())
            with self.assertRaises(Refused): account.monitor.commands.place('ENTRY', result['tag'])
            with self.assertRaises(Refused): self.prepare(p)

    def test_failed_binding_rolls_back_every_reservation_write(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            with patch.object(p.plans, '_bind_locked', side_effect=Refused('synthetic failure')):
                with self.assertRaises(Refused): self.prepare(p)
            for table in ('intents','execution_terms','reconciliation_instruments','fill_contracts','entry_exit_plans','protection_policy'):
                self.assertFalse(p.db.execute(f'SELECT 1 FROM {table}').fetchone(), table)
            self.assertEqual(p.db.execute('SELECT state FROM live_signals').fetchone()[0], 'PENDING')
            self.prepare(p)

    def test_accounts_size_independently_and_paper_unchanged(self):
        for uid, expected in ((self.owner,10),(self.other,20)):
            self.message(uid)
            path = self.store.account_dir(uid)/'paper.db'
            path.write_bytes(b'paper untouched')
            with self.account(uid) as account:
                self.assertEqual(self.prepare(self.ready(account))['quantity'], expected)
            self.assertEqual(path.read_bytes(), b'paper untouched')

    def test_paused_or_closed_account_cannot_prepare(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            account.ledger.pause()
            with self.assertRaises(Refused): self.prepare(p)
        with self.assertRaises(Refused): self.prepare(p)

    def test_abandon_keeps_deduplication_and_releases_only_unsent_units(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            value = self.prepare(p)
            p.abandon(value['tag'])
            self.assertEqual(account.ledger.reserved_units(account.ledger.get(value['tag'])), 0)
            self.assertEqual(p.db.execute('SELECT state FROM live_signals').fetchone()[0], 'INVALIDATED')
            now = datetime.now(timezone.utc)
            p.reconciliation.compare(report(), report(), started_at=now, completed_at=now, complete=True)
            with self.assertRaises(Refused): self.prepare(p)
            with self.assertRaises(Refused): account.ledger.mark_dispatching(value['tag'])

    def test_attempted_prepared_intent_cannot_be_abandoned(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            value = self.prepare(p)
            p.db.execute('INSERT INTO execution_attempts VALUES(?,?,?)', (value['tag'], self.now.isoformat(), self.now.date().isoformat()))
            with self.assertRaises(Refused): p.abandon(value['tag'])

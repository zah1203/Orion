from datetime import datetime, timedelta, timezone
import copy
import unittest
from unittest.mock import patch

import test_live_trades as trade_fixtures
from orion.live.accounting import IST
from orion.live.ledger import Refused
from orion.live.statements import CHARGES, review, unresolved


class StatementTests(unittest.TestCase):
    setUp = trade_fixtures.TradeTests.setUp
    tearDown = trade_fixtures.TradeTests.tearDown
    committed = trade_fixtures.TradeTests.committed
    books = trade_fixtures.TradeTests.books
    start = trade_fixtures.TradeTests.start

    def historical(self):
        snapshot = self.start()
        self.observer.ingest_evidence(snapshot)
        yesterday = self.now-timedelta(days=1)
        day = yesterday.astimezone(IST).date().isoformat()
        self.ledger.db.execute('UPDATE fill_history SET trade_day=?,executed_at=?',(day,yesterday.isoformat()))
        self.ledger.db.execute('UPDATE broker_fill_evidence SET trade_day=?',(day,))
        statement = dict(account=self.ledger.account,ucc='TESTUCC',trade_day=day,supersedes=None,
            charges={k:'2.50' if k=='gst' else '0' for k in CHARGES},
            fills=[dict(segment='nse_fo',trade_id='trade1',broker_id='broker1',side='BUY',quantity=10,price='100',total_fee='2.50')])
        return dict(actor=self.ledger.account, proof=b'synthetic private contract note',statement=statement,
                    confirmation='I REVIEWED THE BROKER STATEMENT')

    def test_complete_review_preserves_raw_evidence_and_append_only_fees(self):
        args = self.historical()
        original = dict(self.ledger.db.execute('SELECT * FROM broker_fill_evidence').fetchone())
        result = review(self.harness.accounting,**args)
        self.assertFalse(result['order_submission_available'])
        self.assertEqual(unresolved(self.ledger.db),[])
        self.assertEqual(dict(self.ledger.db.execute('SELECT * FROM broker_fill_evidence').fetchone()),original)
        self.assertEqual(self.ledger.db.execute('SELECT fee FROM fill_history').fetchone()[0],'0')
        self.assertEqual(self.ledger.db.execute('SELECT total_fee FROM fee_corrections').fetchone()[0],'2.5')
        self.assertTrue(review(self.harness.accounting,**args)['duplicate'])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM fee_corrections').fetchone()[0],1)
        self.harness.accounting.summary(now=datetime.now(timezone.utc), marks={self.tag:('100',self.now)})

    def test_missing_duplicate_external_or_mismatched_fills_never_clear_review(self):
        args = self.historical()
        baseline = args['statement']
        for change in (dict(fills=[]),dict(fills=baseline['fills']*2),
                       dict(fills=[baseline['fills'][0]|dict(quantity=9)]),
                       dict(fills=[baseline['fills'][0]|dict(price='99')]),
                       dict(fills=[baseline['fills'][0]|dict(trade_id=[])]),
                       dict(fills=[baseline['fills'][0]|dict(broker_id='outside')]),
                       dict(ucc='OTHER'),dict(account='0'*32),
                       dict(fills=[baseline['fills'][0]|dict(total_fee='2.49')])):
            with self.subTest(change=change), self.assertRaises(Refused):
                review(self.harness.accounting,**(args|dict(statement=baseline|change)))
            self.assertEqual(len(unresolved(self.ledger.db)),1)
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM fee_corrections').fetchone())

    def test_future_or_open_day_and_unreviewed_owner_refused(self):
        args = self.historical()
        for change in (dict(actor='0'*32),dict(proof=b''),dict(confirmation='yes'),
                       dict(statement=args['statement']|dict(trade_day=datetime.now(IST).date().isoformat()))):
            with self.assertRaises(Refused): review(self.harness.accounting,**(args|change))

    def test_later_correction_invalidates_review_until_explicit_complete_revision(self):
        args = self.historical()
        first = review(self.harness.accounting,**args)
        self.harness.accounting.correct_fee(account=self.ledger.account,ucc='TESTUCC',source_ref='later',
            segment='nse_fo',trade_day=args['statement']['trade_day'],trade_id='trade1',total_fee='3',
            reported_at=datetime.now(timezone.utc))
        self.assertEqual(len(unresolved(self.ledger.db)),1)
        review(self.harness.accounting,**args) # old replay never re-certifies
        self.assertEqual(len(unresolved(self.ledger.db)),1)
        revised = copy.deepcopy(args)
        revised['statement']['charges']['gst']='3'
        revised['statement']['fills'][0]['total_fee']='3'
        with self.assertRaises(Refused): review(self.harness.accounting,**revised)
        revised['statement']['supersedes']=first['review_id']
        review(self.harness.accounting,**revised)
        self.assertFalse(unresolved(self.ledger.db))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM fee_corrections').fetchone()[0],3)

    def test_new_fill_after_review_stays_unverified(self):
        args=self.historical()
        review(self.harness.accounting,**args)
        row=dict(self.ledger.db.execute('SELECT * FROM broker_fill_evidence').fetchone())
        row['trade_id']='late-fill'
        self.ledger.db.execute('INSERT INTO broker_fill_evidence VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        self.assertEqual(len(unresolved(self.ledger.db)),1)

    def test_failed_correction_rolls_back_entire_review(self):
        args=self.historical()
        original=self.harness.accounting.correct_fee
        def fail_after_write(**kwargs):
            original(**kwargs)
            raise Refused('synthetic failure')
        with patch.object(self.harness.accounting,'correct_fee',side_effect=fail_after_write):
            with self.assertRaises(Refused): review(self.harness.accounting,**args)
        for table in ('reviewed_statements','reviewed_statement_lines','fee_corrections'):
            self.assertFalse(self.ledger.db.execute(f'SELECT 1 FROM {table}').fetchone())
        self.assertEqual(len(unresolved(self.ledger.db)),1)

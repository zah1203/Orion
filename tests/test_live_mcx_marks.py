"""Offline segment identity checks; frozen economics binding tested separately."""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import test_live_marks as fixtures
from orion.live.marks import collect_marks
from orion.live.ledger import Refused
from orion.live.coordinator import EntryCoordinator


class CommodityMarksTests(unittest.TestCase):
    setUp = fixtures.MarkTests.setUp
    tearDown = fixtures.MarkTests.tearDown
    session_with_quotes = fixtures.MarkTests.session_with_quotes

    def test_mcx_mark_requires_frozen_binding_before_read(self):
        body=json.loads(self.ledger.get(self.tag)['body']); body['segment']='mcx_fo'
        self.ledger.db.execute('UPDATE intents SET body=? WHERE tag=?', (json.dumps(body), self.tag))
        self.session_with_quotes(segment='mcx_fo')
        with self.assertRaises(Refused): collect_marks(self.ledger,self.session)
        self.assertEqual(self.requests, [])

    def test_bound_mcx_mark_requests_and_checks_exact_segment(self):
        body=json.loads(self.ledger.get(self.tag)['body']); body['segment']='mcx_fo'
        self.ledger.db.execute('UPDATE intents SET body=? WHERE tag=?', (json.dumps(body), self.tag))
        response=self.session_with_quotes(segment='mcx_fo')
        with patch('orion.live.entry_plans.bound_quantity_multiplier', return_value=100) as bound:
            marks=collect_marks(self.ledger,self.session)
            bound.assert_called_once_with(self.ledger,self.tag)
            self.assertEqual(marks[self.tag][0], '105')
            self.assertEqual(self.requests, [dict(instruments=[dict(segment='mcx_fo', token='123',symbol='TESTCE')])])
            response['quotes'][0]['segment']='nse_fo'
            with self.assertRaises(Refused): collect_marks(self.ledger,self.session)

    def test_market_status_cannot_substitute_nse_for_mcx(self):
        from types import SimpleNamespace
        coordinator=object.__new__(EntryCoordinator)
        coordinator.account=SimpleNamespace(session=self.session)
        now=datetime.now(timezone.utc)
        status=dict(ucc=self.session.ucc, segment='nse_fo', status_code=1,
                    observed_at=now.isoformat(), order_submission_available=False)
        with self.assertRaises(Refused): coordinator._market(status, started=now, segment='mcx_fo')
        coordinator._market(status | dict(segment='mcx_fo'), started=now, segment='mcx_fo')

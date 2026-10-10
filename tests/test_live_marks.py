from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_live_strategy as fixtures
from orion.live.ledger import Refused
from orion.live.marks import collect_marks
from orion.live.monitor import AccountMonitor


class MarkTests(unittest.TestCase):
    setUp = fixtures.StrategyTests.setUp
    tearDown = fixtures.StrategyTests.tearDown

    def session_with_quotes(self, **changes):
        original = self.session.request
        now = datetime.now(timezone.utc).isoformat()
        quote = dict(token='123', symbol='TESTCE', segment='nse_fo', bid='105', ask='111',
                     quoted_at=now, observed_at=now)
        response = dict(ucc='TESTUCC', quotes=[quote | changes])
        self.requests = []
        def request(operation, **kwargs):
            if operation == 'quotes':
                self.requests.append(kwargs)
                return response
            return original(operation, **kwargs)
        self.session.request = request
        return response

    def test_uses_bound_bid_not_ask_or_last_trade(self):
        self.session_with_quotes()
        marks = collect_marks(self.ledger,self.session)
        self.assertEqual(marks[self.tag][0], '105')
        self.assertEqual(self.requests, [dict(instruments=[dict(token='123',symbol='TESTCE')])])
        self.strategy.cycle(marks=marks)
        self.assertEqual(self.strategy.cycle(marks=marks)['action'], 'none')
        self.assertEqual(len(self.session.sdk.stops), 1)

    def test_wrong_session_refused_before_quote_request(self):
        self.session_with_quotes()
        self.session.ucc = 'OTHER'
        with self.assertRaises(Refused): collect_marks(self.ledger,self.session)
        self.assertEqual(self.requests, [])

    def test_untrusted_response_identity_prices_and_timestamps_refused(self):
        now = datetime.now(timezone.utc)
        for change in (dict(token='999'),dict(symbol='OTHER'),dict(segment='mcx_fo'),dict(bid='112'),
                       dict(bid='105.01'),dict(bid='NaN'),dict(quoted_at=(now-timedelta(seconds=6)).isoformat()),
                       dict(observed_at=(now+timedelta(seconds=10)).isoformat())):
            with self.subTest(change=change):
                # Restore the original dispatcher before wrapping it again.
                self.session.request = fixtures.Session.request.__get__(self.session)
                self.session_with_quotes(**change)
                with self.assertRaises((Refused, ValueError)): collect_marks(self.ledger,self.session)
        response = self.session_with_quotes()
        response['ucc'] = 'OTHER'
        with self.assertRaises(Refused): collect_marks(self.ledger,self.session)

    def test_quote_aging_during_book_read_cannot_trigger_target(self):
        self.session_with_quotes(bid='111',ask='112')
        marks = collect_marks(self.ledger,self.session)
        self.strategy.cycle(marks={})  # Establish stop coverage first.
        future = datetime.now(timezone.utc)+timedelta(seconds=6)
        with patch('orion.live.strategy.datetime') as clock:
            clock.now.return_value = future
            self.assertEqual(self.strategy.cycle(marks=marks)['action'], 'quote-stale')
        self.assertEqual(self.ledger.db.execute('SELECT phase FROM strategy_plans').fetchone()[0], 'WATCH')

    def worker(self):
        worker = AccountMonitor(SimpleNamespace(lock=lambda _:nullcontext()), 'a'*32, None, strategy=True)
        worker.ledger, worker.session, worker.monitor = self.ledger,self.session,self.strategy
        return worker

    def test_leased_worker_uses_quotes_and_refuses_bad_identity_without_commands(self):
        response = self.session_with_quotes()
        worker = self.worker()
        self.assertEqual(worker.cycle_from_broker()['action'], 'strategy-protection-requested')
        response['ucc'] = 'OTHER'
        before = len(self.session.sdk.stops)
        with self.assertRaises(Refused): worker.cycle_from_broker()
        self.assertEqual(len(self.session.sdk.stops), before)
        self.assertEqual(worker.monitor.health()['action'], 'broker-quote-failed')

    def test_empty_exposure_skips_quote_request(self):
        self.session_with_quotes()
        self.ledger.db.execute('UPDATE intents SET filled=0')
        self.assertEqual(collect_marks(self.ledger,self.session), {})
        self.assertEqual(self.requests, [])

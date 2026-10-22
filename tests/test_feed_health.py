import unittest

from orion.feed_health import FeedHealth
import test_engine


class FeedHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.health = FeedHealth(5, clock=lambda: self.now)
        self.crude = ('mcx_fo', '1')
        self.silver = ('mcx_fo', '2')

    def test_silent_socket_recovers_without_a_first_quote(self):
        self.health.sync({self.crude}, {})
        self.now = 6
        self.assertEqual(self.health.overdue(), [self.crude])
        self.assertEqual(self.health.overdue(reconnect=True), [])
        self.now = 61
        self.health.sync({self.crude}, {})
        self.assertEqual(self.health.overdue(reconnect=True), [self.crude])

    def test_healthy_silver_does_not_hide_stalled_crude(self):
        self.health.sync({self.crude, self.silver}, {'mcx_fo': True})
        self.now = 61
        self.health.accepted(self.silver, [])
        self.assertEqual(self.health.overdue(reconnect=True), [self.crude])
        self.health.accepted(self.crude, [{'event': 'TARGET_REACHED'}])
        self.assertEqual(self.health.overdue(), [])

    def test_rejected_quotes_do_not_reset_stall_clock(self):
        self.health.sync({self.crude}, {})
        for event in ('OUT_OF_ORDER_QUOTE', 'INVALID_QUOTE', 'STALE_QUOTE'):
            self.now += 21
            self.assertFalse(self.health.accepted(self.crude, [{'event': event}]))
        self.assertEqual(self.health.overdue(reconnect=True), [self.crude])

    def test_closed_market_removal_and_reopening(self):
        self.health.sync({self.crude}, {'mcx_fo': True})
        self.now = 100
        self.health.sync({self.crude}, {'mcx_fo': False})
        self.assertEqual(self.health.overdue(reconnect=True), [])
        self.now = 10000
        self.health.sync({self.crude}, {'mcx_fo': True})
        self.assertEqual(self.health.overdue(), [])
        self.now += 61
        self.assertEqual(self.health.overdue(reconnect=True), [self.crude])
        self.health.sync(set(), {})
        self.assertEqual(self.health.overdue(), [])

    def test_segment_identity_and_fresh_subscription_grace(self):
        nse = ('nse_fo', '1')
        self.health.sync({self.crude}, {})
        self.now = 61
        self.health.sync({self.crude, nse}, {})
        self.health.accepted(nse, [])
        self.assertEqual(self.health.overdue(reconnect=True), [self.crude])


class AcceptedEngineQuoteTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp
    tearDown = test_engine.EngineTests.tearDown
    msg = test_engine.EngineTests.msg
    quote = test_engine.EngineTests.quote
    pos = test_engine.EngineTests.pos
    enter = test_engine.EngineTests.enter

    def test_rejected_target_quote_cannot_report_healthy_or_fake_a_fill(self):
        self.enter()
        now = [0]
        health = FeedHealth(5, clock=lambda: now[0])
        key = ('nse_fo', 'TEST1')
        health.sync({key}, {})
        original_time = self.t.isoformat()
        before = self.pos()
        now[0] = 61
        out = self.quote(180, source_time=original_time)
        self.assertEqual(out[0]['event'], 'OUT_OF_ORDER_QUOTE')
        self.assertFalse(health.accepted(key, out))
        self.assertEqual(health.overdue(reconnect=True), [key])
        self.assertEqual(self.pos(), before)
        out = self.quote(180)
        self.assertEqual(out[0]['event'], 'TARGET_REACHED')
        self.assertTrue(health.accepted(key, out))
        self.assertEqual(health.overdue(), [])
        self.assertEqual(self.pos()['stop'], self.pos()['entry_fill'])

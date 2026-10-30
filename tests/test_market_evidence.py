import unittest

from orion.market_evidence import MarketEvidence


class MarketEvidenceTests(unittest.TestCase):
    def test_missing_is_distinct_from_closed_and_other_segments(self):
        evidence = MarketEvidence()
        evidence.observe('mcx_fo', 1, 'a')
        fields, warning = evidence.quote('nse_fo')
        self.assertFalse(fields['market_status_known'])
        self.assertIsNone(fields['market_status_code'])
        self.assertEqual(warning['observed_status_segments'], ['mcx_fo'])
        self.assertIsNone(evidence.quote('nse_fo')[1])
        evidence.observe('nse_fo', 2, 'b')
        fields, warning = evidence.quote('nse_fo')
        self.assertTrue(fields['market_status_known'])
        self.assertEqual(fields['market_status_code'], 2)
        self.assertIsNone(warning)

    def test_transitions_and_reconnect_do_not_reuse_open_status(self):
        evidence = MarketEvidence()
        for code in range(1, 13):
            event = evidence.observe('nse_fo', code, 'a')
            self.assertEqual(event['market_open'], code in (1, 4))
            self.assertIsNone(evidence.observe('nse_fo', code, 'b'))
            self.assertEqual(evidence.quote('nse_fo')[0]['market_status_observed_at'], 'b')
        evidence.observe('nse_fo', 1, 'c')
        evidence.clear()
        fields, warning = evidence.quote('nse_fo')
        self.assertFalse(fields['market_status_known'])
        self.assertIsNotNone(warning)

    def test_nse_morning_sequence_preserves_open_after_auction_end(self):
        evidence = MarketEvidence()
        for code, expected in [(2, False), (0, False), (3, False), (4, True), (12, True)]:
            event = evidence.observe('nse_fo', code, str(code))
            self.assertEqual(event['market_open'], expected)
        fields, _ = evidence.quote('nse_fo')
        self.assertEqual(fields['market_status_code'], 12)
        self.assertTrue(fields['market_status_effective_open'])

    def test_auction_end_cannot_open_or_reopen_after_close_restriction_or_reset(self):
        for blocking_code in (0, 2, 3, 5, 6, 7, 8, 9, 10, 11, 99):
            evidence = MarketEvidence()
            evidence.observe('nse_fo', 1, 'a')
            evidence.observe('nse_fo', blocking_code, 'b')
            evidence.observe('nse_fo', 12, 'c')
            self.assertFalse(evidence.is_open('nse_fo'))
        evidence.observe('nse_fo', 1, 'd')
        evidence.clear()
        evidence.observe('nse_fo', 12, 'e')
        self.assertFalse(evidence.is_open('nse_fo'))

    def test_other_segments_are_unchanged_and_cannot_open_nse(self):
        evidence = MarketEvidence()
        for segment in ('mcx_fo', 'bse_fo', 'nse_cm'):
            evidence.observe(segment, 1, 'a')
            evidence.observe('nse_fo', 12, 'b')
            self.assertFalse(evidence.is_open('nse_fo'))
            evidence.observe(segment, 12, 'c')
            self.assertFalse(evidence.is_open(segment))

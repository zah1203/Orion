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

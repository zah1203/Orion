from datetime import datetime, timedelta, timezone
import unittest

import test_live_routing as routing
from test_live_execution import report
from orion.live.execution import ExecutionHarness, SimulatedBroker
from orion.live.ledger import Refused


class EntryRiskTests(unittest.TestCase):
    setUp = routing.RoutingTests.setUp
    tearDown = routing.RoutingTests.tearDown
    message = routing.RoutingTests.message
    quote = routing.RoutingTests.quote

    def prepare(self, uid=None):
        uid = uid or self.owner
        ucc = 'TESTOWNER' if uid == self.owner else 'TESTSECOND'
        harness = ExecutionHarness(self.ledgers[uid], ucc,
            self.pilot.status(uid)['limits'], SimulatedBroker(), enrolled=True, reviewed=True)
        self.message(uid)
        harness.check_book(report(), report(), datetime.now(timezone.utc))
        return harness

    def reserve(self, harness, **changes):
        harness.ledger.development_resume()
        now = datetime.now(timezone.utc)
        tag = harness.prepare_entry(**(dict(event='other-source',symbol='NIFTYTESTCE',segment='nse_fo',
            product='NRML',token='123',lots=1,lot_size=10,limit_price='101',stop='90',tick='.05',
            signal_time=now,quote_time=now,now=now) | changes))
        harness.check_book(report(), report(), datetime.now(timezone.utc))
        return tag

    def test_paused_assessment_has_no_authority_or_side_effects(self):
        h = self.prepare()
        before = list(h.ledger.db.iterdump())
        value = self.quote(accounting=h.accounting)
        self.assertEqual(value['lots'], 1)
        self.assertEqual(value['risk']['candidate_premium'], '1020')
        self.assertTrue(value['risk']['entries_paused'])
        self.assertIn('entries-paused', value['blockers'])
        self.assertNotIn('current-ledger-risk-and-reservations', value['blockers'])
        self.assertIn('verified-cash-and-charges', value['blockers'])
        self.assertFalse(value['order_submission_available'])
        self.assertFalse(value['risk']['available_cash_verified'])
        self.assertEqual(list(h.ledger.db.iterdump()), before)
        self.assertFalse(h.broker.orders)

    def test_pending_reservation_reduces_candidate_to_whole_lot(self):
        limits = routing.LIMITS | dict(max_lots=3,max_order_premium='4000',max_open_premium='2500')
        # Per-order limit must not exceed open limit.
        limits['max_order_premium'] = '2500'
        self.pilot.configure(self.owner,self.owner,limits)
        self.pilot.consent(self.owner,2)
        h = self.prepare()
        self.reserve(h)
        self.assertEqual(self.quote()['lots'], 2)
        value = self.quote(accounting=h.accounting)
        self.assertEqual(value['lots'], 1)
        self.assertEqual(value['risk']['reserved_premium'], '1020')
        self.assertEqual(value['risk']['counted_entries'], 1)

    def test_count_limit_includes_prepared_intents_without_attempts(self):
        h = self.prepare()
        for n in range(3): self.reserve(h,event='source'+str(n))
        with self.assertRaisesRegex(Refused,'entry capacity'):
            self.quote(accounting=h.accounting)

    def test_daily_loss_boundary_is_strict_and_risk_includes_pending(self):
        limits = routing.LIMITS | dict(daily_loss='240',max_open_risk='240',max_trade_loss='200')
        self.pilot.configure(self.owner,self.owner,limits)
        self.pilot.consent(self.owner,2)
        h = self.prepare()
        self.reserve(h)  # 120 stop risk; another 120 reaches daily limit.
        with self.assertRaisesRegex(Refused,'remaining ledger risk'):
            self.quote(accounting=h.accounting)

    def test_policy_withdrawal_and_changed_review_block_risk_assessment(self):
        h = self.prepare()
        self.pilot.configure(self.owner,self.owner,routing.LIMITS | dict(max_lots=2))
        self.pilot.consent(self.owner,2)
        with self.assertRaisesRegex(Refused,'policy or channel changed'):
            self.quote(accounting=h.accounting)
        self.pilot.revoke(self.owner,self.owner)
        with self.assertRaisesRegex(Refused,'reviewed account policy'):
            self.quote(accounting=h.accounting)

    def test_wrong_account_and_stale_reconciliation_refused(self):
        owner = self.prepare()
        other = self.prepare(self.other)
        with self.assertRaisesRegex(Refused,'account mismatch'):
            self.quote(accounting=other.accounting)
        owner.ledger.db.execute('UPDATE reconciliation_state SET checked_at=?',
            ((datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat(),))
        with self.assertRaisesRegex(Refused,'stale'):
            self.quote(accounting=owner.accounting)

    def test_duplicate_source_cannot_be_assessed_as_new_capacity(self):
        h = self.prepare()
        self.reserve(h,event=routing.CHANNEL+':1')
        with self.assertRaisesRegex(Refused,'already reserved'):
            self.quote(accounting=h.accounting)

    def test_unverified_broker_fees_cannot_be_treated_as_zero(self):
        h = self.prepare()
        h.ledger.db.execute('CREATE TABLE broker_fill_evidence(fee_status TEXT)')
        h.ledger.db.execute("INSERT INTO broker_fill_evidence VALUES('unverified')")
        h.check_book(report(),report(),datetime.now(timezone.utc))
        with self.assertRaisesRegex(Refused,'charges unverified'):
            self.quote(accounting=h.accounting)

    def test_unaccounted_reservation_and_unknown_order_refuse_assessment(self):
        h = self.prepare()
        tag = self.reserve(h)
        h.ledger.db.execute('DELETE FROM execution_terms WHERE tag=?',(tag,))
        h.check_book(report(),report(),datetime.now(timezone.utc))
        with self.assertRaisesRegex(Refused,'Unaccounted reservation'):
            self.quote(accounting=h.accounting)
        h.ledger.db.execute("UPDATE intents SET status='UNKNOWN' WHERE tag=?",(tag,))
        with self.assertRaises(Refused):
            self.quote(accounting=h.accounting)

    def test_other_account_reservations_do_not_consume_owner_budget(self):
        owner = self.prepare()
        other = self.prepare(self.other)
        self.reserve(other)
        value = self.quote(accounting=owner.accounting)
        self.assertEqual(value['risk']['reserved_premium'],'0')
        self.assertEqual(value['risk']['counted_entries'],0)

    def test_queued_previous_day_intent_still_consumes_daily_capacity(self):
        import json
        h = self.prepare()
        tags = [self.reserve(h,event='queued'+str(n)) for n in range(3)]
        for tag in tags:
            body=json.loads(h.ledger.get(tag)['body'])
            body['signal_time']=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
            h.ledger.db.execute('UPDATE intents SET body=? WHERE tag=?',(json.dumps(body),tag))
        h.check_book(report(),report(),datetime.now(timezone.utc))
        with self.assertRaisesRegex(Refused,'entry capacity'):
            self.quote(accounting=h.accounting)

    def test_missing_execution_schema_is_a_safe_refusal(self):
        from orion.live.accounting import Accounting
        accounting=Accounting(self.ledgers[self.owner],'TESTOWNER')
        self.message()
        with self.assertRaisesRegex(Refused,'Initialized execution risk ledger'):
            self.quote(accounting=accounting)

from datetime import datetime, timedelta, timezone
import copy
import unittest

import test_live_commands as fixtures
import test_live_observations as observation_fixtures
from test_live_execution import report
from orion.live.accounting import IST
from orion.live.kotak import TransportFailure
from orion.live.ledger import Refused
from orion.live.observations import Observations
from orion.live.reconciliation import require_current
from orion.live.trades import normalize_trades


def trade(now, **changes):
    local = now.astimezone(IST)
    return dict(actId='', nOrdNo='broker1', exSeg='nse_fo', prod='NRML', tok='',
                trdSym='TESTCE', trnsTp='B', rptTp='fill', multiplier='1', genDen='1',
                genNum='1', prcNum='1', prcDen='1', fldQty=10, lotSz='10', avgPrc='100',
                prc='', flId='trade1', flDt=local.strftime('%d-%b-%Y'),
                flTm=local.strftime('%H:%M:%S')) | changes


class TradeTests(unittest.TestCase):
    setUp = fixtures.CommandTests.setUp
    tearDown = fixtures.CommandTests.tearDown
    committed = fixtures.CommandTests.committed
    books = observation_fixtures.ObservationTests.books

    def start(self, filled=10):
        self.committed()
        self.commands.place('ENTRY', self.tag)
        sdk = self.session.sdk
        orders, positions = self.books(filled=filled, status='complete' if filled == 10 else 'open')
        sdk.order_report = lambda: copy.deepcopy(orders)
        sdk.positions = lambda: copy.deepcopy(positions)
        sdk.trade_report = lambda: report(trade(self.now, fldQty=filled))
        sdk.limits = lambda: dict(stat='Ok', stCode=200, Net='300000', sensitive='must-not-escape')
        self.observer = Observations(self.ledger, 'TESTUCC', self.commands)
        return self.session.adapter.evidence()

    def test_full_pipeline_is_idempotent_and_never_certifies_cash_or_fees(self):
        snapshot = self.start()
        self.assertEqual(self.observer.ingest_evidence(snapshot)['new_fills'], 1)
        self.assertEqual(self.observer.ingest_evidence(snapshot)['new_fills'], 0)
        require_current(self.ledger)
        self.assertEqual(self.ledger.get(self.tag)['status'], 'FILLED')
        self.assertFalse(snapshot['available_cash_verified'])
        self.assertFalse(snapshot['fees_verified'])
        self.assertNotIn('sensitive', str(snapshot))
        with self.assertRaisesRegex(Refused, 'charges unverified'):
            self.harness.accounting.summary(now=datetime.now(timezone.utc), marks={self.tag:('100', self.now)})
        # An offline fee correction cannot certify the unverified broker charges.
        self.harness.accounting.correct_fee(account=self.ledger.account, ucc='TESTUCC',
            source_ref='offline', segment='nse_fo', trade_day=self.now.astimezone(IST).date().isoformat(),
            trade_id='trade1', total_fee='2', reported_at=datetime.now(timezone.utc))
        with self.assertRaisesRegex(Refused, 'charges unverified'):
            self.harness.accounting.summary(now=datetime.now(timezone.utc), marks={self.tag:('100', self.now)})

    def test_missing_fill_rolls_back_order_and_command_confirmation(self):
        snapshot = self.start()
        snapshot['trades'] = []
        with self.assertRaises(Refused): self.observer.ingest_evidence(snapshot)
        self.assertEqual(self.ledger.get(self.tag)['status'], 'DISPATCHING')
        self.assertEqual(self.ledger.db.execute('SELECT status FROM broker_commands').fetchone()[0], 'ACKNOWLEDGED')
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM fill_history').fetchone())
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM reconciliation_state').fetchone())
        self.assertTrue(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())

    def test_partial_fill_exact_units_preserved(self):
        snapshot = self.start(filled=4)
        self.observer.ingest_evidence(snapshot)
        self.assertEqual(self.ledger.db.execute('SELECT quantity FROM fill_history').fetchone()[0], 4)
        self.assertEqual(self.ledger.get(self.tag)['filled'], 4)

    def test_invalid_second_trade_rolls_back_first(self):
        snapshot = self.start()
        snapshot['trades'][0]['quantity'] = 5
        snapshot['trades'].append(snapshot['trades'][0] | dict(trade_id='trade2', symbol='OTHER'))
        with self.assertRaises(Refused): self.observer.ingest_evidence(snapshot)
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM fill_history').fetchone())
        self.assertEqual(self.ledger.get(self.tag)['filled'], 0)

    def test_disappeared_changed_or_cross_account_evidence_blocks(self):
        snapshot = self.start()
        self.observer.ingest_evidence(snapshot)
        for changes in ({'trades':[]}, {'ucc':'OTHER'}, {'fees_verified':True},
                        {'trades':[snapshot['trades'][0] | {'price':'99'}]}):
            with self.subTest(changes=changes), self.assertRaises(Refused):
                self.observer.ingest_evidence(snapshot | changes)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM fill_history').fetchone()[0], 1)

    def test_bad_report_fields_fail_inside_session_without_raw_output(self):
        snapshot = self.start()
        orders = {r['broker_id']: r for r in snapshot['orders']}
        for change in (dict(actId='OTHER'), dict(tok='other'), dict(trdSym='OTHER'),
                       dict(nOrdNo='external'), dict(trnsTp='S'), dict(multiplier='2'),
                       dict(fldQty=True), dict(fldQty=11), dict(avgPrc='NaN'),
                       dict(avgPrc='99'), dict(prc='99'), dict(rptTp='cancel'),
                       dict(flDt='01-Jan-2000'), dict(flTm='bad'), dict(lotSz=0)):
            with self.subTest(change=change), self.assertRaises((Refused, ValueError)):
                normalize_trades(report(trade(self.now, **change)), 'TESTUCC', orders,
                                 completed_at=datetime.now(timezone.utc))
        with self.assertRaises(Refused):
            normalize_trades(report(trade(self.now), trade(self.now)), 'TESTUCC', orders,
                             completed_at=datetime.now(timezone.utc))
        self.session.sdk.trade_report = lambda: {'Error': 'sensitive-test-secret'}
        with self.assertRaises(TransportFailure) as error:
            self.session.adapter.evidence()
        self.assertNotIn('sensitive-test-secret', str(error.exception))

    def test_moving_books_do_not_produce_evidence(self):
        self.start()
        books, _ = self.books(filled=10, status='complete')
        responses = iter([books, report()])
        self.session.sdk.order_report = lambda: next(responses)
        with self.assertRaises(TransportFailure): self.session.adapter.evidence()

    def test_broker_lot_mismatch_and_stale_evidence_rejected(self):
        snapshot = self.start()
        for changes in ({'trades':[snapshot['trades'][0] | dict(lot_size=1)]},
                        {'completed_at':(self.now-timedelta(seconds=60)).isoformat()}):
            with self.assertRaises(Refused): self.observer.ingest_evidence(snapshot | changes)
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM fill_history').fetchone())

class EvidenceSDK:
    """Picklable factory using only a synthetic SDK, including child-process reads."""
    def __new__(cls, creds):
        from test_live_kotak import FakeSDK
        sdk = FakeSDK(creds)
        sdk.trade_report = sdk.order_report
        sdk.limits = lambda: dict(stat='Ok', stCode=200, Net='123')
        return sdk


class EvidenceProcessTests(unittest.TestCase):
    def test_bounded_process_can_collect_sanitized_evidence(self):
        from test_live_kotak import CREDS
        from orion.live.session import ProcessSession
        with ProcessSession(CREDS, '123456', _factory=EvidenceSDK) as session:
            value = session.request('evidence')
            self.assertEqual(value['trades'], [])
            self.assertEqual(value['rms_net'], '123')
            self.assertFalse(value['available_cash_verified'])
            self.assertFalse(value['fees_verified'])

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from orion.live.ledger import Ledger, Limits, Refused
from orion.live.protection import Protection
from orion.live.reconciliation import Reconciliation, normalize_orders, normalize_positions


def report(*rows):
    return dict(stat='Ok', stCode=200, data=list(rows))


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'live.db'
        self.account = 'a' * 32
        self.ledger = Ledger(self.path, self.account)
        self.ledger.development_resume()
        now = datetime.now(timezone.utc)
        self.args = dict(event='test', symbol='TESTCE', segment='nse_fo', lots=1,
            lot_size=10, limit_price='100', tick_size='.05', signal_time=now,
            quote_time=now, now=now, limits=Limits(2, 10, 10000, 500), realized_loss=0)
        self.tag = self.ledger.reserve(**self.args)['tag']
        self.ledger.mark_dispatching(self.tag)
        self.ledger.reconcile(self.tag, account=self.account, broker_id='entry1',
            symbol='TESTCE', quantity=10, status='FILLED', filled=10, average='99')
        self.audit = Reconciliation(self.ledger, 'TESTUCC')
        self.audit.bind_instrument(self.tag, product='NRML', token='123')
        self.order = dict(actId='TESTUCC', nOrdNo='entry1', exSeg='nse_fo', prod='NRML',
            tok='123', trdSym='TESTCE', qty=10, fldQty=10, ordSt='complete', trnsTp='B',
            avgPrc='99', prc='100', trgPrc='0', prcTp='L')
        self.position = dict(actId='TESTUCC', exSeg='nse_fo', prod='NRML', tok='123',
            trdSym='TESTCE', cfBuyQty='0', flBuyQty='10', cfSellQty='0', flSellQty='0')

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def compare(self, **changes):
        now = datetime.now(timezone.utc)
        return self.audit.compare(**(dict(orders=report(self.order), positions=report(self.position),
            started_at=now-timedelta(seconds=1), completed_at=now, complete=True) | changes))

    def assert_locked(self):
        with self.assertRaises(Refused):
            self.ledger.development_resume()
        self.assertEqual(self.ledger.db.execute('SELECT paused FROM control').fetchone()[0], 1)

    def test_match_does_not_change_orders_or_enable_live(self):
        before = self.ledger.get(self.tag)
        self.ledger.pause()
        result = self.compare()
        self.assertTrue(result['matched'])
        self.assertFalse(result['live_available'])
        self.assertEqual(self.ledger.get(self.tag), before)
        self.assertEqual(self.ledger.db.execute('SELECT paused FROM control').fetchone()[0], 1)

    def test_missing_order_does_not_infer_rejection(self):
        with self.assertRaises(Refused):
            self.compare(orders=report())
        self.assertEqual(self.ledger.get(self.tag)['status'], 'FILLED')
        self.assert_locked()
        self.compare()
        self.assert_locked()  # Subsequent match never clears an incident.

    def test_external_orders_and_positions_block(self):
        for changes in [dict(orders=report(self.order, self.order | dict(nOrdNo='external'))),
                        dict(positions=report(self.position, self.position | dict(tok='456'))),
                        dict(positions=report(self.position | dict(flBuyQty='9'))),
                        dict(positions=report(self.position | dict(flSellQty='20')))]:
            with self.subTest(changes=changes), self.assertRaises(Refused):
                self.compare(**changes)
            self.assert_locked()

    def test_identity_product_token_and_price_mismatch(self):
        for patch in [dict(actId='OTHER'), dict(prod='MIS'), dict(tok='999'),
                      dict(trnsTp='S'), dict(prc='101'), dict(avgPrc='98')]:
            with self.subTest(patch=patch), self.assertRaises(Refused):
                self.compare(orders=report(self.order | patch))
            self.assert_locked()

    def test_malformed_missing_duplicate_and_error_books(self):
        for bad in [dict(stat='Ok', stCode=200), {'Error': 'must never log'},
                    report(self.order, self.order), report(self.order | dict(qty=True)),
                    report(self.order | dict(avgPrc='NaN')), {1: 'malformed'},
                    report(self.order | dict(ordSt='new unknown state'))]:
            with self.subTest(bad=bad), self.assertRaises(Refused):
                self.compare(orders=bad)
            self.assert_locked()
        with self.assertRaises(Refused):
            self.compare(positions=report(self.position, self.position))

    def test_timestamps_and_completeness(self):
        now = datetime.now(timezone.utc)
        for patch in [dict(complete=False), dict(complete=1),
                      dict(completed_at=now-timedelta(seconds=31), started_at=now-timedelta(seconds=32)),
                      dict(completed_at=now+timedelta(seconds=10)),
                      dict(started_at=now-timedelta(seconds=21)),
                      dict(started_at=now.replace(tzinfo=None))]:
            with self.subTest(patch=patch), self.assertRaises(Refused):
                self.compare(**patch)
            self.assert_locked()

    def test_reopen_invalidates_successful_snapshot(self):
        self.compare()
        self.ledger.close()
        self.ledger = Ledger(self.path, self.account)
        with self.assertRaisesRegex(Refused, 'after open'):
            self.ledger.development_resume()
        self.audit = Reconciliation(self.ledger, 'TESTUCC')
        self.compare()
        self.ledger.development_resume()

    def test_expiry_and_local_mutation_invalidate_dispatch(self):
        self.compare()
        self.ledger.development_resume()
        prepared = self.ledger.reserve(**(self.args | dict(event='next')))['tag']
        with self.assertRaisesRegex(Refused, 'ledger changed'):
            self.ledger.mark_dispatching(prepared)
        self.audit.bind_instrument(prepared, product='NRML', token='123')
        self.compare()
        stale = (datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat()
        self.ledger.db.execute('UPDATE reconciliation_state SET checked_at=?', (stale,))
        with self.assertRaisesRegex(Refused, 'stale'):
            self.ledger.mark_dispatching(prepared)
        self.compare()
        self.ledger.mark_dispatching(prepared)

    def test_uncertain_order_cannot_be_rebound_from_snapshot(self):
        self.ledger.db.execute("UPDATE intents SET status='UNKNOWN',broker_id=NULL WHERE tag=?", (self.tag,))
        with self.assertRaises(Refused):
            self.compare()
        self.assertIsNone(self.ledger.get(self.tag)['broker_id'])
        self.assert_locked()

    def test_binding_is_immutable_and_account_scoped(self):
        with self.assertRaises(Refused):
            self.audit.bind_instrument(self.tag, product='MIS', token='123')
        with self.assertRaises(Refused):
            Reconciliation(self.ledger, 'OTHER')
        self.compare()

    def test_partial_sell_and_carry_forward_netting(self):
        protection = Protection(self.ledger)
        tag = protection.prepare(self.tag, trigger_price='90', tick_size='.05', lot_size=1)['tag']
        protection.dispatch(tag)
        protection.observe(tag, account=self.account, broker_id='exit1', symbol='TESTCE',
            segment='nse_fo', side='SELL', quantity=10, status='PARTIAL', filled=4)
        exit_order = self.order | dict(nOrdNo='exit1', trnsTp='S', fldQty=4, ordSt='open',
                                      avgPrc='90', prc='0', trgPrc='90', prcTp='SL-M')
        positions = report(self.position | dict(cfBuyQty='10', flBuyQty='0', flSellQty='4'))
        self.compare(orders=report(self.order, exit_order), positions=positions)
        with self.assertRaises(Refused):
            self.compare(orders=report(self.order, exit_order | dict(trgPrc='80')), positions=positions)
        protection.request_cancel(tag)
        protection.observe(tag, account=self.account, broker_id='exit1', symbol='TESTCE',
            segment='nse_fo', side='SELL', quantity=10, status='CANCELLED', filled=4)
        with self.assertRaises(Refused):
            protection.prepare(self.tag, trigger_price='90', tick_size='.05', lot_size=1)

    def test_parser_uses_raw_quantities_and_reports_partial_open(self):
        position = self.position | dict(netQty=999, cfSellQty='2', flSellQty='3')
        self.assertEqual(list(normalize_positions(report(position), 'TESTUCC').values()), [5])
        order = self.order | dict(ordSt='open', fldQty=3)
        self.assertEqual(normalize_orders(report(order), 'TESTUCC')['entry1']['status'], 'PARTIAL')

    def test_empty_books_are_valid_only_for_empty_ledger(self):
        self.ledger.close()
        self.ledger = Ledger(Path(self.tmp.name)/'empty'/'live.db', self.account)
        self.audit = Reconciliation(self.ledger, 'TESTUCC')
        self.compare(orders=report(), positions=report())
        self.ledger.development_resume()

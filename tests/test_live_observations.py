import unittest

import test_live_commands as fixtures
from test_live_execution import report
from orion.live.ledger import Refused
from orion.live.observations import Observations


class ObservationTests(unittest.TestCase):
    setUp = fixtures.CommandTests.setUp
    tearDown = fixtures.CommandTests.tearDown
    committed = fixtures.CommandTests.committed

    def books(self, *, filled=0, status='open'):
        order = dict(actId='TESTUCC', nOrdNo='broker1', exSeg='nse_fo', prod='NRML', tok='123',
            trdSym='TESTCE', qty=10, fldQty=filled, ordSt=status, trnsTp='B', avgPrc='100' if filled else '0',
            prc='100', trgPrc='0', prcTp='L')
        position = dict(actId='TESTUCC', exSeg='nse_fo', prod='NRML', tok='123', trdSym='TESTCE',
                        cfBuyQty=0, cfSellQty=0, flBuyQty=filled, flSellQty=0)
        return report(order), report(position) if filled else report()

    def start(self):
        self.committed()
        self.commands.place('ENTRY', self.tag)
        self.observer = Observations(self.ledger, 'TESTUCC', self.commands)

    def ingest(self, orders, positions):
        return self.observer.ingest(orders, positions, started_at=self.now, completed_at=self.now)

    def test_current_book_ingests_partial_fills_and_cancel_race(self):
        self.start()
        self.ingest(*self.books())
        self.commands.cancel('ENTRY', self.tag)
        self.ingest(*self.books(filled=4))
        status = self.ledger.db.execute("SELECT status FROM broker_commands WHERE operation='cancel'").fetchone()[0]
        self.assertEqual(status, 'ACKNOWLEDGED')
        self.assertEqual(self.ledger.get(self.tag)['filled'], 4)
        self.ingest(*self.books(filled=6, status='cancelled'))
        self.assertEqual(self.ledger.get(self.tag)['filled'], 6)
        self.assertEqual(self.ledger.db.execute("SELECT status FROM broker_commands WHERE operation='cancel'").fetchone()[0], 'CONFIRMED')

    def test_external_position_rolls_back_entire_batch_and_latches(self):
        self.start()
        orders, positions = self.books(filled=4)
        positions['data'][0]['flBuyQty'] = 5
        with self.assertRaises(Refused): self.ingest(orders, positions)
        self.assertEqual(self.ledger.get(self.tag)['status'], 'DISPATCHING')
        self.assertEqual(self.ledger.get(self.tag)['filled'], 0)
        self.assertTrue(self.ledger.db.execute("SELECT 1 FROM live_incidents WHERE code='broker-observation-conflict'").fetchone())
        with self.assertRaises(Refused): self.ledger.development_resume()

    def test_terminal_change_is_not_silently_applied(self):
        self.start()
        self.ingest(*self.books(filled=4, status='cancelled'))
        with self.assertRaises(Refused): self.ingest(*self.books(filled=5, status='cancelled'))
        self.assertEqual(self.ledger.get(self.tag)['filled'], 4)

    def test_missing_order_is_not_assumed_cancelled(self):
        self.start()
        with self.assertRaises(Refused): self.ingest(report(), report())
        self.assertEqual(self.ledger.get(self.tag)['status'], 'DISPATCHING')

    def test_stop_limit_terms_checked_before_confirming(self):
        self.start()
        self.ingest(*self.books(filled=10, status='complete'))
        protection = self.harness.protection
        tag = protection.prepare(self.tag, trigger_price='90', tick_size='.05', lot_size=10)['tag']
        protection.dispatch(tag)
        self.session.sdk.response['nOrdNo'] = 'broker2'
        self.commands.place('STOP', tag, stop_limit='89.95')
        orders, positions = self.books(filled=10, status='complete')
        stop = orders['data'][0] | dict(nOrdNo='broker2', trnsTp='S', fldQty=0, ordSt='trigger pending',
                                        avgPrc='0', prc='89.95', trgPrc='90', prcTp='SL')
        orders['data'].append(stop)
        self.ingest(orders, positions)
        self.assertEqual(protection.get(tag)['status'], 'OPEN')
        stop['prc'] = '80'
        with self.assertRaises(Refused): self.ingest(orders, positions)
        self.assertEqual(protection.get(tag)['status'], 'OPEN')

    def test_process_snapshot_round_trip_and_no_external_identity_import(self):
        self.start()
        orders, positions = self.books(filled=10, status='complete')
        self.session.sdk.order_report = lambda: orders
        self.session.sdk.positions = lambda: positions
        snapshot = self.session.adapter.snapshot()
        self.observer.ingest_snapshot(snapshot)
        self.assertEqual(self.ledger.get(self.tag)['filled'], 10)
        snapshot['orders'][0]['instrument'] = ('nse_fo', 'NRML', '999', 'TESTCE')
        with self.assertRaises(Refused): self.observer.ingest_snapshot(snapshot)

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orion.live.accounting import Accounting
from orion.live.history import seal
from orion.live.ledger import Ledger, Limits, Refused
from orion.live.reconciliation import Reconciliation
from test_live_reconciliation import report


class ClockMeta(type):
    def __instancecheck__(cls, value):
        return isinstance(value, datetime)


class Clock(metaclass=ClockMeta):
    value = None
    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz)
    fromisoformat = staticmethod(datetime.fromisoformat)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'live.db'
        self.ledger = Ledger(self.path, 'a'*32)
        self.now = datetime.now(timezone.utc)-timedelta(days=1, seconds=5)
        Clock.value = self.now
        self.clock = patch('orion.live.reconciliation.datetime', Clock)
        self.clock.start()
        self.ledger.development_resume()
        self.tag = self.ledger.reserve(event='old', symbol='TESTCE', segment='nse_fo', lots=1,
            lot_size=10, limit_price='100', tick_size='.05', signal_time=self.now,
            quote_time=self.now, now=self.now, limits=Limits(2,10,10000,500), realized_loss=0)['tag']
        self.ledger.mark_dispatching(self.tag)
        self.ledger.reconcile(self.tag, account='a'*32, broker_id='entry1', symbol='TESTCE',
            quantity=10, status='FILLED', filled=10, average='99')
        self.book = Accounting(self.ledger, 'TESTUCC')
        self.book.bind_multiplier(self.tag, 1)
        self.book.record(account='a'*32, ucc='TESTUCC', trade_id='fill1', broker_id='entry1',
            segment='nse_fo', symbol='TESTCE', side='BUY', quantity=10, price='99', fee='2', executed_at=self.now)
        self.audit = Reconciliation(self.ledger, 'TESTUCC')
        self.audit.bind_instrument(self.tag, product='NRML', token='123')
        self.order = dict(actId='TESTUCC', nOrdNo='entry1', exSeg='nse_fo', prod='NRML', tok='123',
            trdSym='TESTCE', qty=10, fldQty=10, ordSt='complete', trnsTp='B', avgPrc='99',
            prc='100', trgPrc='0', prcTp='L')
        self.position = dict(actId='TESTUCC', exSeg='nse_fo', prod='NRML', tok='123', trdSym='TESTCE',
                             cfBuyQty=10, cfSellQty=0, flBuyQty=0, flSellQty=0)

    def tearDown(self):
        self.clock.stop()
        self.ledger.close()
        self.temp.cleanup()

    def seal(self):
        return seal(self.ledger, 'TESTUCC', report(self.order), report(self.position),
                    started_at=self.now, completed_at=self.now, marks={self.tag:('99',self.now)})

    def compare(self, orders=None, positions=None):
        return self.audit.compare(report() if orders is None else orders,
            report(self.position) if positions is None else positions,
            started_at=Clock.value, completed_at=Clock.value, complete=True)

    def test_next_day_missing_terminal_order_uses_verified_evidence_after_restart(self):
        self.assertEqual(self.seal(), 1)
        self.assertEqual(self.seal(), 0)
        self.ledger.close()
        self.ledger = Ledger(self.path, 'a'*32)
        self.audit = Reconciliation(self.ledger, 'TESTUCC')
        Clock.value = self.now+timedelta(days=1)
        self.assertTrue(self.compare()['matched'])
        self.assertEqual(self.ledger.get(self.tag)['filled'], 10)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM terminal_history').fetchone()[0], 1)

    def test_same_day_missing_order_still_fails(self):
        self.seal()
        with self.assertRaises(Refused): self.compare()

    def test_current_position_must_still_match_carried_exposure(self):
        self.seal()
        Clock.value += timedelta(days=1)
        with self.assertRaises(Refused): self.compare(positions=report())

    def test_reappearing_changed_order_is_not_hidden_by_archive(self):
        self.seal()
        Clock.value += timedelta(days=1)
        with self.assertRaises(Refused): self.compare(orders=report(self.order | {'fldQty':9, 'ordSt':'cancelled'}))

    def test_missing_fills_cannot_be_sealed(self):
        self.ledger.db.execute('DELETE FROM fill_history')
        with self.assertRaises(Refused): self.seal()
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM terminal_history').fetchone())
        self.assertTrue(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())

    def test_tampered_fill_or_archive_body_fails_closed(self):
        self.seal()
        Clock.value += timedelta(days=1)
        self.ledger.db.execute("UPDATE terminal_history SET body=replace(body,'99','98')")
        with self.assertRaises(Refused): self.compare()

    def test_missing_working_order_never_archived(self):
        self.ledger.db.execute("UPDATE intents SET status='PARTIAL',filled=4")
        self.ledger.db.execute('UPDATE fill_history SET quantity=4')
        self.order.update(fldQty=4, ordSt='open')
        self.position['cfBuyQty'] = 4
        self.assertEqual(self.seal(), 0)
        Clock.value += timedelta(days=1)
        with self.assertRaises(Refused): self.compare()

    def test_wrong_broker_account_cannot_seal(self):
        self.order['actId'] = 'OTHER'
        with self.assertRaises(Refused): self.seal()
        self.assertFalse(self.ledger.db.execute('SELECT 1 FROM terminal_history').fetchone())

    def test_next_day_observation_ingestion_uses_same_history_rules(self):
        from orion.live.commands import Commands
        from orion.live.observations import Observations
        self.seal()
        commands = Commands(self.ledger, SimpleNamespace(ucc='TESTUCC'))
        observer = Observations(self.ledger, 'TESTUCC', commands)
        Clock.value += timedelta(days=1)
        with patch('orion.live.observations.datetime', Clock):
            result = observer.ingest(report(), report(self.position),
                                     started_at=Clock.value, completed_at=Clock.value)
        self.assertTrue(result['matched'])
        self.assertEqual(self.ledger.get(self.tag)['status'], 'FILLED')

    def test_changed_local_fill_invalidates_retained_evidence(self):
        self.seal()
        Clock.value += timedelta(days=1)
        self.ledger.db.execute("UPDATE fill_history SET price='98'")
        with self.assertRaises(Refused): self.compare()

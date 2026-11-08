"""Synthetic MCX execution economics only; no real broker or activation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
import unittest
from unittest.mock import Mock

from orion.live.accounting import IST
from orion.live.cash import CONFIRMATION, REUSE_MODEL
from orion.live.entry_plans import EntryPlans, bound_quantity_multiplier
from orion.live.entry_risk import candidate_quantity_multiplier
from orion.live.ledger import Limits, Refused
from orion.live.preparation import EntryPreparation
from orion.live.routing import reviewed_policy
from test_live_execution import report
import test_live_mcx_routing as commodity
import test_live_preparation as preparation


class CommodityExecutionTests(unittest.TestCase):
    base_setup = commodity.CommodityRoutingTests.setUp
    tearDown = commodity.CommodityRoutingTests.tearDown
    fixture = commodity.CommodityRoutingTests.fixture
    message = commodity.CommodityRoutingTests.message
    quote = commodity.CommodityRoutingTests.quote
    account = preparation.PreparationTests.account

    def setUp(self):
        self.base_setup()
        self.fixture()

    def ready(self, account):
        p = EntryPreparation(account)
        self.reconcile_empty(p)
        p.cash.review(actor=account.uid, policy=reviewed_policy(self.store, account.uid, p.ucc),
                      opening_cash='50000', confirmation=CONFIRMATION)
        self.reconcile_empty(p)
        p.ledger.development_resume()
        return p

    def reconcile_empty(self, p):
        now = datetime.now(timezone.utc)
        p.reconciliation.compare(report(), report(), started_at=now, completed_at=now, complete=True)

    def prepare(self, p, message_id=1):
        return p.prepare(commodity.CHANNEL+':'+str(message_id), marks={}, segment='mcx_fo',
                         token='123', bid='100', ask='101', quoted_at=datetime.now(timezone.utc), market_open=True)

    def balance(self, p):
        with p.ledger.transaction():
            return p.cash.balance(policy=reviewed_policy(self.store, p.account.uid, p.ucc),
                                  now=datetime.now(timezone.utc))

    def test_preparation_binds_exact_per_unit_money_and_no_buy_authority(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            candidate = p.plans.router.quote(commodity.CHANNEL+':1', segment='mcx_fo', token='123',
                bid='100', ask='101', quoted_at=datetime.now(timezone.utc), market_open=True,
                accounting=p.accounting, cash=p.cash, marks={})
            self.assertEqual(Decimal(candidate['risk']['candidate_premium']), Decimal('20210'))
            self.assertEqual(Decimal(candidate['risk']['candidate_stop_risk']), Decimal('2210'))
            value = self.prepare(p)
            body = json.loads(p.ledger.get(value['tag'])['body'])
            self.assertEqual(body['quantity'], 2)
            self.assertEqual(Decimal(body['premium']), Decimal('20200'))
            self.assertEqual(bound_quantity_multiplier(p.ledger, value['tag']), Decimal('100'))
            self.assertEqual(Decimal(p.db.execute('SELECT multiplier FROM fill_contracts').fetchone()[0]), Decimal('100'))
            cash = self.balance(p)
            self.assertEqual(Decimal(cash['purchase_debits']), Decimal('20200'))
            self.assertEqual(Decimal(cash['remaining']), Decimal('29790'))
            self.assertFalse(cash['available_cash_verified'])
            self.assertFalse(value['order_submission_available'])
            self.assertFalse(p.db.execute('SELECT 1 FROM broker_commands').fetchone())
            p.db.execute("UPDATE intents SET status='DISPATCHING' WHERE tag=?", (value['tag'],))
            account.session.request = Mock()
            with self.assertRaisesRegex(Refused, 'MCX BUY dispatch remains unavailable'):
                account.monitor.commands.place('ENTRY', value['tag'])
            account.session.request.assert_not_called()
            self.assertFalse(p.db.execute('SELECT 1 FROM broker_commands').fetchone())

    def test_per_lot_multiplier_is_converted_for_broker_quantity_ten(self):
        self.master['contracts'][0]['order_quantity_per_lot'] = 10
        self.master['economics']['products']['CRUDEOIL'].update(
            verified_lot_sizes=[10], premium_rupees_per_quantity_point='10')
        self.message()
        with self.account() as account:
            p = self.ready(account)
            value = self.prepare(p)
            self.assertEqual(value['quantity'], 20)
            self.assertEqual(bound_quantity_multiplier(p.ledger, value['tag']), Decimal('10'))
            self.assertEqual(Decimal(self.balance(p)['purchase_debits']), Decimal('20200'))

    def test_multiple_pending_entries_and_fees_consume_full_cash(self):
        self.message()
        self.message(message_id=2)
        self.message(message_id=3)
        with self.account() as account:
            p = self.ready(account)
            self.prepare(p)
            self.reconcile_empty(p)
            self.prepare(p, 2)
            self.reconcile_empty(p)
            cash = self.balance(p)
            self.assertEqual(Decimal(cash['purchase_debits']), Decimal('40400'))
            self.assertEqual(Decimal(cash['fee_hold']), Decimal('20'))
            self.assertEqual(Decimal(cash['remaining']), Decimal('9580'))
            with self.assertRaisesRegex(Refused, 'whole lot'):
                self.prepare(p, 3)
            self.assertEqual(p.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 2)

    def test_partial_working_entry_retains_pending_cash_then_terminal_releases_only_unfilled(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            p.db.execute("UPDATE intents SET status='PARTIAL',broker_id='synthetic-buy',filled=1,average='101'")
            self.assertEqual(p.ledger.reserved_units(p.ledger.get(tag)), 2)
            self.assertEqual(Decimal(self.balance(p)['purchase_debits']), Decimal('20200'))
            p.db.execute("UPDATE intents SET status='CANCELLED'")
            self.assertEqual(p.ledger.reserved_units(p.ledger.get(tag)), 1)
            self.assertEqual(Decimal(self.balance(p)['purchase_debits']), Decimal('10100'))
            self.assertEqual(Decimal(self.balance(p)['fee_hold']), Decimal('10'))
            p.db.execute("INSERT INTO protective_exits VALUES('synthetic-exit',?,1,'90','FILLED','synthetic-sell',1)", (tag,))
            self.assertEqual(p.ledger.reserved_units(p.ledger.get(tag)), 0)
            # Opening-only allocation conservatively retains spent principal.
            self.assertEqual(Decimal(self.balance(p)['purchase_debits']), Decimal('10100'))
            p.db.execute("UPDATE daily_cash_allocations SET model=?", (REUSE_MODEL,))
            with self.assertRaises(Refused): self.balance(p)

    def test_missing_or_changed_binding_never_falls_back_to_one(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            p.db.execute("UPDATE fill_contracts SET multiplier='1'")
            with self.assertRaises(Refused): bound_quantity_multiplier(p.ledger, tag)
            with self.assertRaises(Refused): self.balance(p)
            p.db.execute("UPDATE fill_contracts SET multiplier='100'")
            p.db.execute('DELETE FROM entry_contract_economics')
            with self.assertRaises(Refused): self.balance(p)

    def test_caller_cannot_choose_factor_or_disguise_mcx_as_legacy_nse(self):
        self.message()
        ledger = self.ledgers[self.owner]
        candidate = self.quote(segment='mcx_fo')
        for changes in (dict(quantity_multiplier='1'), dict(premium_multiplier='1'),
                        dict(source_digest='0'*64), dict(segment='nse_fo'), dict(stop='91'),
                        dict(lot_size=10), dict(token='999'), dict(targets=['111','121','131'])):
            with self.subTest(changes=changes), self.assertRaises(Refused):
                candidate_quantity_multiplier(ledger, candidate | changes, datetime.now(timezone.utc))
        candidate.pop('segment')
        with self.assertRaises(Refused):
            candidate_quantity_multiplier(ledger, candidate, datetime.now(timezone.utc))

    def test_ledger_reservation_requires_admitted_source_and_multiplied_budget(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            args = dict(event=commodity.CHANNEL+':1',symbol='CRUDETEST',segment='mcx_fo',lots=2,lot_size=1,
                limit_price='101',tick_size='.1',signal_time=self.now,quote_time=datetime.now(timezone.utc),
                now=datetime.now(timezone.utc),limits=Limits(2,3,'1000','10000'),realized_loss=0)
            with p.ledger.transaction(), self.assertRaisesRegex(Refused, 'Premium budget'):
                p.ledger.reserve(**args)
            with p.ledger.transaction(), self.assertRaisesRegex(Refused, 'source'):
                p.ledger.reserve(**(args | dict(event='missing',limits=Limits(2,3,'50000','10000'))))
            self.assertFalse(p.db.execute('SELECT 1 FROM intents').fetchone())

    def test_abandon_releases_unsent_premium_and_fee_but_keeps_identity(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            p.abandon(tag)
            cash = self.balance(p)
            self.assertEqual(Decimal(cash['remaining']), Decimal('50000'))
            self.assertEqual(bound_quantity_multiplier(p.ledger, tag), Decimal('100'))

    def test_failed_plan_binding_rolls_back_all_mcx_economics(self):
        from unittest.mock import patch
        self.message()
        with self.account() as account:
            p = self.ready(account)
            with patch.object(p.plans, '_bind_locked', side_effect=Refused('synthetic failure')):
                with self.assertRaises(Refused): self.prepare(p)
            for table in ('intents','execution_terms','fill_contracts','entry_contract_economics','entry_exit_plans'):
                self.assertFalse(p.db.execute('SELECT 1 FROM '+table).fetchone(), table)

    def test_protection_uses_bound_mcx_segment_and_survives_revoked_source(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            p.db.execute("UPDATE intents SET status='FILLED',broker_id='synthetic-buy',filled=2,average='101'")
            self.pilot.revoke(self.owner, self.owner)
            p.db.execute("UPDATE live_signals SET state='INVALIDATED'")
            self.assertEqual(bound_quantity_multiplier(p.ledger, tag), Decimal('100'))
            stop = account.monitor.protection.prepare(tag, trigger_price='90',tick_size='.1',lot_size=1)
            account.monitor.protection.dispatch(stop['tag'])
            account.session.request = Mock(return_value=dict(broker_id='synthetic-stop'))
            account.monitor.commands.place('STOP', stop['tag'], stop_limit='89.9')
            request = account.session.request.call_args.kwargs['request']
            self.assertEqual(request['segment'], 'mcx_fo')
            self.assertEqual(request['quantity'], 2)
            self.assertEqual(request['kind'], 'STOP')

    def test_protective_transport_refuses_mismatched_accounting_before_send(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            p.db.execute("UPDATE intents SET status='FILLED',broker_id='synthetic-buy',filled=2,average='101'")
            stop = account.monitor.protection.prepare(tag, trigger_price='90',tick_size='.1',lot_size=1)
            account.monitor.protection.dispatch(stop['tag'])
            p.db.execute("UPDATE fill_contracts SET multiplier='1'")
            account.session.request = Mock()
            with self.assertRaises(Refused):
                account.monitor.commands.place('STOP', stop['tag'], stop_limit='89.9')
            account.session.request.assert_not_called()
            self.assertFalse(p.db.execute('SELECT 1 FROM broker_commands').fetchone())

    def test_realized_unrealized_and_fee_money_use_exact_bound_factor(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            now = datetime.now(timezone.utc)
            p.db.execute("UPDATE intents SET status='FILLED',broker_id='synthetic-buy',filled=2,average='101'")
            p.db.execute("INSERT INTO protective_exits VALUES('synthetic-exit',?,1,'0','FILLED','synthetic-sell',1)", (tag,))
            for trade, oid, side, quantity, price, fee, offset in (
                    ('buy', 'synthetic-buy', 'BUY', 2, '101', '3', 2),
                    ('sell', 'synthetic-sell', 'SELL', 1, '110', '2', 1)):
                p.accounting.record(account=account.uid, ucc=p.ucc, trade_id=trade, broker_id=oid,
                    segment='mcx_fo', symbol='CRUDETEST', side=side, quantity=quantity,
                    price=price, fee=fee, executed_at=now-timedelta(seconds=offset))
            with localcontext() as ctx:
                ctx.prec = 3
                value = p.accounting.summary(now=now, marks={tag:('98', now)})
            for key, expected in dict(realized='900', unrealized_loss='300', fees='5',
                                      loss_used='305', open_premium='10100').items():
                self.assertEqual(Decimal(value[key]), Decimal(expected), key)
            # Even a flat position remains part of daily loss/evidence checks.
            p.db.execute("UPDATE protective_exits SET quantity=2,filled=2")
            p.accounting.record(account=account.uid, ucc=p.ucc, trade_id='sell2', broker_id='synthetic-sell',
                segment='mcx_fo', symbol='CRUDETEST', side='SELL', quantity=1, price='90', fee='2',
                executed_at=now-timedelta(milliseconds=100))
            self.assertFalse(p.accounting.summary(now=now, marks={})['open_quantities'])
            p.db.execute("UPDATE fill_contracts SET multiplier='1'")
            with self.assertRaisesRegex(Refused, 'MCX'):
                p.accounting.summary(now=now, marks={})

    def test_frozen_admission_time_retained_after_contract_date_and_policy_expire(self):
        from orion.live.commodity import validate_saved_contract
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            frozen = dict(p.db.execute('SELECT * FROM entry_contract_economics').fetchone())
            self.assertGreaterEqual(datetime.fromisoformat(frozen['validated_at']), self.now)
            with self.assertRaises(Refused):
                validate_saved_contract(json.loads(frozen['contract']), datetime.now(timezone.utc)+timedelta(days=3))
            self.pilot.revoke(self.owner, self.owner)
            p.db.execute("UPDATE live_signals SET state='INVALIDATED'")
            self.assertEqual(bound_quantity_multiplier(p.ledger, tag), Decimal('100'))
            self.assertEqual(dict(p.db.execute('SELECT * FROM entry_contract_economics').fetchone()), frozen)

    def test_duplicate_plan_binding_preserves_first_validation_and_fingerprint_tracks_evidence(self):
        from orion.live.reconciliation import fingerprint
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            frozen = dict(p.db.execute('SELECT * FROM entry_contract_economics').fetchone())
            p.db.execute("UPDATE live_signals SET state='PENDING'")
            p.plans.bind(tag)
            self.assertEqual(dict(p.db.execute('SELECT * FROM entry_contract_economics').fetchone()), frozen)
            before = fingerprint(p.ledger)
            p.db.execute("UPDATE entry_contract_economics SET quantity_multiplier='1'")
            self.assertNotEqual(fingerprint(p.ledger), before)
            with self.assertRaises(Refused): bound_quantity_multiplier(p.ledger, tag)

    def test_target_and_fallback_transport_keep_segment_and_total_quantity(self):
        policy = self.pilot.status(self.owner)['limits']
        self.pilot.configure(self.owner, self.owner, policy | dict(stop_gap_ticks=20, stop_fallback=True))
        self.pilot.consent(self.owner, 3)
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            p.db.execute("UPDATE intents SET status='FILLED',broker_id='synthetic-buy',filled=2,average='101'")
            target = account.monitor.protection.prepare_limit(tag, quantity=1, limit_price='110',tick_size='.1',lot_size=1)
            account.monitor.protection.dispatch(target['tag'])
            account.session.request = Mock(return_value=dict(broker_id='synthetic-target'))
            account.monitor.commands.place('EXIT', target['tag'])
            self.assertEqual(account.session.request.call_args.kwargs['request']['segment'], 'mcx_fo')
            # Synthetic observed target completion frees the residual unit.
            p.db.execute("UPDATE protective_exits SET status='FILLED',filled=1 WHERE tag=?", (target['tag'],))
            p.db.execute("UPDATE broker_commands SET status='CONFIRMED'")
            stop = account.monitor.protection.prepare(tag, trigger_price='90',tick_size='.1',lot_size=1)
            account.monitor.protection.dispatch(stop['tag'])
            account.session.request.return_value = dict(broker_id='synthetic-stop')
            account.monitor.commands.place('STOP', stop['tag'], stop_limit='88')
            p.db.execute("UPDATE protective_exits SET status='OPEN' WHERE tag=?", (stop['tag'],))
            p.db.execute("UPDATE broker_commands SET status='CONFIRMED'")
            now = datetime.now(timezone.utc)
            p.db.execute("INSERT INTO stop_observations VALUES(?,'triggered',?)", (stop['tag'], now.isoformat()))
            p.db.execute('INSERT INTO stop_fallback_state VALUES(?,?,?)',
                         (stop['tag'], (now-timedelta(seconds=6)).isoformat(), (now+timedelta(seconds=24)).isoformat()))
            account.monitor.commands.widen_stop(stop['tag'])
            request = account.session.request.call_args.kwargs['request']
            self.assertEqual(request['segment'], 'mcx_fo')
            self.assertEqual(request['quantity'], 1)
            self.assertEqual(Decimal(request['price']), Decimal('85'))

    def test_two_protected_positions_are_aggregated_with_bound_money(self):
        from orion.live.entry_plans import handoff
        from orion.live.entry_risk import assess
        policy = self.pilot.status(self.owner)['limits']
        self.pilot.configure(self.owner, self.owner, policy | dict(max_lots=1))
        self.pilot.consent(self.owner, 3)
        for message_id in (1, 2, 3): self.message(message_id=message_id)
        with self.account() as account:
            p = self.ready(account)
            tags = [self.prepare(p)['tag']]
            self.reconcile_empty(p)
            tags.append(self.prepare(p, 2)['tag'])
            now = datetime.now(timezone.utc)
            orders = []
            for n, tag in enumerate(tags):
                oid, sid = 'synthetic-buy'+str(n), 'synthetic-stop'+str(n)
                p.db.execute("UPDATE intents SET status='FILLED',broker_id=?,filled=1,average='101' WHERE tag=?", (oid, tag))
                p.accounting.record(account=account.uid, ucc=p.ucc, trade_id='trade'+str(n),broker_id=oid,
                    segment='mcx_fo', symbol='CRUDETEST',side='BUY',quantity=1,price='101',fee='3',
                    executed_at=now-timedelta(milliseconds=1))
                stop = account.monitor.protection.prepare(tag,trigger_price='90',tick_size='.1',lot_size=1)
                p.db.execute("UPDATE protective_exits SET status='OPEN',broker_id=? WHERE tag=?", (sid,stop['tag']))
                order = dict(actId=p.ucc,nOrdNo=oid,exSeg='mcx_fo',prod='NRML',tok='123',trdSym='CRUDETEST',
                    qty=1,fldQty=1,ordSt='complete',trnsTp='B',avgPrc='101',prc='101',trgPrc='0',prcTp='L')
                orders.extend((order, order | dict(nOrdNo=sid,fldQty=0,ordSt='trigger pending',trnsTp='S',
                    avgPrc='0',prc='89.9',trgPrc='90',prcTp='SL')))
            self.assertIsNotNone(handoff(account.monitor))
            position = dict(actId=p.ucc,exSeg='mcx_fo',prod='NRML',tok='123',trdSym='CRUDETEST',
                            cfBuyQty=0,flBuyQty=2,cfSellQty=0,flSellQty=0)
            now = datetime.now(timezone.utc)
            p.reconciliation.compare(report(*orders),report(position),started_at=now,completed_at=now,complete=True)
            marks = {tag:('101',datetime.now(timezone.utc)) for tag in tags}
            candidate = p.plans.router.quote(commodity.CHANNEL+':3',segment='mcx_fo',token='123',
                bid='100',ask='101',quoted_at=datetime.now(timezone.utc),market_open=True,
                accounting=p.accounting,marks=marks)
            self.assertEqual(Decimal(candidate['risk']['reserved_premium']),Decimal('20220'))
            self.assertEqual(Decimal(candidate['risk']['reserved_stop_risk']),Decimal('2220'))
            limits = self.pilot.status(self.owner)['limits'] | dict(max_open_premium='25000',max_order_premium='25000')
            with p.ledger.transaction(), self.assertRaisesRegex(Refused,'whole lot'):
                assess(p.ledger,p.accounting,limits,candidate,now=datetime.now(timezone.utc),marks=marks)

    def test_bound_mcx_evidence_cannot_be_relabelled_nse(self):
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            body = json.loads(p.ledger.get(tag)['body'])
            body['segment'] = 'nse_fo'
            p.db.execute('UPDATE intents SET body=? WHERE tag=?',(json.dumps(body),tag))
            with self.assertRaisesRegex(Refused,'Commodity evidence'):
                bound_quantity_multiplier(p.ledger,tag)
            with self.assertRaises(Refused): self.balance(p)

    def test_nse_simulation_harness_cannot_dispatch_a_prepared_mcx_tag(self):
        from orion.live.execution import ExecutionHarness, SimulatedBroker
        self.message()
        with self.account() as account:
            p = self.ready(account)
            tag = self.prepare(p)['tag']
            broker = SimulatedBroker()
            harness = ExecutionHarness(p.ledger,p.ucc,self.pilot.status(self.owner)['limits'],broker,
                                       enrolled=True,reviewed=True)
            now = datetime.now(timezone.utc)
            harness.check_book(report(),report(),now)
            with self.assertRaisesRegex(Refused,'Harness dispatch supports NSE'):
                harness.dispatch_entry(tag,now=now,quote_time=now,available_cash='50000')
            self.assertFalse(broker.orders)
            self.assertEqual(p.ledger.get(tag)['status'],'PREPARED')
            self.assertFalse(p.db.execute('SELECT 1 FROM execution_attempts').fetchone())


if __name__ == '__main__': unittest.main()

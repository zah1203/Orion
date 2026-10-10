import asyncio
import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from orion.live.readonly import ProbeFailure, probe, summarize
from orion.live.probe import run_probe


CREDS = dict(kotak_ucc='TEST1', kotak_mobile='+910000000000',
             kotak_mpin='private-pin', kotak_consumer_key='private-token')
ORDERS = dict(stat='Ok', stCode=200, data=[
    dict(actId='TEST1', nOrdNo='order1', qty='10', fldQty='3', ordSt='open')])
POSITIONS = dict(stat='ok', stCode=200, data=[
    dict(actId='TEST1', cfBuyQty='0', cfSellQty='0', flBuyQty='3', flSellQty='0')])
LIMITS = dict(stat='Ok', stCode=200, Net='1250.50', EntityId='TEST1')


class ReadOnlyTests(unittest.TestCase):
    def client(self):
        client = Mock(spec=['configuration','totp_login','totp_validate','order_report','positions','limits'])
        client.configuration = SimpleNamespace(ucc='TEST1')
        client.totp_login.return_value = {'data': dict(status='success', token='secret', sid='secret', ucc='TEST1')}
        client.totp_validate.return_value = {'data': dict(status='success', token='secret', sid='secret',
            ucc='TEST1', kType='Trade', baseUrl='https://neo-gw.kotaksecurities.com/route')}
        client.order_report.return_value = copy.deepcopy(ORDERS)
        client.positions.return_value = copy.deepcopy(POSITIONS)
        client.limits.return_value = copy.deepcopy(LIMITS)
        return client

    def test_only_auth_and_read_methods_and_redacted_summary(self):
        client = self.client()
        report = probe(client, CREDS, '123456')
        self.assertEqual([c[0] for c in client.method_calls],
                         ['totp_login','totp_validate','order_report','positions','limits'])
        self.assertEqual(report['working_order_count'], 1)
        self.assertEqual(report['nonzero_position_count'], 1)
        self.assertEqual(report['rms_net'], '1250.50')
        self.assertFalse(report['order_submission_available'])
        self.assertNotIn('TEST1', json.dumps(report))
        self.assertNotIn('secret', json.dumps(report))

    def test_sdk_error_dicts_and_missing_data_never_mean_empty(self):
        for response in ({'Error':'private'}, {'error':[]}, {'Error Message':'private'},
                         {'stat':'Ok','stCode':200}, {'stat':'Not_Ok','stCode':200,'data':[]}):
            with self.subTest(response=response), self.assertRaises(ProbeFailure):
                summarize(response, POSITIONS, LIMITS, 'TEST1')

    def test_empty_books_valid_but_not_live_readiness(self):
        report = summarize(dict(stat='Ok', stCode=200,data=[]),
                           dict(stat='ok',stCode=200,data=[]), LIMITS, 'TEST1')
        self.assertEqual(report['order_count'], 0)
        self.assertFalse(report['live_available'])

    def test_account_mismatch_and_unknown_schema(self):
        for key, value in [('actId','OTHER'), ('fldQty','11'), ('ordSt','unknown')]:
            orders = copy.deepcopy(ORDERS)
            orders['data'][0][key] = value
            with self.assertRaises(ProbeFailure):
                summarize(orders, POSITIONS, LIMITS, 'TEST1')
        bad = copy.deepcopy(POSITIONS)
        del bad['data'][0]['cfBuyQty']
        with self.assertRaises(ProbeFailure):
            summarize(ORDERS, bad, LIMITS, 'TEST1')

    def test_numeric_errors_and_negative_rms_net(self):
        for value in ('NaN','Infinity',True,'',None):
            with self.assertRaises(ProbeFailure):
                summarize(ORDERS, POSITIONS, LIMITS | dict(Net=value), 'TEST1')
        self.assertEqual(summarize(ORDERS, POSITIONS, LIMITS | dict(Net='-1'), 'TEST1')['rms_net'],'-1')

    def test_invalid_auth_or_route_prevents_reads(self):
        for change in [dict(ucc='OTHER'), dict(kType='View'), dict(baseUrl='https://evil.example/path'),
                       dict(baseUrl='https://neo-gw.kotaksecurities.com@evil.example')]:
            client = self.client()
            client.totp_validate.return_value['data'].update(change)
            with self.assertRaises(ProbeFailure):
                probe(client, CREDS, '123456')
            client.order_report.assert_not_called()

    def test_slow_snapshot_rejected(self):
        with patch('orion.live.readonly.time.monotonic', side_effect=[0,21]):
            with self.assertRaisesRegex(ProbeFailure, 'snapshot-too-slow'):
                probe(self.client(), CREDS, '123456')


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_stdin_and_minimal_environment(self):
        report = summarize(ORDERS, POSITIONS, LIMITS, 'TEST1')
        report['checked_at'] = datetime.now(timezone.utc).isoformat()
        child = Mock(returncode=0, communicate=AsyncMock(return_value=(json.dumps(report).encode(),None)),
                     wait=AsyncMock())
        with patch('orion.live.probe.asyncio.create_subprocess_exec', AsyncMock(return_value=child)) as spawn:
            result = await run_probe(CREDS,'123456')
        self.assertNotIn('private-token', str(spawn.call_args))
        self.assertNotIn('AWS_ACCESS_KEY_ID', spawn.call_args.kwargs['env'])
        self.assertIn(b'private-token', child.communicate.call_args.args[0])
        self.assertFalse(result['live_available'])
        child.wait.assert_awaited_once()

    async def test_timeout_kills_and_reaps_child(self):
        child = Mock(returncode=None, communicate=AsyncMock(side_effect=asyncio.TimeoutError),
                     wait=AsyncMock())
        with patch('orion.live.probe.asyncio.create_subprocess_exec', AsyncMock(return_value=child)):
            with self.assertRaisesRegex(ProbeFailure, 'timeout'):
                await run_probe(CREDS,'123456')
        child.kill.assert_called_once()
        child.wait.assert_awaited_once()

    async def test_child_errors_do_not_leak(self):
        child = Mock(returncode=1, communicate=AsyncMock(return_value=(b'{"error":"private-secret"}',None)),
                     wait=AsyncMock())
        with patch('orion.live.probe.asyncio.create_subprocess_exec', AsyncMock(return_value=child)):
            with self.assertRaisesRegex(ProbeFailure, '^probe-failed$'):
                await run_probe(CREDS,'123456')

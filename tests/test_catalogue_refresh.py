import asyncio
from datetime import datetime, timedelta
import fcntl
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_catalogue
from orion.catalogue import normalize
from orion.core import IST
from orion.portal.background import Background
from orion.portal.catalogue_refresh import refresh


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_catalogue.CatalogueTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.master = self.fixture.build([self.fixture.row(1, day=29)])
        self.master['as_of'] = '2026-09-21'
        self.master['economics']['verified_on'] = '2026-09-21'
        self.path = Path(self.fixture.tmp.name) / 'contracts.json'
        self.path.write_text(json.dumps(self.master))
        self.original = self.path.read_bytes()
        self.clock = patch('orion.portal.catalogue_refresh.clock', return_value=self.fixture.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def download(self, segment, output, username):
        self.assertEqual(username, 'alice')
        if segment == 'nse_fo':
            output.write_bytes(self.fixture.path.read_bytes())
            receipt = Path(str(self.fixture.path) + '.receipt.json').read_text()
        else:
            output.write_text('pSymbol,pSymbolName\n')
            import hashlib
            receipt = json.dumps({'as_of': '2026-09-22', 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()})
        Path(str(output) + '.receipt.json').write_text(receipt)

    def test_refresh_preserves_approval_and_downloads_once_per_day(self):
        with patch('orion.portal.catalogue_refresh.download', side_effect=self.download) as download:
            result = refresh(self.path, 'alice')
            self.assertEqual(result['catalogue_refresh'], 'Current')
            master = json.loads(self.path.read_text())
            self.assertEqual(master['as_of'], '2026-09-22')
            self.assertEqual(master['economics'], self.master['economics'])
            self.assertTrue(master['economics_reused'])
            self.assertEqual(master['contracts'], self.master['contracts'])
            refresh(self.path, 'bob')
            self.assertEqual(download.call_count, 2)
        self.assertFalse(list(self.path.parent.glob('catalogue-refresh-*')))

    def test_download_failure_preserves_previous_master_and_redacts_error(self):
        with patch('orion.portal.catalogue_refresh.download', side_effect=subprocess.TimeoutExpired('SECRET', 90)):
            result = refresh(self.path, 'alice')
        self.assertIn('Broker export failed', result['catalogue_refresh'])
        self.assertNotIn('SECRET', str(result))
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_economics_change_fails_without_replacing_master(self):
        content = self.fixture.path.read_text().replace(',65,65,', ',30,30,')
        self.fixture.path.write_text(content)
        import hashlib
        Path(str(self.fixture.path) + '.receipt.json').write_text(json.dumps({
            'as_of': '2026-09-22', 'sha256': hashlib.sha256(self.fixture.path.read_bytes()).hexdigest()}))
        with patch('orion.portal.catalogue_refresh.download', side_effect=self.download):
            result = refresh(self.path, 'alice')
        self.assertIn('Validation failed', result['catalogue_refresh'])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_shared_lease_prevents_competing_download(self):
        with self.path.with_suffix('.json.refresh.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('orion.portal.catalogue_refresh.download') as download:
                result = refresh(self.path, 'alice')
                download.assert_not_called()
        self.assertIn('Another worker', result['catalogue_refresh'])

    def test_rollover_during_download_cannot_publish(self):
        times = [self.fixture.now, self.fixture.now, self.fixture.now + timedelta(days=1)]
        with patch('orion.portal.catalogue_refresh.clock', side_effect=times), patch(
            'orion.portal.catalogue_refresh.download', side_effect=self.download
        ):
            result = refresh(self.path, 'alice')
        self.assertIn('Validation failed', result['catalogue_refresh'])
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_reuse_does_not_bypass_fresh_receipt_or_future_approval(self):
        profile = self.master['economics']
        paths = {'nse_fo': self.fixture.path}
        with self.assertRaises(ValueError):
            normalize(paths, profile, self.fixture.now)
        result = normalize(paths, profile, self.fixture.now, reuse_approved=True)
        self.assertEqual(result['economics']['verified_on'], '2026-09-21')
        profile['verified_on'] = '2026-09-23'
        with self.assertRaises(ValueError):
            normalize(paths, profile, self.fixture.now, reuse_approved=True)
        profile['verified_on'] = '2026-09-21'
        Path(str(self.fixture.path) + '.receipt.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'receipt'):
            normalize(paths, profile, self.fixture.now, reuse_approved=True)


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_retries_and_never_changes_user_permission(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        now = datetime(2026, 9, 22, 8, 29, tzinfo=IST)
        store = SimpleNamespace(health=Mock(), heartbeat=Mock(), user=Mock(return_value={'username': 'alice'}))
        bg = Background(store, 'uid', '/not-used')
        bg.catalogue = Mock(return_value=({}, False))
        statuses = []
        bg.status = lambda **kw: statuses.append(kw)
        moments = [now, now.replace(minute=30), now.replace(minute=31), now.replace(minute=35)]
        position = 0

        async def sleep(seconds):
            nonlocal position, now
            position += 1
            if position == len(moments):
                raise asyncio.CancelledError
            now = moments[position]

        with patch('orion.portal.background.datetime') as dt, patch(
            'orion.portal.background.asyncio.sleep', side_effect=sleep
        ), patch('orion.portal.background.refresh', return_value={'catalogue_refresh': 'Failed safely'}) as attempt:
            dt.now.side_effect = lambda zone: now
            with self.assertRaises(asyncio.CancelledError):
                await bg.refresh_catalogue()
            self.assertEqual(attempt.call_count, 2)
        self.assertEqual(statuses[0]['catalogue_refresh'], 'Scheduled for 08:30 IST')
        self.assertEqual(statuses[-1]['catalogue_refresh'], 'Failed safely')

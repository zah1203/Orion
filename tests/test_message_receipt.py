import json
import sqlite3
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from orion.message_receipt import record_and_process, failure


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE source_events(id TEXT PRIMARY KEY, received_at TEXT, body TEXT)')
        self.event = dict(event_id='tg:1:2', channel_id='1', message_id='2', text='example')
        self.now = datetime.now(timezone.utc)

    def call(self, refresh, process, channels=('1',)):
        return record_and_process(self.db, self.event, self.now, channels, refresh, process)

    def test_refresh_failure_preserves_committed_receipt_without_execution(self):
        process = Mock()
        with self.assertRaises(RuntimeError):
            self.call(Mock(side_effect=RuntimeError('private')), process)
        self.db.rollback()
        self.assertEqual(json.loads(self.db.execute('SELECT body FROM source_events').fetchone()[0]), self.event)
        process.assert_not_called()

    def test_processing_failure_preserves_receipt_without_retry(self):
        process = Mock(side_effect=ValueError('private'))
        with self.assertRaises(ValueError):
            self.call(Mock(), process)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_events').fetchone()[0], 1)
        process.assert_called_once_with(self.event, self.now)

    def test_unselected_channel_is_neither_recorded_nor_processed(self):
        refresh, process = Mock(), Mock()
        self.assertIsNone(self.call(refresh, process, channels=('other',)))
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_events').fetchone()[0], 0)
        refresh.assert_not_called()
        process.assert_not_called()

    def test_success_and_error_redaction(self):
        process = Mock(return_value=[{'event':'SIGNAL_PENDING'}])
        self.assertEqual(self.call(Mock(), process), [{'event':'SIGNAL_PENDING'}])
        event = failure(SimpleNamespace(chat_id=1, id=2), 'message', ValueError('secret-token'))
        self.assertNotIn('secret-token', json.dumps(event))
        self.assertEqual(event['error_type'], 'ValueError')

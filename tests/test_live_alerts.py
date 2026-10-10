from pathlib import Path
import tempfile
import unittest

from orion.live.alerts import AlertJournal, health_event
from orion.live.ledger import Ledger, Refused
from orion.live.status import account_status

class AlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.uid = 'a'*32
        self.path = Path(self.temp.name)/'accounts'/self.uid/'live.db'
        self.ledger = Ledger(self.path,self.uid)
        self.journal = AlertJournal(self.ledger,self.uid)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def incident(self):
        self.ledger.db.execute("INSERT OR IGNORE INTO live_incidents VALUES('broker-command-unknown')")

    def test_incident_queue_atomic_deduplicated_and_survives_restart(self):
        with self.assertRaises(RuntimeError), self.ledger.transaction():
            self.incident()
            raise RuntimeError
        self.assertIsNone(self.journal.claim())
        self.incident(); self.incident()
        self.ledger.close()
        self.ledger = Ledger(self.path,self.uid)
        self.journal = AlertJournal(self.ledger,self.uid)
        event = self.journal.claim()
        self.assertEqual(event['code'],'broker-command-unknown')
        self.assertIsNone(self.journal.claim())
        self.assertTrue(self.journal.finish(event['id'],event['claim'],delivered=True))
        self.assertFalse(self.journal.finish(event['id'],event['claim'],delivered=True))
        self.assertTrue(self.ledger.db.execute('SELECT 1 FROM live_incidents').fetchone())
        self.assertEqual(self.ledger.db.execute('SELECT paused FROM control').fetchone()[0],1)

    def test_unknown_delivery_and_crashed_claim_are_never_automatically_retried(self):
        self.incident(); event = self.journal.claim()
        self.journal.finish(event['id'],event['claim'],delivered=False)
        with self.assertRaises(Refused): self.journal.finish(event['id'],event['claim'],delivered=True)
        health_event(self.ledger,'review-required',10)
        self.journal.claim()  # Simulate crash before receipt.
        self.ledger.close(); self.ledger = Ledger(self.path,self.uid)
        self.journal = AlertJournal(self.ledger,self.uid)
        self.assertIsNone(self.journal.claim())
        status = account_status(self.temp.name,self.uid)
        self.assertEqual(status['uncertain_alerts'],2)
        self.assertEqual(status['pending_alerts'],0)

    def test_wrong_account_and_wrong_receipt_refused(self):
        with self.assertRaises(Refused): AlertJournal(self.ledger,'b'*32)
        self.incident(); event = self.journal.claim()
        with self.assertRaises(Refused): self.journal.finish(event['id'],'0'*32,delivered=True)
        with self.assertRaises(Refused): self.journal.finish(event['id'],event['claim'],delivered=1)

    def test_repeated_health_is_coalesced_but_new_episode_queued(self):
        health_event(self.ledger,'review-required',3)
        health_event(self.ledger,'review-required',3)
        health_event(self.ledger,'observed',0)
        health_event(self.ledger,'review-required',3)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM live_alerts').fetchone()[0],2)
        self.assertEqual(account_status(self.temp.name,self.uid)['pending_alerts'],2)

    def test_nested_claim_refused_and_two_connections_cannot_claim_same_event(self):
        self.incident()
        with self.ledger.transaction(), self.assertRaises(Refused): self.journal.claim()
        other = Ledger(self.path,self.uid)
        try:
            event = self.journal.claim()
            self.assertIsNone(AlertJournal(other,self.uid).claim())
            self.assertEqual(other.db.execute('SELECT claim FROM live_alerts').fetchone()[0],event['claim'])
        finally:
            other.close()

    def test_existing_incident_migration_and_payload_sanitization(self):
        self.ledger.db.execute('DROP TRIGGER live_incident_alert')
        self.ledger.db.execute("INSERT INTO live_incidents VALUES('sensitive/credential')")
        self.ledger.close(); self.ledger = Ledger(self.path,self.uid)
        event = AlertJournal(self.ledger,self.uid).claim()
        self.assertEqual(event['code'],'incident-requires-review')
        self.assertNotIn('sensitive',str(event))

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from orion.portal.app import create_app, defaults
from orion.portal.trade_details import details

SID = "-100123:1"


def fixture(path):
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE state(id INTEGER PRIMARY KEY,body TEXT);"
                         "CREATE TABLE audit(seq INTEGER PRIMARY KEY,at TEXT,event TEXT,body TEXT);"
                         "CREATE TABLE source_events(id TEXT PRIMARY KEY,received_at TEXT,body TEXT);")
        p = dict(contract=dict(symbol="GOLD-TEST"), entry_fill="100", entry_time="2026-10-27T09:00:00+00:00",
                 status="CLOSED", pnl="50", stop="100", targets=["110","120","130"], lots=2, remaining=0)
        db.execute("INSERT INTO state VALUES(1,?)", (json.dumps(dict(positions={SID:p})),))
        for i, (event, body) in enumerate([
            ("PAPER_BUY", dict(price="100", stop="90", pnl="-25")),
            ("TARGET_REACHED", dict(price="110", stop="100", pnl="75", target=1, remaining=1)),
            ("PAPER_EXIT", dict(price="100", reason="STOP", pnl="50")),
        ]):
            db.execute("INSERT INTO audit(at,event,body) VALUES(?,?,?)",
                       (f"2026-10-27T09:0{i}:00+00:00",event,json.dumps(dict(signal_id=SID,**body))))
        for mid, parent, channel, text in [(1,None,"-100123","original"),(2,1,"-100123","T1 hit"),
                                          (3,2,"-100123","follow-up"),(4,1,"-100999","other channel secret"),
                                          (5,None,"-100123","unlinked")]:
            e=dict(type="message",channel_id=channel,message_id=str(mid),reply_to=parent,text=text,
                   source_time="2026-10-27T09:00:00+00:00",edited=False)
            db.execute("INSERT INTO source_events VALUES(?,?,?)",(str(mid),"2026-10-27T09:01:00+00:00",json.dumps(e)))


class TradeDetailsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"paper.db"
        fixture(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stop_exit_preserves_profit_and_initial_stop(self):
        before = self.path.read_bytes()
        d = details(self.path,SID,{"-100123":{"name":"Channel A"}})
        self.assertEqual((d["outcome"],d["realized_pnl"],d["exit_reason"]),("Profit","50","STOP"))
        self.assertEqual((d["initial_stop"],d["current_stop"],d["exit_price"]),("90","100","100"))
        self.assertEqual(d["channel_name"],"Channel A")
        self.assertEqual(self.path.read_bytes(),before)

    def test_reply_chains_and_channel_isolation(self):
        d = details(self.path,SID,{})
        msgs = [e for e in d["timeline"] if e["kind"]=="telegram"]
        self.assertEqual({e["text"] for e in msgs},{"original","T1 hit","follow-up"})

    def test_edits_and_empty_media_text_preserved(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO source_events VALUES(?,?,?)",("edit","2026-10-27T09:03:00+00:00",json.dumps(dict(type="message",channel_id="-100123",message_id="1",reply_to=None,text="",edited=True))))
        d=details(self.path,SID,{})
        self.assertTrue(any(e.get("edited") and e["text"]=="" for e in d["timeline"]))

    def test_legacy_rejection_does_not_guess_reason(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO audit(at,event,body) VALUES(?,?,?)",("2026-10-27T09:03:00+00:00","ENTRY_LIMIT_REJECTED",json.dumps(dict(signal_id="-100123:8"))))
        d=details(self.path,"-100123:8",{})
        self.assertEqual(d["status"],"Not entered")
        self.assertTrue(any("did not record" in n for n in d["notes"]))
        self.assertIsNone(d["entry_price"])

    def test_pagination_and_missing_source_table(self):
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE source_events")
            db.executemany("INSERT INTO audit(at,event,body) VALUES(?,?,?)",[("2026-10-27T09:03:00+00:00","NEEDS_QUOTE",json.dumps(dict(signal_id=SID)))]*110)
        first=details(self.path,SID,{})
        second=details(self.path,SID,{},first["next_offset"])
        self.assertEqual((len(first["timeline"]),len(second["timeline"])),(100,13))
        self.assertIsNone(second["next_offset"])
        self.assertEqual(len({e["seq"] for e in first["timeline"]+second["timeline"]}),113)

    def test_missing_trade_and_database(self):
        with self.assertRaises(LookupError):details(self.path,"-100123:999",{})
        missing=self.path.parent/"absent.db"
        with self.assertRaises(LookupError):details(missing,SID,{})
        self.assertFalse(missing.exists())

    def test_routes_enforce_account_isolation_and_owner_access(self):
        with tempfile.TemporaryDirectory() as root:
            app=create_app(root,Fernet.generate_key(),"http://127.0.0.1:8000")
            store=app.state.store
            owner=store.create_user("owner","long-test-password",defaults());store.bootstrap_owner("owner")
            other=store.create_user("other","long-test-password",defaults())
            fixture(store.account_dir(owner)/"paper.db")
            with TestClient(app,base_url="http://127.0.0.1:8000") as c:
                def auth(name):
                    return {"Authorization":"Bearer "+c.post("/api/mobile/login",json=dict(username=name,password="long-test-password")).json()["token"]}
                h=auth("other")
                self.assertEqual(c.get("/api/paper-trade",params=dict(signal_id=SID),headers=h).status_code,404)
                self.assertEqual(c.get(f"/api/admin/users/{owner}/paper-trade",params=dict(signal_id=SID),headers=h).status_code,403)
                self.assertEqual(c.get("/api/paper-trade",params=dict(signal_id=SID)).status_code,401)
                h=auth("owner")
                self.assertEqual(c.get("/api/paper-trade",params=dict(signal_id=SID),headers=h).json()["outcome"],"Profit")
                self.assertEqual(c.get(f"/api/admin/users/{other}/paper-trade",params=dict(signal_id=SID),headers=h).status_code,404)
                self.assertEqual(c.get("/api/paper-trade",params=dict(signal_id=SID,offset=-1),headers=h).status_code,422)

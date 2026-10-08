import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from orion.core import Engine, stamp
from orion.portal.app import create_app, defaults
from orion.portal.performance import performance

NOW=datetime(2026,10,8,6,tzinfo=timezone.utc)

def position(product,pnl):
    return dict(contract=dict(product=product,symbol=product+'-TEST',expiry='2026-10-29',strike='100',option_type='CE',premium_multiplier='1',segment='test',token=product),lots=3,remaining=0,entry_fill='100',entry_time='2026-10-04T18:00:00+00:00',entry_date_ist='2026-10-04',status='CLOSED',pnl=str(pnl),stop='90',targets=['110','120','130'])

class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.state=dict(cash='200000',positions={'chan:1':position('NIFTY',225),'chan:2':position('GOLDM',-150)},days={'2026-10-04':dict(pnl='-25',entries=1),'2026-10-05':dict(pnl='75',entries=0),'2026-10-08':dict(pnl='25',entries=1)})
        self.records=[]
        for sid,day,event,delta,pnl,extras in [('chan:1','2026-10-04','PAPER_BUY',-25,-25,{}),('chan:1','2026-10-05','TARGET_REACHED',75,50,{'sold_lots':1,'remaining':2}),('chan:1','2026-10-08','PAPER_EXIT',175,225,{}),('chan:2','2026-10-08','PAPER_BUY',-25,-25,{}),('chan:2','2026-10-08','PAPER_EXIT',-125,-150,{})]:
            self.records.append(dict(at=day+'T06:00:00+00:00',event=event,body=dict(signal_id=sid,realized_delta=str(delta),pnl=str(pnl),pnl_date_ist=day,**extras)))
    def run_report(self,**kwargs):return performance(self.state,self.records,5,now=NOW,**kwargs)
    def test_week_month_and_instrument(self):
        r=self.run_report(period='week')
        self.assertEqual((r['start'],r['end'],r['realized']),('2026-10-05','2026-10-08','100'))
        self.assertEqual(self.run_report(period='month')['realized'],'75')
        self.assertEqual(self.run_report(period='week',instrument='NIFTY')['realized'],'250')
        self.assertEqual(self.run_report(period='month',instrument='NIFTY')['realized'],'225')
        self.assertEqual(self.run_report(period='week',instrument='GOLD')['realized'],'0')
        self.assertEqual(self.run_report(period='week',instrument='GOLDM')['realized'],'-150')
    def test_chart_grouping_reconciles(self):
        for group in ('day','week','month'):
            r=self.run_report(period='month',group_by=group)
            self.assertEqual(sum(Decimal(x['realized']) for x in r['chart']),Decimal(r['realized']))
            self.assertEqual(sum(Decimal(x['realized']) for x in r['instruments']),Decimal(r['realized']))
    def test_india_date_rollover(self):
        r=performance(self.state,self.records,5,period='today',now=datetime(2026,10,7,19,tzinfo=timezone.utc))
        self.assertEqual(r['start'],'2026-10-08')
        self.assertEqual(r['realized'],'25')
    def test_inclusive_custom_partial_exits_and_cash_adjustments(self):
        self.state['cash']='999999'
        r=self.run_report(period='custom',start='2026-10-05',end='2026-10-05',instrument='NIFTY')
        self.assertEqual(r['realized'],'75')
        self.assertEqual(r['trades'][0]['period_realized'],'75')
        self.assertEqual(r['entries'],0)
    def test_legacy_cross_day_does_not_guess_fees(self):
        for r in self.records:
            r['body'].pop('realized_delta');r['body'].pop('pnl_date_ist')
            if r['event']!='PAPER_EXIT':r['body'].pop('pnl')
        r=self.run_report(period='week',instrument='NIFTY')
        self.assertIsNone(r['realized']);self.assertTrue(r['incomplete_breakdown'])
        self.assertEqual(self.run_report(period='month',instrument='NIFTY',group_by='month')['chart'][0]['realized'],'225')
        self.assertEqual(self.run_report(period='week')['realized'],'100')
        self.assertEqual(self.run_report(period='week',instrument='GOLDM')['realized'],'-150')
    def test_upgrade_mid_trade_uses_checkpoints(self):
        for r in self.records[:2]:
            r['body'].pop('realized_delta');r['body'].pop('pnl')
        self.assertEqual(self.run_report(period='today',instrument='NIFTY')['realized'],'175')
        self.assertIsNone(self.run_report(period='week',instrument='NIFTY')['realized'])
        self.assertEqual(self.run_report(period='all',instrument='NIFTY')['realized'],'225')
    def test_missing_open_quote_separate_from_historical_pnl(self):
        self.state['positions']['chan:1']['remaining']=1
        self.state['positions']['chan:1']['status']='OPEN'
        r=self.run_report(period='today',instrument='NIFTY')
        self.assertEqual(r['realized'],'175')
        self.assertIsNone(r['current_open']['unrealized'])
        self.assertEqual(r['current_open']['mark_status'],'unavailable')
    def test_invalid_filters_and_empty_account(self):
        for kw in [dict(period='yesterday'),dict(instrument='GOLDFAKE'),dict(group_by='annual'),dict(period='custom',start='2026-10-09',end='2026-10-10'),dict(period='custom',start='2026-10-08',end='2026-10-07')]:
            with self.assertRaises(ValueError):self.run_report(**kw)
        r=performance({'cash':'100','days':{},'positions':{}},[],5,period='all',now=NOW)
        self.assertEqual((r['realized'],r['entries'],r['trades']),('0',0,[]))
    def test_engine_emits_exact_realized_deltas(self):
        root=Path(__file__).resolve().parents[1]
        engine=Engine(':memory:',json.loads((root/'config/paper.json').read_text()),json.loads((root/'examples/instruments.synthetic.json').read_text()),True)
        try:
            for line in (root/'examples/replay.jsonl').read_text().splitlines():
                e=json.loads(line);engine.process(e,stamp(e['source_time']))
            records=[dict(at=r[0],event=r[1],body=json.loads(r[2])) for r in engine.db.execute('SELECT at,event,body FROM audit ORDER BY seq')]
            realized=[r['body'] for r in records if r['event'] in ('PAPER_BUY','PAPER_EXIT','TARGET_REACHED')]
            self.assertTrue(all('realized_delta' in r and 'pnl_date_ist' in r for r in realized))
            self.assertEqual(sum(Decimal(r['realized_delta']) for r in realized),sum(Decimal(p['pnl']) for p in engine.state()['positions'].values()))
        finally:engine.db.close()
    def test_owner_route_and_account_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=create_app(tmp,Fernet.generate_key(),'http://127.0.0.1:8000');store=app.state.store
            owner=store.create_user('owner','long-test-password',defaults());store.bootstrap_owner('owner')
            other=store.create_user('other','long-test-password',defaults())
            with TestClient(app,base_url='http://127.0.0.1:8000') as c:
                def auth(name):return {'Authorization':'Bearer '+c.post('/api/mobile/login',json={'username':name,'password':'long-test-password'}).json()['token']}
                h=auth('other')
                self.assertEqual(c.get('/api/performance',headers=h).json()['realized'],'0')
                self.assertEqual(c.get(f'/api/admin/users/{owner}/performance',headers=h).status_code,403)
                self.assertEqual(c.get('/api/performance?instrument=INVALID',headers=h).status_code,422)
                self.assertEqual(c.get(f'/api/admin/users/{other}/performance',headers=auth('owner')).status_code,200)
                self.assertEqual(c.get('/api/performance').status_code,401)

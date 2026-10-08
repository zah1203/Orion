"""Period P&L uses realized ledger changes, never entry-date lifetime totals."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from ..core import IST, dec, stamp
from .reporting import report

PRODUCTS = ('BANKNIFTY','CRUDEOIL','CRUDEOILM','GOLD','GOLDM','NIFTY','SILVER','SILVERM')


def day_of(at):
    return stamp(at).astimezone(IST).date().isoformat()


def chunks_for(state, records, today):
    """Legacy checkpoints spanning dates stay indivisible, without guessing fees."""
    by_signal=defaultdict(list)
    for row in records:
        if row['event'] in ('PAPER_BUY','PAPER_EXIT','TARGET_REACHED'):
            by_signal[row['body'].get('signal_id')].append(row)
    chunks=[]; entries={}; exits={}
    for sid,p in state.get('positions',{}).items():
        if not p.get('entry_fill') or not p.get('lots'): continue
        product=p['contract']['product']; cumulative=Decimal(0); pending=[]; emitted=False
        chunk_start=len(chunks)
        def add(days,delta):
            if days: chunks.append(dict(signal_id=sid,product=product,start=min(days),end=max(days),delta=delta))
        for r in by_signal[sid]:
            b=r['body'];day=b.get('pnl_date_ist') or day_of(r['at'])
            if r['event']=='PAPER_BUY': entries[sid]=day
            if r['event']=='PAPER_EXIT' or (r['event']=='TARGET_REACHED' and b.get('remaining')==0): exits[sid]=day
            if 'realized_delta' in b:
                delta=dec(b['realized_delta']); after=dec(b['pnl'])
                if pending:
                    add(pending,after-delta-cumulative);pending=[]
                add([day],delta);cumulative=after;emitted=True
            elif r['event']=='TARGET_REACHED' and not b.get('sold_lots'):
                continue
            else:
                pending.append(day)
                if 'pnl' in b:
                    after=dec(b['pnl']);add(pending,after-cumulative);pending=[];cumulative=after;emitted=True
        if pending:
            add(pending,dec(p['pnl'])-cumulative);emitted=True
        if not emitted:
            first=p.get('entry_date_ist') or (day_of(p['entry_time']) if p.get('entry_time') else today)
            add([first,today],dec(p['pnl']))
        residual=dec(p['pnl'])-sum((c['delta'] for c in chunks[chunk_start:]),Decimal(0))
        if residual:
            first=p.get('entry_date_ist') or (day_of(p['entry_time']) if p.get('entry_time') else today)
            add([first,today],residual)
        entries.setdefault(sid,p.get('entry_date_ist') or (day_of(p['entry_time']) if p.get('entry_time') else ''))
    return chunks,entries,exits


def performance(state, records, quote_age, period='week', instrument='ALL', start=None, end=None, group_by='day', now=None):
    now=now or datetime.now(timezone.utc); today=now.astimezone(IST).date()
    if instrument!='ALL' and instrument not in PRODUCTS: raise ValueError('Unknown instrument')
    if group_by not in ('day','week','month'):raise ValueError('Unknown chart grouping')
    chunks,entries,exits=chunks_for(state,records,today.isoformat())
    if period=='today':first=last=today
    elif period=='week':first=today-timedelta(days=today.weekday());last=today
    elif period=='month':first=today.replace(day=1);last=today
    elif period=='all':
        dates=[c['start'] for c in chunks]+list(state.get('days',{}))
        first=date.fromisoformat(min(dates)) if dates else today;last=today
    elif period=='custom':
        if not start or not end:raise ValueError('Choose both start and end dates')
        first=date.fromisoformat(start);last=date.fromisoformat(end)
        if first>last or last>today:raise ValueError('Choose ordered dates ending no later than today in India')
        if (last-first).days>3660:raise ValueError('Choose at most ten years')
    else:raise ValueError('Unknown period')
    lo,hi=first.isoformat(),last.isoformat()
    selected=[c for c in chunks if (instrument=='ALL' or c['product']==instrument)]
    def value(items,a,b):
        total=Decimal(0);partial=False
        for c in items:
            if c['end']<a or c['start']>b:continue
            if a<=c['start'] and c['end']<=b:total+=c['delta']
            else:partial=True
        return None if partial else str(total)
    def entry_count(product,a,b):
        return sum(a<=d<=b and (product=='ALL' or state['positions'][sid]['contract']['product']==product) for sid,d in entries.items())
    # The engine's daily ledger is authoritative for unfiltered historical totals.
    daily=[];d=first
    while d<=last:
        ds=d.isoformat();day=state.get('days',{}).get(ds,{})
        pnl=str(day.get('pnl','0')) if instrument=='ALL' else value(selected,ds,ds)
        count=day.get('entries',0) if instrument=='ALL' else entry_count(instrument,ds,ds)
        daily.append(dict(date=ds,realized=pnl,entries=count));d+=timedelta(days=1)
    realized=str(sum((dec(x['realized']) for x in daily),Decimal(0))) if instrument=='ALL' else value(selected,lo,hi)
    buckets={}; bounds={}
    for x in daily:
        d=date.fromisoformat(x['date'])
        key=(d-timedelta(days=d.weekday())).isoformat() if group_by=='week' else d.replace(day=1).isoformat() if group_by=='month' else x['date']
        bounds.setdefault(key,[x['date'],x['date']])[1]=x['date']
        g=buckets.setdefault(key,dict(date=key,realized=Decimal(0),entries=0))
        g['realized']=None if g['realized'] is None or x['realized'] is None else g['realized']+dec(x['realized'])
        g['entries']+=x['entries']
    # For a selected instrument, recompute a whole bucket from checkpoints: a
    # monthly total can be exact even when a legacy cross-day daily split is not.
    for key,g in buckets.items():
        if instrument!='ALL':
            g['realized']=value(selected,*bounds[key])
        elif g['realized'] is not None:g['realized']=str(g['realized'])
    positions=report(state,quote_age,now)['positions']
    relevant=[p for p in positions if instrument=='ALL' or p['product']==instrument]
    current=[p for p in relevant if p['remaining']]
    open_pnl=None if any(p['unrealized'] is None for p in current) else str(sum((dec(p['unrealized']) for p in current),Decimal(0)))
    trades=[]
    for p in relevant:
        own=[c for c in selected if c['signal_id']==p['signal_id']]
        if any(c['start']<=hi and c['end']>=lo for c in own):
            trades.append({**p,'period_realized':value(own,lo,hi),'closed_date':exits.get(p['signal_id'])})
    breakdown=[]
    for product in sorted({c['product'] for c in selected}):
        own=[c for c in selected if c['product']==product]
        if any(c['start']<=hi and c['end']>=lo for c in own):
            breakdown.append(dict(product=product,realized=value(own,lo,hi),entries=entry_count(product,lo,hi)))
    return dict(period=period,start=lo,end=hi,timezone='Asia/Kolkata',instrument=instrument,group_by=group_by,
        realized=realized,entries=sum(x['entries'] for x in daily),daily=daily,chart=list(buckets.values()),
        instruments=breakdown,available_instruments=list(PRODUCTS),trades=trades,
        current_open=dict(unrealized=open_pnl,positions=len(current),mark_status='unavailable' if open_pnl is None else 'stale' if any(p['mark_status']=='stale' for p in current) else 'fresh',as_of=now.isoformat()),
        incomplete_breakdown=any(x['realized'] is None for x in breakdown) or any(x['realized'] is None for x in buckets.values()),
        note='Realized P&L includes charged paper fees on their ledger date in India. Balance adjustments are excluded. Open P&L is a current estimate, not a return for the selected dates. Some older cross-day instrument splits are unavailable because historical event-level fees were not recorded.')

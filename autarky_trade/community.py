"""Persistent multi-agent trading lab. All capital and fills are simulated."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
import math
from pathlib import Path
import sqlite3
import time

from .signals import filter_schema
from .research import assess
from .core import Account, Costs, Strategy, backtest, kraken, quote, load_csv, synthetic, validate, request

AGENTS = {'alpha': 'trend', 'beta': 'reversion', 'gamma': 'breakout'}
MIN_TEST = 120
MIN_HISTORY = 600
MAX_DRAWDOWN = .08
COSTS = Costs()


def encode(x):
    return json.dumps(x, sort_keys=True, allow_nan=False)


@contextmanager
def db(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    try:
        yield c
    finally:
        c.close()


def init(path, pair='XBTUSD', interval=60, capital=1000.):
    if pair not in ('XBTUSD', 'ETHUSD') or interval not in (15,60,240,1440) or not math.isfinite(capital) or capital <= 0:
        raise ValueError('Invalid community configuration')
    with db(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS treasury(id INTEGER PRIMARY KEY CHECK(id=1), pair TEXT NOT NULL, interval INTEGER NOT NULL, initial REAL NOT NULL, reserve REAL NOT NULL, peak REAL NOT NULL, stopped INTEGER NOT NULL DEFAULT 0, last_ts INTEGER NOT NULL DEFAULT 0, last_holdout INTEGER NOT NULL DEFAULT 0, last_rebalance INTEGER NOT NULL DEFAULT 0, last_bid REAL);
        CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY, family TEXT NOT NULL, account TEXT NOT NULL, funding REAL NOT NULL, shares REAL NOT NULL, active TEXT, pending TEXT, active_after INTEGER, pending_after INTEGER, first_tick INTEGER, ticks INTEGER NOT NULL DEFAULT 0, active_since INTEGER, active_fills INTEGER NOT NULL DEFAULT 0, active_ticks INTEGER NOT NULL DEFAULT 0, active_start_nav REAL);
        CREATE TABLE IF NOT EXISTS rounds(id INTEGER PRIMARY KEY, created INTEGER NOT NULL, source TEXT NOT NULL, test_start INTEGER NOT NULL, test_end INTEGER NOT NULL UNIQUE, observations INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS trials(round_id INTEGER REFERENCES rounds(id), agent TEXT REFERENCES agents(id), report TEXT NOT NULL, PRIMARY KEY(round_id,agent));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ticks(ts INTEGER PRIMARY KEY, observed INTEGER NOT NULL, bid REAL NOT NULL, ask REAL NOT NULL, equity REAL NOT NULL, detail TEXT NOT NULL);
        ''')
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM treasury').fetchone()
        if row:
            if (row['pair'],row['interval']) != (pair,interval):
                raise ValueError('Database is bound to another market/interval')
            c.rollback(); return
        c.execute('INSERT INTO treasury(id,pair,interval,initial,reserve,peak) VALUES(1,?,?,?,?,?)',(pair,interval,capital,capital*.25,capital))
        for name,family in AGENTS.items():
            a=Account(cash=capital*.25,peak=capital*.25)
            c.execute('INSERT INTO agents(id,family,account,funding,shares) VALUES(?,?,?,?,?)',(name,family,encode(asdict(a)),a.cash,a.cash))
        event(c,'init',{'capital':capital,'pair':pair,'interval':interval})
        c.commit()


def event(c, kind, detail):
    c.execute('INSERT INTO events(ts,kind,detail) VALUES(?,?,?)',(int(time.time()),kind,encode(detail)))


def config(path):
    with db(path) as c:
        row=c.execute('SELECT * FROM treasury').fetchone()
        if row is None: raise ValueError('Initialize community first')
        return dict(row)


def candidates(family, model, training, agent, prior):
    # Agents have disjoint strategy families; prior reports contain training/validation only.
    defaults=[Strategy(family,f,s,f'{agent}-{f}-{s}') for f,s in [(5,20),(12,48),(8,35),(20,80)]]
    # Diverse deterministic baselines remain available without a model.
    defaults[2]=Strategy(family,8,35,f'{agent}-8-35-filtered',({'feature':'volatility','window':20,'op':'lt','threshold':.03},))
    defaults[3]=Strategy(family,20,80,f'{agent}-20-80-filtered',({'feature':'momentum','window':12,'op':'gt','threshold':-.01},))
    if not model: return defaults
    returns=[b.close/a.close-1 for a,b in zip(training,training[1:])]
    summary={'n':len(training),'return':training[-1].close/training[0].close-1,'mean':sum(returns)/len(returns),'volatility':math.sqrt(sum(x*x for x in returns)/len(returns)),'prior_training_validation':prior}
    schema={'type':'object','properties':{'strategies':{'type':'array','maxItems':5,'items':{'type':'object','properties':{'fast':{'type':'integer'},'slow':{'type':'integer'},'filters':filter_schema(),'hypothesis':{'type':'string','maxLength':500}},'required':['fast','slow'],'additionalProperties':False}}},'required':['strategies'],'additionalProperties':False}
    r=request('http://127.0.0.1:11434/api/chat',{'model':model,'stream':False,'format':schema,'messages':[{'role':'user','content':f'You are independent research agent {agent}, specializing in {family}. Propose up to five falsifiable hypotheses with 2 <= fast < slow <= 100. Explain each in hypothesis (max 500 characters): what pattern, why it may persist, and what would disprove it. Avoid repeating failed prior configurations. Optional filters (AND): momentum (fractional return), distance (price/SMA-1), volatility (standard deviation of fractional returns), rsi (0..100); each has window 2..100, op gt/lt, threshold. Training-only evidence: {encode(summary)}. Avoid claims of future profit.'}],'options':{'temperature':.4,'num_predict':512}})
    proposed=json.loads(r['message']['content'])['strategies']
    for x in proposed[:5]:
        try:
            s=Strategy(family,x['fast'],x['slow'],f'{agent}-{x["fast"]}-{x["slow"]}',x.get('filters',()),x.get('hypothesis',''))
            if not any((s.fast,s.slow,s.filters)==(d.fast,d.slow,d.filters) for d in defaults): defaults.append(s)
        except (KeyError,TypeError,ValueError): continue
    return defaults


def same_strategy(left, right):
    if not left or not right: return False
    a,b=Strategy(**json.loads(left)),Strategy(**json.loads(right))
    return (a.kind,a.fast,a.slow,a.filters)==(b.kind,b.fast,b.slow,b.filters)


FORWARD_FREEZE_SECONDS = 60*86400


def stage_candidate(c, agent, strategy, holdout_end):
    """Do not reset a forward trial merely because a new backtest looks good."""
    r=c.execute('SELECT * FROM agents WHERE id=?',(agent,)).fetchone()
    t=c.execute('SELECT last_ts,stopped FROM treasury').fetchone()
    if t['stopped'] or Account(**json.loads(r['account'])).stopped: return 'risk stopped'
    proposed=encode(strategy)
    if any(same_strategy(proposed,old) for old in (r['active'],r['pending'])): return 'unchanged'
    if r['pending']: return 'pending candidate already frozen'
    if r['active'] and r['active_since'] is not None and t['last_ts']-r['active_since']<FORWARD_FREEZE_SECONDS:
        return 'active forward trial frozen'
    c.execute('UPDATE agents SET pending=?,pending_after=? WHERE id=?',(proposed,holdout_end,agent))
    return 'staged'


def research(path, bars, source, model=None, now=None):
    if source not in ('synthetic','csv','kraken'): raise ValueError('Unknown data source')
    cfg=config(path); bars=validate(bars,cfg['interval']*60)
    now=int(time.time()) if now is None else now
    if len(bars)<MIN_HISTORY: raise ValueError(f'Need at least {MIN_HISTORY} bars')
    if source!='synthetic' and bars[-1].ts+cfg['interval']*60>now: raise ValueError('History contains an incomplete/future candle')
    if source=='synthetic':
        # No synthetic evidence is written into a real community's research ledger.
        return {'status':'synthetic demonstration only','reports':{a:assess(bars,candidates(f,None,bars[:int(len(bars)*.6)],a,[]),int(len(bars)*.8)) for a,f in AGENTS.items()}}
    end=bars[-1].ts
    cutoff=next((i for i,b in enumerate(bars) if b.ts>cfg['last_holdout']),len(bars))
    hold=max(int(len(bars)*.8),cutoff)
    if len(bars)-hold<MIN_TEST: return {'status':'waiting for new unseen data','new_test_bars':len(bars)-hold,'required':MIN_TEST}
    train_end=int(hold*.75)
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT last_holdout,stopped FROM treasury').fetchone()
        if current['stopped']: return {'status':'stopped'}
        if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='batches'").fetchone() and c.execute("SELECT id FROM batches WHERE state='open'").fetchone(): raise ValueError('Queued research batch open; finalize it first')
        if current['last_holdout']!=cfg['last_holdout']: raise ValueError('Research state changed; retry with fresh data')
        prior={}
        for a in AGENTS:
            row=c.execute('SELECT report FROM trials WHERE agent=? ORDER BY round_id DESC LIMIT 1',(a,)).fetchone()
            prior[a]=json.loads(row[0])['candidates'][:3] if row else []
        # Consume the holdout before evaluation, even if inference or a later write fails.
        c.execute('UPDATE treasury SET last_holdout=?',(end,))
        event(c,'holdout_reserved',{'start':bars[hold].ts,'end':end,'source':source}); c.commit()
    reports={a:assess(bars,candidates(f,model,bars[:train_end],a,prior[a]),hold) for a,f in AGENTS.items()}
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT last_holdout,stopped FROM treasury').fetchone()
        if current['last_holdout']!=end or current['stopped']:
            raise ValueError('Research superseded or stopped; holdout remains consumed')
        rid=c.execute('INSERT INTO rounds(created,source,test_start,test_end,observations) VALUES(?,?,?,?,?)',(now,source,bars[hold].ts,end,len(bars))).lastrowid
        for a,r in reports.items():
            c.execute('INSERT INTO trials VALUES(?,?,?)',(rid,a,encode(r)))
            if r['eligible']:
                event(c,'candidate_decision',{'agent':a,'round':rid,'decision':stage_candidate(c,a,r['strategy'],end)})
        c.execute('UPDATE treasury SET last_holdout=?',(end,))
        event(c,'research',{'round':rid,'source':source,'model':model,'eligible':[a for a,r in reports.items() if r['eligible']]})
        c.commit()
    return {'status':'researched','round':rid,'test_start':bars[hold].ts,'test_end':end,'reports':reports}


def tick(path, bars, bid, ask, now=None):
    cfg=config(path); bars=validate(bars,cfg['interval']*60)
    now=int(time.time()) if now is None else now
    ts=bars[-1].ts; end=ts+cfg['interval']*60
    if not 0<=now-end<=cfg['interval']*60+120 or not math.isfinite(bid) or not math.isfinite(ask) or not 0<bid<=ask: raise ValueError('Stale candle or invalid quote')
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE'); t=c.execute('SELECT * FROM treasury').fetchone()
        if ts<t['last_ts']:
            return {'status':'already processed','ts':ts}
        if ts==t['last_ts']:
            # Quotes can move sharply between candle closes. Revalue and enforce risk,
            # but never execute a new strategy entry or count another observation.
            rows=list(c.execute('SELECT * FROM agents ORDER BY id'))
            states={r['id']:Account(**json.loads(r['account'])) for r in rows}
            pre=t['reserve']+sum(a.equity(bid) for a in states.values())
            peak=max(t['peak'],pre); stopped=bool(t['stopped']) or pre<=peak*(1-MAX_DRAWDOWN)
            changed=False
            for name,a in states.items():
                before=a.trades; a.mark(bid)
                if stopped or a.stopped: a.fill(False,bid,ask,COSTS); a.mark(bid)
                changed=changed or a.trades!=before
                c.execute('UPDATE agents SET account=?,active_fills=active_fills+? WHERE id=?',(encode(asdict(a)),a.trades-before,name))
            equity=t['reserve']+sum(a.equity(bid) for a in states.values())
            if equity<=peak*(1-MAX_DRAWDOWN) and not stopped:
                stopped=True
                for name,a in states.items():
                    before=a.trades; a.fill(False,bid,ask,COSTS); a.mark(bid)
                    changed=changed or a.trades!=before
                    c.execute('UPDATE agents SET account=?,active_fills=active_fills+? WHERE id=?',(encode(asdict(a)),a.trades-before,name))
            c.execute('UPDATE treasury SET last_bid=?,peak=?,stopped=?',(bid,peak,int(stopped)))
            if stopped and not t['stopped']: event(c,'portfolio_stop',{'candle':ts,'bid':bid,'intrabar':True})
            if changed: event(c,'halt_flatten' if stopped else 'agent_risk_flatten',{'candle':ts,'bid':bid,'ask':ask})
            c.commit()
            return {'status':'halted and flattened' if changed and stopped else 'risk flattened' if changed else 'already processed','ts':ts}
        rows=list(c.execute('SELECT * FROM agents ORDER BY id'))
        states={r['id']:Account(**json.loads(r['account'])) for r in rows}
        pre=t['reserve']+sum(a.equity(bid) for a in states.values()); peak=max(t['peak'],pre)
        stopped=bool(t['stopped']) or pre<=peak*(1-MAX_DRAWDOWN)
        details={}
        for r in rows:
            name=r['id']; a=states[name]; a.mark(bid)
            active=r['active']; after=r['active_after']; pending=r['pending']; pa=r['pending_after']
            active_since=r['active_since']; active_fills=r['active_fills']; active_ticks=r['active_ticks']; active_start_nav=r['active_start_nav']
            if not a.units and pending and ts>pa and not stopped and not a.stopped:
                active,after,pending,pa=pending,pa,None,None
                active_since=ts; active_fills=0; active_ticks=0; active_start_nav=a.cash/r['shares']
            # If candidate is pending, unwind existing strategy before switching; no overlapping positions.
            signal=bool(active and ts>after and not (pending and ts>pa) and not stopped and not a.stopped and Strategy(**json.loads(active)).signal([b.close for b in bars]))
            before=a.trades; a.fill(signal,bid,ask,COSTS); a.mark(bid)
            if a.stopped: a.fill(False,bid,ask,COSTS); a.mark(bid); signal=False
            active_fills+=a.trades-before
            active_ticks+=int(bool(active))
            details[name]={'equity':a.equity(bid),'signal':signal,'fills':a.trades-before,'active':json.loads(active) if active else None}
            c.execute('UPDATE agents SET account=?,active=?,active_after=?,pending=?,pending_after=?,first_tick=COALESCE(first_tick,?),ticks=ticks+1,active_since=?,active_fills=?,active_ticks=?,active_start_nav=? WHERE id=?',(encode(asdict(a)),active,after,pending,pa,ts if active else None,active_since,active_fills,active_ticks,active_start_nav,name))
        equity=t['reserve']+sum(a.equity(bid) for a in states.values()); peak=max(peak,equity)
        # Fees/slippage can cross the portfolio limit during fills; flatten immediately in this simulation.
        if equity<=peak*(1-MAX_DRAWDOWN) or stopped:
            stopped=True
            for name,a in states.items():
                before=a.trades; a.fill(False,bid,ask,COSTS); a.mark(bid)
                details[name].update(equity=a.equity(bid),signal=False,fills=details[name]['fills']+a.trades-before)
                c.execute('UPDATE agents SET account=?,active_fills=active_fills+? WHERE id=?',(encode(asdict(a)),a.trades-before,name))
            equity=t['reserve']+sum(a.equity(bid) for a in states.values())
        c.execute('UPDATE treasury SET last_ts=?,last_bid=?,peak=?,stopped=?',(ts,bid,peak,int(stopped)))
        c.execute('INSERT INTO ticks VALUES(?,?,?,?,?,?)',(ts,now,bid,ask,equity,encode(details)))
        if stopped and not t['stopped']: event(c,'portfolio_stop',{'equity':equity,'peak':peak})
        c.commit()
    return {'status':'paper','ts':ts,'equity':equity,'stopped':stopped,'agents':details}


def rebalance(path, now=None):
    """Transfer paper cash only, once per week, after 30 days and 20 fills.
    Never increase a stopped agent or allocate more than 40% to one agent.
    """
    now=int(time.time()) if now is None else now
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE'); t=c.execute('SELECT * FROM treasury').fetchone(); rows=list(c.execute('SELECT * FROM agents ORDER BY id'))
        if t['stopped'] or now-t['last_rebalance']<7*86400: return {'status':'stopped or cooldown'}
        if not t['last_ts'] or not 0<=now-(t['last_ts']+t['interval']*60)<=t['interval']*60+120: return {'status':'stale market; no allocation'}
        states={r['id']:Account(**json.loads(r['account'])) for r in rows}
        if any(a.units for a in states.values()): return {'status':'wait until all portfolios flat'}
        eligible=[r for r in rows if r['active'] and not r['pending'] and r['active_since'] is not None and t['last_ts']-r['active_since']>=30*86400 and r['active_fills']>=20 and r['active_ticks']>=.95*(1+(t['last_ts']-r['active_since'])/(t['interval']*60)) and r['active_start_nav'] and not states[r['id']].stopped and states[r['id']].cash/r['shares']/r['active_start_nav']>1]
        if not eligible: return {'status':'insufficient positive forward evidence'}
        best=max(eligible,key=lambda r:states[r['id']].cash/r['shares']/r['active_start_nav']-1-states[r['id']].drawdown)
        a=states[best['id']]; total=t['reserve']+sum(x.cash for x in states.values())
        amount=max(0,min(total*.05,t['reserve']-total*.15,total*.40-a.cash))
        if amount<.01: return {'status':'no available allocation'}
        # Share issuance at current NAV preserves performance across capital flows.
        nav=a.cash/best['shares']; new_shares=best['shares']+amount/nav
        old_cash=a.cash; a.cash+=amount; a.peak*=a.cash/old_cash
        c.execute('UPDATE agents SET account=?,funding=funding+?,shares=? WHERE id=?',(encode(asdict(a)),amount,new_shares,best['id']))
        c.execute('UPDATE treasury SET reserve=reserve-?,last_rebalance=?',(amount,now))
        event(c,'allocation',{'agent':best['id'],'amount':amount,'nav':nav}); c.commit()
        return {'status':'allocated','agent':best['id'],'amount':amount}


def status(path):
    with db(path) as c:
        t=dict(c.execute('SELECT * FROM treasury').fetchone()); agents=[]
        for r in c.execute('SELECT * FROM agents ORDER BY id'):
            a=Account(**json.loads(r['account'])); equity=a.equity(t['last_bid'] or 0)
            agents.append({'id':r['id'],'family':r['family'],'equity':equity,'net_pnl':equity-r['funding'],'nav_return':equity/r['shares']-1,'drawdown':a.drawdown,'fills':a.trades,'stopped':a.stopped,'active':json.loads(r['active']) if r['active'] else None,'pending':json.loads(r['pending']) if r['pending'] else None,'active_since':r['active_since'],'active_fills':r['active_fills'],'active_ticks':r['active_ticks'],'active_nav_return':(equity/r['shares']/r['active_start_nav']-1) if r['active_start_nav'] else None})
        equity=t['reserve']+sum(a['equity'] for a in agents)
        return {'mode':'PAPER ONLY','pair':t['pair'],'interval':t['interval'],'initial':t['initial'],'equity':equity,'net_pnl':equity-t['initial'],'reserve':t['reserve'],'stopped':bool(t['stopped']),'last_ts':t['last_ts'],'last_holdout':t['last_holdout'],'agents':agents}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/community.sqlite'); sub=p.add_subparsers(dest='cmd',required=True)
    i=sub.add_parser('init'); i.add_argument('--pair',default='XBTUSD'); i.add_argument('--interval',type=int,default=60); i.add_argument('--capital',type=float,default=1000)
    r=sub.add_parser('research'); r.add_argument('--source',choices=['synthetic','csv','kraken'],default='kraken'); r.add_argument('--csv'); r.add_argument('--model')
    d=sub.add_parser('run'); d.add_argument('--model'); d.add_argument('--poll',type=int,default=60); d.add_argument('--research-hours',type=int,default=24); d.add_argument('--once',action='store_true'); d.add_argument('--paper-only',action='store_true')
    rl=sub.add_parser('research-loop'); rl.add_argument('--model'); rl.add_argument('--hours',type=int,default=24); rl.add_argument('--once',action='store_true')
    sub.add_parser('status'); sub.add_parser('rebalance'); sub.add_parser('history'); sub.add_parser('trials'); sub.add_parser('stop'); sub.add_parser('readiness')
    x=p.parse_args()
    def out(v): print(json.dumps(v,indent=2,allow_nan=False),flush=True)
    if x.cmd=='init': init(x.db,x.pair,x.interval,x.capital); out(status(x.db)); return
    if x.cmd=='status': out(status(x.db)); return
    if x.cmd=='rebalance': out(rebalance(x.db)); return
    if x.cmd=='stop':
        with db(x.db) as c:
            c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE treasury SET stopped=1'); event(c,'operator_stop',{'note':'Paper positions will flatten at next fresh quote'}); c.commit()
        out({'status':'stopped; run paper loop to flatten'}); return
    if x.cmd=='readiness': out(readiness(x.db)); return
    if x.cmd=='trials':
        with db(x.db) as c: out([{'round':r['round_id'],'agent':r['agent'],'report':json.loads(r['report'])} for r in c.execute('SELECT * FROM trials ORDER BY round_id DESC,agent LIMIT 30')]); return
    if x.cmd=='history':
        with db(x.db) as c: out([dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]); return
    cfg=config(x.db)
    from . import archive
    if x.cmd=='research':
        if x.source=='csv' and not x.csv: p.error('--csv required')
        if x.source=='synthetic' and cfg['interval']!=60: p.error('synthetic uses hourly bars')
        bars=synthetic() if x.source=='synthetic' else load_csv(x.csv,cfg['interval']*60) if x.source=='csv' else kraken(cfg['pair'],cfg['interval'])
        if x.source!='synthetic':
            archive.ingest(x.db,bars,x.source); bars=archive.latest(x.db)
        out(research(x.db,bars,x.source,x.model)); return
    if x.cmd=='research-loop':
        if x.hours<1: p.error('hours must be >=1')
        while True:
            try:
                if status(x.db)['stopped']: out({'status':'stopped'}); break
                out(research(x.db,archive.sync(x.db),'kraken',x.model))
            except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error) as e:
                out({'status':'research skipped','error':str(e)})
                if x.once: raise
            if x.once: break
            time.sleep(x.hours*3600)
        return
    if x.poll<30 or x.research_hours<1: p.error('poll >=30 seconds; research-hours >=1')
    next_research=0
    while True:
        try:
            bars=kraken(cfg['pair'],cfg['interval']); bid,ask=quote(cfg['pair']); out(tick(x.db,bars,bid,ask)); out(rebalance(x.db))
            if not x.paper_only and time.time()>=next_research and not status(x.db)['stopped']:
                next_research=time.time()+x.research_hours*3600
                archive.ingest(x.db,bars,'kraken'); out(research(x.db,archive.latest(x.db),'kraken',x.model))
        except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error) as e:
            out({'status':'cycle skipped','error':str(e)})
            if x.once: raise
        if x.once: break
        time.sleep(x.poll)

def readiness(path):
    s=status(path); results=[]
    for a in s['agents']:
        days=(s['last_ts']-a['active_since'])/86400 if a['active_since'] is not None else 0
        checks={'same_strategy_60_days':days>=60,'at_least_40_fills':a['active_fills']>=40,'at_least_95_percent_candle_coverage':a['active_ticks']>=.95*(1+days*1440/s['interval']),'positive_net_forward_return':a['active_nav_return'] is not None and a['active_nav_return']>0,'no_risk_stop':not a['stopped'] and not s['stopped'],'drawdown_below_5_percent':a['drawdown']<.05,'no_pending_strategy':a['pending'] is None}
        results.append({'agent':a['id'],'forward_days':days,'checks':checks,'eligible_for_manual_review':all(checks.values())})
    return {'mode':'paper only; no exchange order capability','agents':results,'note':'Review gate only, not evidence of statistical significance or authorization to trade. Require benchmark comparison, independent cost audit, exchange sandbox, and operator sign-off.'}

if __name__=='__main__': main()

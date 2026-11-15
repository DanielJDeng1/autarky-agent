"""Frozen prospective shadow-paper production for economic agent generations.
No exchange credentials, real orders, or economic settlement. No historical fills.
"""
import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

from . import core, economic_kernel as economy

PROTOCOL = 'v0.25-production-1'
PAIRS = ('XBTUSD', 'ETHUSD')
ENTRY_GRACE = 120
WINDOW = 720 * 3600


def pack(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(pack(value).encode()).hexdigest()


def code_identity():
    from . import lab, cross_asset, research_tree, autonomous_research, weekly_models, models, model_discovery
    modules = (lab, cross_asset, research_tree, autonomous_research, weekly_models, models, model_discovery, core)
    sources={m.__name__: Path(m.__file__) for m in modules}
    sources[__name__]=Path(__file__)
    # CLI __main__ and imported module must produce the same fingerprint.
    sources['autarky_trade.production']=sources.pop(__name__)
    return {name: hashlib.sha256(path.read_text(encoding='utf-8').encode()).hexdigest() for name,path in sources.items()}


def owner(path, ident):
    with closing(economy.connect(path, True)) as c:
        a = c.execute('SELECT * FROM economy_agents WHERE id=?', (ident,)).fetchone()
        if not a or a['state']=='retired': raise ValueError('Agent generation is not a living incumbent')
        return economy.balance(c, ident+':productive')


def feature_columns(bars, peer, keys):
    from . import lab, cross_asset as cross
    f=lab.Features(bars)
    return {k:cross.series(bars,peer,*k) if k[0] in cross.FEATURES else f.series(*k) for k in keys}


def vector(cols, i):
    from . import lab
    values=[]
    for k,series in cols.items():
        v=series[i]
        if v is None and k[0]=='volume_ratio': v=1.0
        if v is None or not math.isfinite(v): raise ValueError('Missing causal feature at decision')
        values.append(v)
    return values


def training_rows(parts, pair, keys, h):
    """Exact v0.20 window sampling, without evaluating any future labels."""
    from . import lab, weekly_models as wm
    other=next(p for p in PAIRS if p!=pair); rows=[]
    for bars,peer in zip(parts[pair],parts[other]):
        if len(bars)<lab.WARMUP+720: continue
        cols=feature_columns(bars,peer,keys)
        for start in range(lab.WARMUP,len(bars)-719,720):
            for i in range(start,start+720-h,h):
                rows.append({'start':bars[i].ts,'label_end':bars[i+h].ts,
                    'realized':bars[i+h].open/bars[i].open-1,'x':vector(cols,i)})
    return rows[-26*wm.horizon_slots(h):]


def unpack_rows(rows,keys):
    return [dict(r,features=dict(zip(keys,r['x']))) for r in rows]


def schedule(start,end,h):
    from . import weekly_models as wm
    return [w+i*h*3600 for w in range(start,end,WINDOW) for i in range(wm.horizon_slots(h)) if w+(i+1)*h*3600<end]


def freeze_artifact(report, parts, cutoff, node, pair, h, agent, capital_cents, start, end, now):
    from . import lab, cross_asset as cross, research_tree as tree, weekly_models as wm
    if pair not in PAIRS or type(start) is not int or type(end) is not int or start%3600 or end%3600 or start<=now or end-start<90*86400 or end-start>360*86400 or cutoff+3600>now:
        raise ValueError('Require a future UTC hourly start, 90..360 day duration, and completed development data')
    wm.horizon_slots(h)
    selected=[r for r in report['results'] if r['id']==node and r['horizon_hours']==h]
    if len(selected)!=1: raise ValueError('Select one node/horizon from the saved review')
    r=selected[0]; spec=tree.Spec(**r['spec']); keys=spec.features
    if max(w for _,w in keys)>718: raise ValueError('Model lookback exceeds public REST closed-history capacity')
    if type(capital_cents) is not int or capital_cents<=0: raise ValueError('No productive capital for this agent')
    aligned=cross.align(parts); train=training_rows(aligned,pair,keys,h)
    if len(train)<12*wm.horizon_slots(h) or any(t['label_end']>cutoff for t in train): raise ValueError('Insufficient completed training')
    saved=r['reports'][pair]; lookback=26*wm.horizon_slots(h)
    prior={name:[s for w in saved['details'] for s in w['models'][name]['samples'] if s['label_end']<=cutoff][-lookback:] for name in ('candidate','mean')}
    thresholds={name:wm.horizon_calibrate(rows,h,name=='candidate' and spec.calibration=='conservative') for name,rows in prior.items()}
    if any(len(rows)<12*wm.horizon_slots(h) for rows in prior.values()): raise ValueError('Insufficient out-of-fold calibration history')
    model=tree.fit(spec,unpack_rows(train,keys)); probe=model(dict(zip(keys,train[-1]['x'])))
    if not math.isfinite(probe): raise ValueError('Invalid frozen predictor')
    plan={'protocol':PROTOCOL,'agent':agent,'source_node':node,'pair':pair,'horizon_hours':h,'spec':asdict(spec),
          'development_dataset':report['dataset'],'development_cutoff':cutoff,'training':train,'calibration':prior,
          'thresholds':thresholds,'initial_cents':capital_cents,'start':start,'end':end,'registered':now,
          'normal':asdict(lab.NORMAL),'stress':asdict(lab.STRESS),'code_identity':code_identity(),
          'entry_grace_seconds':ENTRY_GRACE,'development_only':False,'paper_only':True,'eligible':False}
    return plan


def create(path, plan, economy_path):
    """A separate, immutable-per-generation shadow ledger. No treasury debit."""
    from .coordinator import exclusive
    p=Path(path).resolve(); p.parent.mkdir(parents=True,exist_ok=True)
    if p==Path(economy_path).resolve(): raise ValueError('Separate shadow production ledger required')
    with exclusive(str(p)+'.lock'), closing(sqlite3.connect(p,timeout=30)) as c,c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables-{'production_trials','production_events'}: raise ValueError('Use a separate production database')
        c.execute('CREATE TABLE IF NOT EXISTS production_trials(id TEXT PRIMARY KEY, agent TEXT NOT NULL UNIQUE, plan TEXT NOT NULL, economy TEXT NOT NULL, state TEXT NOT NULL, snapshot TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS production_events(id INTEGER PRIMARY KEY, trial TEXT NOT NULL, observed INTEGER NOT NULL, report TEXT NOT NULL, UNIQUE(trial,observed))')
        ident=digest(plan); initial=plan['initial_cents']/100
        accounts={k:asdict(core.Account(cash=initial,peak=initial)) for k in ('candidate_normal','candidate_stress','mean_normal','mean_stress')}
        snap={'accounts':accounts,'exit_at':{},'cursor':0,'missed':0,'last_observed':0,'last_bid':None,'max_quote_gap':0,'late_exits':0,'operator_stop':False}
        c.execute('INSERT INTO production_trials VALUES(?,?,?,?,?,?)',(ident,plan['agent'],pack(plan),str(Path(economy_path).resolve()),'pending',pack(snap)))
    return ident


def prediction(plan,bars,decision):
    from . import research_tree as tree, weekly_models as wm
    spec=tree.Spec(**plan['spec']); keys=spec.features; pair=plan['pair']; other=next(p for p in PAIRS if p!=pair)
    if set(bars)!=set(PAIRS): raise ValueError('Both markets required')
    # Use the shared contiguous suffix ending at the last closed hour. No interpolation.
    common={p:{b.ts:b for b in core.validate(list(bars[p]),3600)} for p in PAIRS}
    last=decision-3600
    if any(max(common[p])!=last for p in PAIRS): raise ValueError('Closed market candles not synchronized to decision')
    count=max(w for _,w in keys)+1
    stamps=list(range(decision-count*3600,decision,3600))
    if any(t not in common[p] for p in PAIRS for t in stamps): raise ValueError('Insufficient aligned closed history')
    def series(p):
        seq=[common[p][t] for t in stamps]; b=seq[-1]
        return seq+[core.Bar(decision,b.close,b.close,b.close,b.close,0.)]
    a,b=series(pair),series(other); values=vector(feature_columns(a,b,keys),len(a)-1)
    rows=unpack_rows(plan['training'],keys); x=dict(zip(keys,values))
    return {'candidate':tree.fit(spec,rows)(x),'mean':wm.fit(rows,())(x)}


def step(plan,snap,now,bid,ask,bars=None,retired=False):
    """Pure transition; all fills use a quote observed in this call, never an old candle open."""
    if type(now) is not int or now<=snap['last_observed'] or not all(math.isfinite(v) for v in (bid,ask)) or not 0<bid<=ask:
        raise ValueError('Duplicate/older observation or invalid quote')
    if now<plan['start']:
        state='stopped' if retired or snap['operator_stop'] else 'pending'
        return snap,{'state':state,'observed':now,'fills':[],'decision':None}
    s=json.loads(pack(snap)); accounts={k:core.Account(**a) for k,a in s['accounts'].items()}
    event={'observed':now,'bid':bid,'ask':ask,'fills':[],'decision':None,'missed':[]}
    if s['last_observed']: s['max_quote_gap']=max(s['max_quote_gap'],now-s['last_observed'])
    else: s['max_quote_gap']=max(0,now-plan['start'])
    stopping=retired or s['operator_stop'] or now>=plan['end']; h=plan['horizon_hours']
    def fill(k,buy,why):
        a=accounts[k]; before=asdict(a); cost=core.Costs(**plan['stress' if k.endswith('_stress') else 'normal'])
        a.fill(buy,bid,ask,cost); a.mark(bid)
        if a.trades!=before['trades']: event['fills'].append({'account':k,'side':'buy' if buy else 'sell','reason':why,'before':before,'after':asdict(a)})
    for k,a in accounts.items():
        a.mark(bid); due=s['exit_at'].get(k)
        if a.units and (stopping or a.stopped or (due is not None and now>=due)):
            if due is not None and now>due+ENTRY_GRACE: s['late_exits']+=1
            fill(k,False,'end/operator/retirement' if stopping else 'risk stop' if a.stopped else 'scheduled exit'); s['exit_at'].pop(k,None)
    slots=schedule(plan['start'],plan['end'],h)
    while s['cursor']<len(slots) and slots[s['cursor']]<=now:
        due=slots[s['cursor']]
        if stopping or now>due+ENTRY_GRACE:
            s['cursor']+=1; s['missed']+=1; event['missed'].append(due); continue
        if bars is None: break
        try: preds=prediction(plan,bars,due)
        except ValueError as exc:
            event['data_error']=str(exc); break  # retry within grace, never backfill
        event['decision']={'scheduled':due,'predictions':preds,'thresholds':plan['thresholds']}
        s['cursor']+=1
        for k,a in accounts.items():
            name=k.rsplit('_',1)[0]; threshold=plan['thresholds'][name]
            if threshold is not None and preds[name]>threshold and not a.units and not a.stopped:
                fill(k,True,'scheduled entry'); s['exit_at'][k]=due+h*3600
                if a.stopped and a.units:
                    fill(k,False,'post-fill risk stop'); s['exit_at'].pop(k,None)
    s.update(accounts={k:asdict(a) for k,a in accounts.items()},last_observed=now,last_bid=bid)
    event['state']='complete' if now>=plan['end'] else 'stopped' if stopping else 'running'
    return s,event


def observe(path, ident, bars, bid,ask,now=None):
    now=int(time.time()) if now is None else now
    with closing(sqlite3.connect(path,timeout=30)) as c,c:
        c.row_factory=sqlite3.Row; c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM production_trials WHERE id=?',(ident,)).fetchone()
        if not r: raise ValueError('Unknown production trial')
        plan=json.loads(r['plan']); s=json.loads(r['snapshot'])
        if digest(plan)!=ident or plan['code_identity']!=code_identity(): raise ValueError('Frozen plan or evaluator implementation changed')
        if r['state'] in ('complete','stopped'): return {'state':r['state'],'trial':ident}
        try: owner(r['economy'],plan['agent']); retired=False
        except ValueError: retired=True
        # Inaccessible economy ledger must fail closed, not open new trades.
        if now<=s['last_observed']: return {'state':r['state'],'trial':ident,'duplicate':True}
        s,event=step(plan,s,now,bid,ask,bars,retired)
        if event['state']!='pending':
            c.execute('UPDATE production_trials SET state=?,snapshot=? WHERE id=?',(event['state'],pack(s),ident))
            c.execute('INSERT INTO production_events(trial,observed,report) VALUES(?,?,?)',(ident,now,pack(event)))
        return dict(event,trial=ident)


def status(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row; out=[]
        for r in c.execute('SELECT * FROM production_trials ORDER BY agent'):
            p=json.loads(r['plan']); s=json.loads(r['snapshot'])
            if digest(p)!=r['id']: raise ValueError('Frozen plan changed')
            a={k:core.Account(**v) for k,v in s['accounts'].items()}
            out.append({'id':r['id'],'agent':r['agent'],'state':r['state'],'pair':p['pair'],'source_node':p['source_node'],'horizon_hours':p['horizon_hours'],'start':p['start'],'end':p['end'],'thresholds':p['thresholds'],'initial_usd':p['initial_cents']/100,'accounts':{k:dict(asdict(v),equity=v.equity(s['last_bid'] or 0),net=v.equity(s['last_bid'] or 0)-p['initial_cents']/100) for k,v in a.items()},'decisions_processed':s['cursor'],'missed':s['missed'],'late_exits':s['late_exits'],'max_quote_gap':s['max_quote_gap'],'last_observed':s['last_observed'],'last_bid':s['last_bid'],'stop_requested':s['operator_stop'],'paper_only':True,'eligible':False})
    return {'protocol':PROTOCOL,'trials':out,'economic_settlement':False,'real_revenue_usd':0,'note':'Shadow paper only. No economic treasury debit or credit; candidate/mean and normal/stress are alternative simulations, not additive capital.'}


def stop(path,ident):
    with closing(sqlite3.connect(path,timeout=30)) as c,c:
        c.execute('BEGIN IMMEDIATE'); r=c.execute('SELECT snapshot FROM production_trials WHERE id=?',(ident,)).fetchone()
        if not r: raise ValueError('Unknown trial')
        s=json.loads(r[0]); s['operator_stop']=True
        c.execute('UPDATE production_trials SET snapshot=? WHERE id=?',(pack(s),ident))


def run(path,poll=30,once=False):
    from .coordinator import exclusive
    if poll<15: raise ValueError('Poll at least 15 seconds')
    with exclusive(str(Path(path).resolve())+'.lock'):
        while True:
            active=[r for r in status(path)['trials'] if r['state'] not in ('complete','stopped')]
            if not active: return
            for r in active:
                try:
                    now=int(time.time())
                    if now<r['start'] and not r['stop_requested']: continue
                    # Fetch candles only when a scheduled entry could still be made.
                    slots=schedule(r['start'],r['end'],r['horizon_hours']); i=r['decisions_processed']
                    bars=None
                    if any(0<=now-t<=ENTRY_GRACE for t in slots[i:]):
                        try: bars={p:core.kraken(p,60) for p in PAIRS}
                        except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
                            print(pack({'event':'candle_unavailable','trial':r['id'],'error':str(exc)}),flush=True)
                    quote_started=time.monotonic(); bid,ask=core.quote(r['pair']); now=int(time.time())
                    if time.monotonic()-quote_started>15: raise ValueError('Quote request exceeded 15-second latency limit')
                    print(pack(observe(path,r['id'],bars,bid,ask,now)),flush=True)
                except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error) as exc:
                    print(pack({'event':'production_error','trial':r['id'],'error':str(exc)}),flush=True)
                    if once: raise
            if once: return
            time.sleep(poll)


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',required=True); sub=p.add_subparsers(dest='cmd',required=True)
    f=sub.add_parser('freeze')
    for k in ('economy','tree','review','btc','eth','agent','pair'): f.add_argument('--'+k,required=True)
    f.add_argument('--node',type=int,required=True); f.add_argument('--horizon',type=int,required=True); f.add_argument('--cutoff',type=int,required=True); f.add_argument('--days',type=int,default=90); f.add_argument('--start',type=int)
    r=sub.add_parser('run'); r.add_argument('--poll',type=int,default=30); r.add_argument('--once',action='store_true'); sub.add_parser('status'); st=sub.add_parser('stop'); st.add_argument('--trial',required=True)
    x=p.parse_args()
    if x.cmd=='freeze':
        from . import multiyear, research_allocator as allocator
        if Path(x.db).resolve() in {Path(v).resolve() for v in (x.economy,x.tree,x.review,x.btc,x.eth)}: p.error('Use a new separate production ledger')
        report=json.loads(Path(x.review).read_text(encoding='utf-8-sig')); parts={'XBTUSD':multiyear.segments(x.btc,x.cutoff),'ETHUSD':multiyear.segments(x.eth,x.cutoff)}
        allocator.validate(report,x.tree,parts,x.cutoff); capital=owner(x.economy,x.agent)
        now=int(time.time()); start=x.start if x.start is not None else (now//3600+2)*3600
        plan=freeze_artifact(report,parts,x.cutoff,x.node,x.pair,x.horizon,x.agent,capital,start,start+x.days*86400,now)
        ident=create(x.db,plan,x.economy); print(json.dumps({'trial':ident,'start':start,'end':plan['end'],'thresholds':plan['thresholds'],'agent':x.agent,'paper_only':True},indent=2))
    elif x.cmd=='run': run(x.db,x.poll,x.once)
    elif x.cmd=='stop': stop(x.db,x.trial); print('Stop recorded; run again to flatten at a fresh public quote.')
    else: print(json.dumps(status(x.db),indent=2,allow_nan=False))


if __name__=='__main__': main()

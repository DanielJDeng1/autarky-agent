"""Compositional, evidence-grounded multi-asset research. Never trades or promotes."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from html import escape
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time

from . import autonomous_research as old, forecast_audit as fa, lab, model_discovery as md, multiyear, weekly_models as wm
from .coordinator import exclusive
from .core import request

PROTOCOL = 'v0.17-research-tree-1'
METHODS = ('ridge', 'shrink', 'robust', 'similarity', 'regime', 'logistic_up', 'logistic_downside')
WINDOWS = (6, 12, 24, 48, 72, 120, 168, 336, 504, 672, 720)
FEATURES = tuple(k for k in lab.FEATURES if k != 'forecast')
TRANSFORMS = ('raw', 'signed_log', 'absolute')
CALIBRATIONS = ('standard', 'conservative')


@dataclass(frozen=True)
class Spec:
    features: tuple
    method: str = 'ridge'
    transform: str = 'raw'
    calibration: str = 'standard'

    def __post_init__(self):
        if not isinstance(self.features, (list, tuple)) or not 1 <= len(self.features) <= 12:
            raise ValueError('Require 1..12 features')
        keys = []
        for item in self.features:
            if not isinstance(item, (list, tuple)) or len(item)!=2 or item[0] not in FEATURES or type(item[1]) is not int or item[1] not in WINDOWS:
                raise ValueError('Unsupported feature or lookback')
            keys.append(tuple(item))
        if len(set(keys)) != len(keys): raise ValueError('Duplicate features')
        if self.method not in METHODS or self.transform not in TRANSFORMS or self.calibration not in CALIBRATIONS:
            raise ValueError('Unsupported method, transform or calibration')
        if self.method=='regime' and (keys != [md.REGIME] or self.transform!='raw'):
            raise ValueError('Regime model requires raw momentum(672)')
        object.__setattr__(self,'features',tuple(sorted(keys)))

    @property
    def identity(self): return lab.digest([PROTOCOL,asdict(self)])


def roots():
    result={name:Spec(keys, 'regime' if name=='regime_mean' else 'similarity' if name=='similarity' else 'ridge')
            for name,keys in md.SPECS.items() if keys}
    result.update({e.name:Spec(e.features,e.method) for e in old.EXPERIMENTS})
    return result


def transform(spec, x):
    def convert(v):
        if spec.transform=='absolute': return abs(v)
        if spec.transform=='signed_log': return math.copysign(math.log1p(abs(v)),v)
        return v
    return {k:convert(x[k]) for k in spec.features}


def logistic(rows, keys, downside=False):
    """Regularized event classifier; map probability to training conditional mean returns."""
    y=[r['realized'] for r in rows]; labels=[float(v<-.05 if downside else v>0) for v in y]
    mu=statistics.mean(y)
    if min(labels)==max(labels): return lambda x: mu
    positive=statistics.mean(v for v,t in zip(y,labels) if t)
    negative=statistics.mean(v for v,t in zip(y,labels) if not t)
    centers=[statistics.mean(r['features'][k] for r in rows) for k in keys]
    scales=[max(1e-8,statistics.pstdev(r['features'][k] for r in rows)) for k in keys]
    def scale(x): return [max(-10.,min(10.,(x[k]-m)/s)) for k,m,s in zip(keys,centers,scales)]
    def sigmoid(v): return 1/(1+math.exp(-max(-40.,min(40.,v))))
    z=[scale(r['features']) for r in rows]; n=len(y); weights=[0.]*len(keys)
    p=statistics.mean(labels); intercept=math.log(p/(1-p))
    for _ in range(120):
        err=[sigmoid(intercept+sum(a*b for a,b in zip(weights,v)))-t for v,t in zip(z,labels)]
        intercept-=.1*sum(err)/n
        weights=[w-.1*(sum(e*v[j] for e,v in zip(err,z))/n+.25*w) for j,w in enumerate(weights)]
    def predict(x):
        p=sigmoid(intercept+sum(a*b for a,b in zip(weights,scale(x))))
        return p*positive+(1-p)*negative
    return predict


def fit(spec, rows):
    converted=[dict(r,features=transform(spec,r['features'])) for r in rows]
    if spec.method=='regime': predict=md.fit_model('regime_mean',converted,spec.features)
    elif spec.method in ('logistic_up','logistic_downside'): predict=logistic(converted,spec.features,spec.method=='logistic_downside')
    else: predict=old.fit_candidate(old.Experiment('candidate','beta','ridge_four',spec.features,spec.method,''),converted)
    return lambda x: predict(transform(spec,x))


def calibrate(spec, rows):
    if spec.calibration=='standard': return fa.calibrate(rows)
    if len(rows)<48: return None
    candidates=[(t,fa.selection(rows,t)) for t in fa.THRESHOLDS]
    candidates=[(t,m) for t,m in candidates if m['selected']>=16 and m['mean_stress_all']>0]
    return max(candidates,key=lambda x:(x[1]['mean_stress_all'],x[0]))[0] if candidates else None


def evaluate(parts,pair,cutoff,parent,candidate,now=None):
    configs={'parent':parent,'candidate':candidate}
    return wm.review(parts,pair,cutoff,now,specs={'mean':(),**{k:v.features for k,v in configs.items()}},
        fitter=lambda name,rows,keys: wm.fit(rows,()) if name=='mean' else fit(configs[name],rows),
        calibrator=lambda name,rows: fa.calibrate(rows) if name=='mean' else calibrate(configs[name],rows))


def facts(reports):
    """Only deterministic measurements are used as findings; prose is not evidence."""
    markets={}
    for pair,r in reports.items():
        m=r['summary']; v=m['candidate']; p=m['parent']; b=m['mean']
        def delta(base): return {k:v[k]-base[k] for k in ('mean_net','mean_stress','rmse')}
        markets[pair]={'net':v['mean_net'],'stress':v['mean_stress'],'rmse':v['rmse'],'fills':v['fills'],
                       'drawdown':v['worst_drawdown'],'windows':v['windows'],'active_windows':v['active_windows'],
                       'vs_parent':delta(p),'vs_mean':delta(b),
                       'stress_beats_parent':v['mean_stress']>p['mean_stress'],
                       'stress_beats_mean':v['mean_stress']>b['mean_stress'],
                       'positive_stress':v['mean_stress']>0,
                       'post_2017_stress_delta_mean':r['post_2017']['candidate']['mean_stress']-r['post_2017']['mean']['mean_stress'] if r['post_2017'] else None}
    both=all(m['stress_beats_parent'] and m['stress_beats_mean'] for m in markets.values())
    positive=all(m['positive_stress'] and m['fills']>0 for m in markets.values())
    return {'markets':markets,'classification':'positive_both_development_only' if positive else 'improves_both_but_not_profitable' if both else 'mixed_or_underperforms',
            'beats_both_controls_on_both_markets_stress':both,'positive_stress_both':positive,'eligible':False}


def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path,timeout=30); c.row_factory=sqlite3.Row
    try:
        with c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables-{'tree_runs','tree_nodes','tree_rejections'}: raise ValueError('Use a separate v0.17 research tree database')
            c.execute('CREATE TABLE IF NOT EXISTS tree_runs(id INTEGER PRIMARY KEY,dataset TEXT NOT NULL,status TEXT NOT NULL,report TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS tree_nodes(id INTEGER PRIMARY KEY,run INTEGER NOT NULL,dataset TEXT NOT NULL,identity TEXT NOT NULL,parent TEXT NOT NULL,agent TEXT NOT NULL,status TEXT NOT NULL,report TEXT NOT NULL,UNIQUE(dataset,identity))')
            c.execute('CREATE TABLE IF NOT EXISTS tree_rejections(id INTEGER PRIMARY KEY,run INTEGER NOT NULL,agent TEXT NOT NULL,reason TEXT NOT NULL)')
        return c
    except BaseException: c.close(); raise


def legacy_evidence(path,hashes,cutoff):
    if not path: return {}
    key=lab.digest([old.PROTOCOL,cutoff,hashes,[asdict(e) for e in old.EXPERIMENTS]])
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        base=c.execute('SELECT report FROM model_baselines WHERE dataset=?',(key,)).fetchone()
        if not base: raise ValueError('v0.16 ledger has no matching BTC/ETH dataset')
        b=json.loads(base[0]); output={}
        for name in roots():
            if name in b['XBTUSD']['summary']:
                output[name]={p:{'net':b[p]['summary'][name]['mean_net'],'stress':b[p]['summary'][name]['mean_stress']} for p in old.PAIRS}
        for name,raw in c.execute("SELECT name,report FROM model_experiments WHERE dataset=? AND status='complete'",(key,)):
            r=json.loads(raw)['summary']; output[name]={p:{k:r[p][k] for k in ('net','stress','net_delta_mean','stress_delta_mean','stress_delta_control')} for p in old.PAIRS}
        return output


def parents(c,dataset):
    result=roots()
    for row in c.execute("SELECT id,report FROM tree_nodes WHERE dataset=? AND status='complete' ORDER BY id",(dataset,)):
        result['node:'+str(row['id'])]=Spec(**json.loads(row['report'])['spec'])
    return result


def validate_proposal(value,available,seen):
    if not isinstance(value,dict) or set(value)!={'parent','spec','hypothesis'}: raise ValueError('Expected parent, spec, hypothesis')
    parent=value['parent']; hypothesis=value['hypothesis']
    if not isinstance(parent,str) or parent not in available: raise ValueError('Unknown parent')
    if not isinstance(hypothesis,str) or not hypothesis.strip() or len(hypothesis)>300: raise ValueError('Invalid hypothesis')
    raw=value['spec']
    if not isinstance(raw,dict) or set(raw)!={'features','method','transform','calibration'}: raise ValueError('Invalid spec fields')
    spec=Spec(**raw); previous=available[parent]
    if sum(getattr(spec,k)!=getattr(previous,k) for k in ('features','method','transform','calibration'))!=1:
        raise ValueError('Change exactly one modeling dimension from parent')
    if spec.identity in seen: raise ValueError('Experiment already registered or attempted')
    return parent,spec,hypothesis.strip()


def candidate_menu(agent, available, seen, context, limit=18):
    """Generate legal one-dimensional children, then diversify the short menu."""
    preferred={'alpha':'ridge_trend_only','beta':'ridge_expanded','gamma':'risk_long'}[agent]
    focus={'alpha':('momentum','distance','rsi','breakout'),
           'beta':FEATURES,'gamma':('volatility','volume_ratio')}[agent]
    recent=context.get('recent_nodes',{})
    legacy=context.get('legacy_measured',{})
    def score(name):
        markets=recent.get(name,{}).get('markets',legacy.get(name,{}))
        return min((m.get('stress',-1.) for m in markets.values()),default=-1.)
    # Keep the agent's anchor, a measured promising root, and recent children.
    names=[preferred]+sorted(recent,key=score,reverse=True)+sorted(available,key=score,reverse=True)
    names=list(dict.fromkeys(n for n in names if n in available))
    used=set(seen); menus=[]
    for name in names:
        p=available[name]; groups=[]
        def variants(field, values):
            options=[]
            for v in values:
                try:
                    s=Spec(**{**asdict(p),field:v})
                    if s.identity in used: continue
                    used.add(s.identity)
                    options.append((name,s,field))
                except ValueError: continue
            return options
        # Generate features from the parent, not a prewritten list of models.
        fs=p.features; feature_options=[]
        for k in fs:
            if len(fs)>1: feature_options.append(tuple(x for x in fs if x!=k))
        for k in fs:
            for w in WINDOWS:
                new=(k[0],w)
                if new not in fs: feature_options.append(tuple(new if x==k else x for x in fs))
        if len(fs)<12:
            for f in focus:
                for w in (24,72,168,672,6,12,48,120,336,504,720):
                    if (f,w) not in fs: feature_options.append(fs+((f,w),))
        groups_by_field={
            'features':feature_options,
            'method':[m for m in METHODS if m!=p.method],
            'transform':[v for v in TRANSFORMS if v!=p.transform],
            'calibration':[v for v in CALIBRATIONS if v!=p.calibration]}
        order=('method','features','calibration','transform') if agent=='beta' else ('features','method','calibration','transform')
        groups=[variants(field,groups_by_field[field]) for field in order]
        menu=[]
        for i in range(max(map(len,groups),default=0)):
            for group in groups:
                if i<len(group): menu.append(group[i])
        if menu: menus.append(menu)
    result=[]
    # Anchor gets a few diverse choices; other parents, including children,
    # get a turn before a single parent's many feature edits fill the prompt.
    for i in range(max(map(len,menus),default=0)):
        for menu in menus[:6]:
            if i<len(menu): result.append(menu[i])
            if len(result)>=limit: return result
    return result


def propose(model,agent,available,seen,context,reject):
    menu=candidate_menu(agent,available,seen,context)
    if not menu: raise ValueError('No novel one-dimensional proposals remain')
    def selected(index,hypothesis,source):
        parent,s,field=menu[index]
        return parent,s,hypothesis,source
    def default(source):
        parent,s,field=menu[0]
        return selected(0,f'Does changing {field} from {parent} improve stressed returns on both markets?',source)
    if model is None: return default('deterministic')
    choices=[{'choice':i,'parent':p,'change':field,'value':asdict(s)[field]}
             for i,(p,s,field) in enumerate(menu)]
    # Include measured evidence only for parents represented in the menu.
    relevant={p for p,_,_ in menu}
    evidence={k:v for k,v in context.get('legacy_measured',{}).items() if k in relevant}
    children={k:v for k,v in context.get('recent_nodes',{}).items() if k in relevant}
    prompt=(f'You are {agent}. Focus: {old.FOCUS[agent]}. Choose one numbered experiment. '
            'Every option is already validated and novel. Prefer improvement in stressed returns on both markets; '
            'negative returns are losses. Return only JSON with choice (integer) and hypothesis (a short question, no claimed results). '
            'Do not write a full specification. Example: {"choice":0,"hypothesis":"Does this change improve stressed returns?"}. '
            'Options: '+old.pack(choices)+' Measured parent evidence: '+old.pack({'roots':evidence,'children':children,'parent_specs':{p:asdict(available[p]) for p in sorted(relevant)}}))
    schema={'type':'object','properties':{'choice':{'type':'integer','minimum':0,'maximum':len(menu)-1},
            'hypothesis':{'type':'string','maxLength':300}},'required':['choice','hypothesis'],'additionalProperties':False}
    error='none'
    for attempt in range(2):
        try:
            response=request('http://127.0.0.1:11434/api/chat',{'model':model,'stream':False,'think':False,'format':schema if attempt==0 else 'json','messages':[{'role':'user','content':prompt+' Previous rejection: '+error}],'options':{'temperature':0,'num_ctx':8192,'num_predict':512}})
            raw=response['message']['content']; value=json.loads(raw)
            if not isinstance(value,dict) or set(value)!={'choice','hypothesis'}: raise ValueError('Expected choice and hypothesis')
            index=value['choice']; h=value['hypothesis']
            if type(index) is not int or not 0<=index<len(menu): raise ValueError('Invalid menu index')
            if not isinstance(h,str) or not h.strip() or len(h)>300: raise ValueError('Invalid hypothesis')
            return selected(index,h.strip(),'ollama')
        except (ValueError,TypeError,KeyError) as exc:
            error=str(exc)[:300]; reject(error)
    # Explicitly labeled fallback, never claim the model selected this experiment.
    return default('deterministic_fallback')


def run(path,parts,cutoff,legacy=None,model='qwen3:4b',rounds=1,budget=1,emit=None):
    if set(parts)!=set(old.PAIRS) or type(rounds) is not int or not 1<=rounds<=20 or type(budget) is not int or not 1<=budget<=3:
        raise ValueError('Both markets required; rounds 1..20, budget 1..3 per agent')
    if legacy and Path(path).resolve()==Path(legacy).resolve(): raise ValueError('Separate v0.17 ledger required')
    emit=emit or (lambda e:print(old.pack(e),flush=True))
    hashes={p:old.fingerprint(parts[p],p) for p in old.PAIRS}; dataset=lab.digest([PROTOCOL,cutoff,hashes])
    seed=legacy_evidence(legacy,hashes,cutoff)
    with exclusive(str(Path(path).resolve())+'.lock'),closing(connect(path)) as c:
        with c:
            for table in ('tree_runs','tree_nodes'):
                c.execute(f"UPDATE {table} SET status='interrupted' WHERE status='running'")
            record={'protocol':PROTOCOL,'dataset':dataset,'datasets':hashes,'cutoff':cutoff,'model':model,'completed':0,'rejected':0,'fallbacks':0,'legacy_evidence_loaded':bool(seed),'legacy_evidence':seed,'development_only':True,'eligible':False}
            rid=c.execute('INSERT INTO tree_runs(dataset,status,report) VALUES(?,?,?)',(dataset,'running',old.pack(record))).lastrowid
        record['run_id']=rid; state='complete'
        try:
            for _ in range(rounds):
                for agent in old.AGENTS:
                    for _ in range(budget):
                        available=parents(c,dataset)
                        seen={s.identity for s in available.values()}|{r[0] for r in c.execute('SELECT identity FROM tree_nodes WHERE dataset=?',(dataset,))}
                        recent={}
                        for row in c.execute("SELECT id,report FROM tree_nodes WHERE dataset=? AND status='complete' ORDER BY id DESC LIMIT 6",(dataset,)):
                            r=json.loads(row['report']); f=r['findings']; recent['node:'+str(row['id'])]={'classification':f['classification'],'markets':{p:{k:m[k] for k in ('net','stress','vs_parent','vs_mean','fills')} for p,m in f['markets'].items()}}
                        context={'legacy_measured':seed,'recent_nodes':recent,'warning':'Development only; no promotion. Net/stress are fractional returns. Negative is a loss.'}
                        def reject(reason):
                            with c: c.execute('INSERT INTO tree_rejections(run,agent,reason) VALUES(?,?,?)',(rid,agent,reason))
                            record['rejected']+=1; emit({'event':'rejected','agent':agent,'reason':reason})
                        try: parent,spec,hypothesis,source=propose(model,agent,available,seen,context,reject)
                        except ValueError as exc:
                            emit({'event':'no_proposal','agent':agent,'reason':str(exc)}); continue
                        if source=='deterministic_fallback':
                            record['fallbacks']+=1; emit({'event':'fallback','agent':agent,'reason':'Invalid Ollama choices; using validated deterministic candidate'})
                        r={'proposal_source':source,'spec':asdict(spec),'parent_spec':asdict(available[parent]),'hypothesis_unverified':hypothesis,'development_only':True,'eligible':False}
                        with c:
                            eid=c.execute('INSERT INTO tree_nodes(run,dataset,identity,parent,agent,status,report) VALUES(?,?,?,?,?,?,?)',(rid,dataset,spec.identity,parent,agent,'running',old.pack(r))).lastrowid
                        try:
                            emit({'event':'start','id':eid,'agent':agent,'parent':parent,'spec':asdict(spec),'proposal_source':source})
                            reports={p:evaluate(parts[p],p,cutoff,available[parent],spec) for p in old.PAIRS}
                            r.update(findings=facts(reports),results=reports)
                            with c:
                                c.execute("UPDATE tree_nodes SET status='complete',report=? WHERE id=?",(old.pack(r),eid)); record['completed']+=1
                                c.execute('UPDATE tree_runs SET report=? WHERE id=?',(old.pack(record),rid))
                            emit({'event':'complete','id':eid,'agent':agent,'findings':r['findings']})
                        except BaseException as exc:
                            r['error']=str(exc)
                            with c: c.execute('UPDATE tree_nodes SET status=?,report=? WHERE id=?',('interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',old.pack(r),eid))
                            raise
        except BaseException as exc:
            state='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed'; record['error']=str(exc); raise
        finally:
            if state=='complete' and record['completed']<rounds*budget*len(old.AGENTS):
                state='partial' if record['completed'] else 'no_valid_proposals'
            record['status']=state
            with c: c.execute('UPDATE tree_runs SET status=?,report=? WHERE id=?',(state,old.pack(record),rid))
        emit({'event':'finished',**record}); return record


def status(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        runs=[dict(json.loads(raw),run_id=i,status=s) for i,s,raw in c.execute('SELECT id,status,report FROM tree_runs ORDER BY id DESC LIMIT 20')]
        nodes=[]
        for i,d,p,a,s,raw in c.execute('SELECT id,dataset,parent,agent,status,report FROM tree_nodes ORDER BY id DESC LIMIT 200'):
            r=json.loads(raw); r.pop('results',None); nodes.append(dict(r,id=i,dataset=d,parent=p,agent=a,status=s))
        rejected=[{'run':r,'agent':a,'reason':s} for r,a,s in c.execute('SELECT run,agent,reason FROM tree_rejections ORDER BY id DESC LIMIT 30')]
    return {'protocol':PROTOCOL,'runs':runs,'nodes':nodes,'rejections':rejected,'development_only':True,'eligible':False}


def render(data):
    def cell(v): return '<td>'+escape(str(v),quote=True)+'</td>'
    def pct(v): return '—' if v is None else f'{100*v:+.3f}%'
    rows=[]
    for n in data['nodes']:
        f=n.get('findings',{}); m=f.get('markets',{}); b=m.get('XBTUSD',{}); e=m.get('ETHUSD',{})
        rows.append('<tr>'+''.join(cell(v) for v in (n['id'],n['parent'],n['agent'],n['status'],n['spec'],pct(b.get('net')),pct(b.get('stress')),pct(e.get('net')),pct(e.get('stress')),f.get('classification','—'),n.get('hypothesis_unverified','')) )+'</tr>')
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="15"><title>Autarky Research Tree</title><style>body{font:15px system-ui;background:#101820;color:#eee;padding:24px}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #456;text-align:left;vertical-align:top}a{color:#9df}</style><h1>Autarky / Research Tree</h1><p>Development only · no promotion · <a href="/api/status">JSON status</a></p><table><tr><th>ID</th><th>Parent</th><th>Agent</th><th>Status</th><th>Specification</th><th>BTC net</th><th>BTC stress</th><th>ETH net</th><th>ETH stress</th><th>Measured finding</th><th>Unverified hypothesis</th></tr>'+''.join(rows)+'</table>'


def serve(path,port):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/','/api/status'): self.send_error(404); return
            try: data=status(path); body=(old.pack(data) if self.path=='/api/status' else render(data)).encode()
            except (OSError,sqlite3.Error,ValueError): self.send_error(503); return
            self.send_response(200); self.send_header('Content-Type','application/json' if self.path=='/api/status' else 'text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,*_): pass
    with ThreadingHTTPServer(('127.0.0.1',port),Handler) as server:
        print(f'Research tree: http://127.0.0.1:{port}',flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',required=True); sub=p.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('run'); a.add_argument('--btc',required=True); a.add_argument('--eth',required=True); a.add_argument('--cutoff',required=True,type=int); a.add_argument('--legacy'); a.add_argument('--model',default='qwen3:4b'); a.add_argument('--deterministic',action='store_true'); a.add_argument('--rounds',type=int,default=1); a.add_argument('--budget',type=int,default=1)
    sub.add_parser('status'); d=sub.add_parser('dashboard'); d.add_argument('--port',type=int,default=8767); x=p.parse_args()
    if x.cmd=='run':
        if len({Path(y).resolve() for y in (x.db,x.btc,x.eth)})!=3: p.error('Use separate data files and research database')
        run(x.db,{'XBTUSD':multiyear.segments(x.btc,x.cutoff),'ETHUSD':multiyear.segments(x.eth,x.cutoff)},x.cutoff,x.legacy,None if x.deterministic else x.model,x.rounds,x.budget)
    elif x.cmd=='status': print(json.dumps(status(x.db),indent=2,allow_nan=False))
    else: serve(x.db,x.port)

if __name__=='__main__': main()

"""Bounded, multi-asset autonomous quantitative research; never trades or promotes."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time

from . import lab, model_discovery as discovery, multiyear, weekly_models as wm
from .coordinator import exclusive
from .core import request

PROTOCOL = 'v0.16-autonomous-1'
PAIRS = ('XBTUSD', 'ETHUSD')
AGENTS = ('alpha', 'beta', 'gamma')
FOCUS = {
    'alpha': 'Market regimes, price trends, and causal feature ablation.',
    'beta': 'Forecast model design and robustness against weak benchmarks.',
    'gamma': 'Volatility, volume, and economically useful selectivity.',
}


@dataclass(frozen=True)
class Experiment:
    name: str
    agent: str
    control: str
    features: tuple
    method: str
    hypothesis: str


# Each experiment changes one modeling choice relative to its control.
EXPERIMENTS = (
    Experiment('momentum_only', 'alpha', 'ridge_trend_only',
               (('momentum', 24), ('momentum', 168), ('momentum', 672)), 'ridge',
               'Is momentum alone more useful than the full trend group?'),
    Experiment('distance_only', 'alpha', 'ridge_trend_only',
               (('distance', 168), ('distance', 672)), 'ridge',
               'Does price distance from its average carry the trend-group signal?'),
    Experiment('long_trend_only', 'alpha', 'ridge_trend_only',
               (('momentum', 672), ('distance', 672)), 'ridge',
               'Do slower trend features outperform the mixed-horizon trend group?'),
    Experiment('risk_similarity', 'beta', 'ridge_risk_only', discovery.RISK, 'similarity',
               'Does nonlinear local averaging improve the same risk features?'),
    Experiment('robust_expanded', 'beta', 'ridge_expanded', wm.SPECS['ridge_expanded'], 'robust',
               'Does clipping extreme training targets improve expanded ridge?'),
    Experiment('strong_shrinkage', 'beta', 'ridge_expanded', wm.SPECS['ridge_expanded'], 'shrink',
               'Does stronger regularization improve expanded ridge?'),
    Experiment('volatility_only', 'gamma', 'ridge_risk_only',
               (('volatility', 24), ('volatility', 168)), 'ridge',
               'Does removing volume improve the risk-only model?'),
    Experiment('volume_only', 'gamma', 'ridge_risk_only',
               (('volume_ratio', 24), ('volume_ratio', 168)), 'ridge',
               'Does removing volatility improve the risk-only model?'),
    Experiment('risk_long', 'gamma', 'ridge_risk_only',
               (('volatility', 168), ('volatility', 672), ('volume_ratio', 168), ('volume_ratio', 672)), 'ridge',
               'Does replacing the 24-hour risk horizon with a longer horizon improve forecasts?'),
)


def pack(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(',', ':'))


def fingerprint(parts, pair):
    return lab.digest([pair, [[asdict(b) for b in segment] for segment in parts]])


def fit_candidate(exp, rows):
    if exp.method == 'similarity':
        return discovery.fit_model('similarity', rows, exp.features)
    if exp.method == 'robust':
        # Quantiles derived from training targets only. Evaluation labels stay unmodified.
        ys = sorted(r['realized'] for r in rows)
        lo, hi = ys[math.floor(.05 * (len(ys)-1))], ys[math.ceil(.95 * (len(ys)-1))]
        rows = [dict(r, realized=max(lo, min(hi, r['realized']))) for r in rows]
    if exp.method == 'shrink':
        # Use the same training-only scaling and normal equations with penalty 4.
        from .models import solve
        keys = exp.features
        y = [r['realized'] for r in rows]; mu = statistics.mean(y)
        cols = [[r['features'][k] for r in rows] for k in keys]
        centers = [statistics.mean(c) for c in cols]
        scales = [max(1e-8, statistics.pstdev(c)) for c in cols]
        z = [[(v-m)/s for v,m,s in zip(row,centers,scales)] for row in zip(*cols)]
        n, d = len(y), len(keys)
        a = [[sum(row[j]*row[k] for row in z)/n + (4. if j==k else 0.) for k in range(d)] for j in range(d)]
        b = [sum(row[j]*(v-mu) for row,v in zip(z,y))/n for j in range(d)]
        weights = solve(a,b)
        return lambda x: mu + sum(w*(x[k]-m)/s for w,k,m,s in zip(weights,keys,centers,scales))
    if exp.method not in ('ridge', 'robust'):
        raise ValueError('Unknown candidate method')
    return wm.fit(rows, exp.features)


def experiment(parts, pair, cutoff, exp):
    specs = {'mean': (), exp.control: discovery.SPECS[exp.control], exp.name: exp.features}
    def fitter(name, rows, spec):
        return fit_candidate(exp, rows) if name == exp.name else discovery.fit_model(name, rows, spec)
    report = wm.review(parts, pair, cutoff, specs=specs, fitter=fitter)
    m = report['summary']; post = report['post_2017']
    def delta(base):
        return {k: m[exp.name][k] - m[base][k] for k in ('mean_net', 'mean_stress', 'rmse')}
    report.update(experiment=asdict(exp), delta_vs_mean=delta('mean'), delta_vs_control=delta(exp.control),
                  post_2017_delta_vs_mean={k: post[exp.name][k]-post['mean'][k] for k in ('mean_net','mean_stress','rmse')} if post else None)
    return report


def connect(path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30); c.row_factory = sqlite3.Row
    try:
        with c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables - {'model_runs', 'model_experiments', 'model_baselines'}:
                raise ValueError('Use a separate autonomous research database')
            c.execute('CREATE TABLE IF NOT EXISTS model_runs(id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, created INTEGER NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS model_baselines(dataset TEXT PRIMARY KEY, report TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS model_experiments(id INTEGER PRIMARY KEY, run INTEGER NOT NULL REFERENCES model_runs(id), dataset TEXT NOT NULL, name TEXT NOT NULL, agent TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL, UNIQUE(dataset,name))')
        return c
    except BaseException:
        c.close(); raise


def recover(c):
    with c:
        for table in ('model_runs', 'model_experiments'):
            for row in c.execute(f"SELECT id,report FROM {table} WHERE status='running'").fetchall():
                report = json.loads(row['report']); report['error'] = 'Previous process ended before finalizing this record'
                if table == 'model_runs':
                    report['completed'] = c.execute("SELECT count(*) FROM model_experiments WHERE run=? AND status='complete'", (row['id'],)).fetchone()[0]
                report['status'] = 'interrupted'
                c.execute(f"UPDATE {table} SET status='interrupted',report=? WHERE id=?", (pack(report),row['id']))


def baseline(c, dataset, parts, cutoff):
    row = c.execute('SELECT report FROM model_baselines WHERE dataset=?', (dataset,)).fetchone()
    if row:
        return json.loads(row[0])
    result = {}
    for pair in PAIRS:
        report = discovery.evaluate(parts[pair], pair, cutoff)
        result[pair] = {k: report[k] for k in ('protocol','dataset_sha256','windows','summary','post_2017','paired_vs_mean')}
    with c:
        c.execute('INSERT INTO model_baselines VALUES(?,?)', (dataset,pack(result)))
    return result


def evidence(c, dataset, base):
    def metrics(m):
        return {k: round(m[k],6) for k in ('mean_net','mean_stress','rmse','fills')}
    recent = []
    rows = c.execute("SELECT name,agent,report FROM model_experiments WHERE dataset=? AND status='complete' ORDER BY id DESC LIMIT 9", (dataset,))
    for row in rows:
        report = json.loads(row['report'])
        recent.append({'experiment':row['name'],'agent':row['agent'],'results':report['summary']})
    return {'baseline':{p:{name:metrics(m) for name,m in base[p]['summary'].items()} for p in PAIRS},
            'recent_experiments':recent, 'warning':'All observations are development data; no profit or promotion claims.'}


def choose(model, agent, options, context):
    if not options:
        raise ValueError('No experiment options')
    if model is None:
        return options[0], 'Deterministic test mode: first available registered hypothesis.'
    menu = [{'choice':i, **asdict(exp)} for i,exp in enumerate(options)]
    prompt = (f'You are {agent}, an autonomous quantitative researcher. Focus: {FOCUS[agent]} '
              'Choose ONE untested registered experiment. Both markets will be evaluated against its parent and a historical mean. '
              'Prefer falsifiable hypotheses and stressed returns; cash abstention is not predictive skill. '
              'Never claim a trading edge or invent results. Return JSON with integer choice and short reason. Evidence: '
              + pack(context) + ' Menu: ' + pack(menu))
    schema = {'type':'object','properties':{'choice':{'type':'integer','minimum':0,'maximum':len(options)-1},'reason':{'type':'string','maxLength':240}},'required':['choice','reason'],'additionalProperties':False}
    errors = []
    for attempt in range(3):
        payload = {'model':model,'stream':False,'think':False,'format':schema if attempt==0 else 'json',
                   'messages':[{'role':'user','content':prompt if attempt<2 else prompt+' Return only a valid JSON choice index if necessary.'}],
                   'options':{'temperature':0,'num_predict':2048,'num_ctx':8192}}
        try:
            raw = request('http://127.0.0.1:11434/api/chat', payload)['message']['content']
            if not isinstance(raw,str): raise ValueError('Non-text response')
            body=raw.strip()
            if body.startswith('```') and body.endswith('```'):
                body=body[3:-3].strip()
                if body.startswith('json'): body=body[4:].strip()
            value=json.loads(body)
            if not isinstance(value,dict): raise ValueError('Expected JSON object')
            index=value.get('choice')
            if isinstance(index,str) and index.isascii() and index.isdecimal(): index=int(index)
            if type(index) is not int or not 0<=index<len(options): raise ValueError('Invalid choice index')
            reason=value.get('reason')
            if not isinstance(reason,str) or not reason.strip():
                if attempt<2: raise ValueError('Missing rationale')
                reason='Model selected this registered experiment without a returned rationale.'
            return options[index],reason.strip()[:500]
        except (ValueError,TypeError,KeyError) as exc:
            errors.append(str(exc))
    raise ValueError('Ollama returned no valid experiment choice: '+'; '.join(errors))


def compact(exp, reports):
    result = {}
    for pair, r in reports.items():
        m=r['summary'][exp.name]
        result[pair] = {'windows':m['windows'],'net':m['mean_net'],'stress':m['mean_stress'],
                        'rmse':m['rmse'],'fills':m['fills'],'drawdown':m['worst_drawdown'],
                        'net_delta_mean':r['delta_vs_mean']['mean_net'],
                        'stress_delta_mean':r['delta_vs_mean']['mean_stress'],
                        'net_delta_control':r['delta_vs_control']['mean_net'],
                        'stress_delta_control':r['delta_vs_control']['mean_stress'],
                        'post_2017_delta_mean':r['post_2017_delta_vs_mean']}
    return result


def run(path, parts, cutoff, model='qwen3:4b', budget=1, rounds=1, pause=0, emit=None):
    if set(parts)!=set(PAIRS) or any(not v for v in parts.values()): raise ValueError('Both BTC and ETH histories required')
    if type(budget) is not int or not 1<=budget<=3 or type(rounds) is not int or rounds<0 or pause<0:
        raise ValueError('Budget 1..3 per agent, rounds >=0, pause >=0')
    emit = emit or (lambda event: print(pack(event),flush=True))
    hashes = {p:fingerprint(parts[p],p) for p in PAIRS}
    dataset = lab.digest([PROTOCOL,cutoff,hashes,[asdict(e) for e in EXPERIMENTS]])
    with exclusive(str(Path(path).resolve())+'.lock'), closing(connect(path)) as c:
        recover(c)
        report={'protocol':PROTOCOL,'dataset':dataset,'datasets':hashes,'cutoff':cutoff,'model':model,'budget':budget,'rounds':rounds,'completed':0,'development_only':True,'eligible':False}
        with c:
            rid=c.execute('INSERT INTO model_runs(dataset,created,status,report) VALUES(?,?,?,?)',(dataset,int(time.time()),'running',pack(report))).lastrowid
        report['run_id']=rid; status='complete'
        try:
            emit({'event':'baseline','run':rid,'dataset':dataset,'cached':bool(c.execute('SELECT 1 FROM model_baselines WHERE dataset=?',(dataset,)).fetchone())})
            base=baseline(c,dataset,parts,cutoff)
            n=0
            while rounds==0 or n<rounds:
                n+=1; progressed=False
                for agent in AGENTS:
                    for _ in range(budget):
                        seen={row[0] for row in c.execute('SELECT name FROM model_experiments WHERE dataset=?',(dataset,))}
                        options=[e for e in EXPERIMENTS if e.agent==agent and e.name not in seen]
                        if not options: break
                        exp,reason=choose(model,agent,options,evidence(c,dataset,base))
                        record={'experiment':asdict(exp),'reason':reason,'model':model,'development_only':True,'eligible':False}
                        with c:
                            eid=c.execute('INSERT INTO model_experiments(run,dataset,name,agent,status,report) VALUES(?,?,?,?,?,?)',(rid,dataset,exp.name,agent,'running',pack(record))).lastrowid
                        try:
                            emit({'event':'start','run':rid,'experiment_id':eid,'agent':agent,'experiment':exp.name,'reason':reason})
                            results={pair:experiment(parts[pair],pair,cutoff,exp) for pair in PAIRS}
                            record.update(results=results,summary=compact(exp,results))
                            with c:
                                c.execute("UPDATE model_experiments SET status='complete',report=? WHERE id=?",(pack(record),eid))
                                report['completed']+=1
                                c.execute('UPDATE model_runs SET report=? WHERE id=?',(pack(report),rid))
                        except BaseException as exc:
                            record['error']=str(exc)
                            state='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed'
                            with c: c.execute('UPDATE model_experiments SET status=?,report=? WHERE id=?',(state,pack(record),eid))
                            raise
                        progressed=True
                        emit({'event':'complete','experiment_id':eid,'agent':agent,'experiment':exp.name,'summary':record['summary']})
                if not progressed:
                    status='exhausted'; break
                if c.execute('SELECT count(*) FROM model_experiments WHERE dataset=?',(dataset,)).fetchone()[0]>=len(EXPERIMENTS):
                    status='exhausted'; break
                if (rounds==0 or n<rounds) and pause: time.sleep(pause)
        except BaseException as exc:
            status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed'; report['error']=str(exc)
            raise
        finally:
            report['status']=status
            with c: c.execute('UPDATE model_runs SET status=?,report=? WHERE id=?',(status,pack(report),rid))
        emit({'event':'finished',**report})
    return report


def status(path):
    """Read-only status; never create or update a database."""
    uri=Path(path).resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True,timeout=10)) as c:
        runs=[dict(json.loads(raw),run_id=rid,status=state) for rid,state,raw in c.execute('SELECT id,status,report FROM model_runs ORDER BY id DESC LIMIT 20')]
        experiments=[]
        for eid,dataset,name,agent,state,raw in c.execute('SELECT id,dataset,name,agent,status,report FROM model_experiments ORDER BY id DESC LIMIT 100'):
            r=json.loads(raw)
            experiments.append({'id':eid,'dataset':dataset,'name':name,'agent':agent,'status':state,'reason':r.get('reason'),'summary':r.get('summary'),'error':r.get('error')})
        base=c.execute('SELECT report FROM model_baselines WHERE dataset=?',(runs[0]['dataset'],)).fetchone() if runs else None
    return {'protocol':PROTOCOL,'development_only':True,'eligible':False,'runs':runs,'experiments':experiments,'baseline':json.loads(base[0]) if base else None}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',required=True)
    sub=p.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('run'); a.add_argument('--btc',required=True); a.add_argument('--eth',required=True); a.add_argument('--cutoff',required=True,type=int)
    a.add_argument('--model',default='qwen3:4b'); a.add_argument('--deterministic',action='store_true'); a.add_argument('--budget',type=int,default=1); a.add_argument('--rounds',type=int,default=1); a.add_argument('--pause',type=int,default=0)
    sub.add_parser('status'); d=sub.add_parser('dashboard'); d.add_argument('--port',type=int,default=8766)
    x=p.parse_args()
    if x.cmd=='run':
        if Path(x.btc).resolve()==Path(x.eth).resolve(): p.error('BTC and ETH must use separate files')
        if Path(x.db).resolve() in (Path(x.btc).resolve(),Path(x.eth).resolve()): p.error('Separate research database required')
        parts={'XBTUSD':multiyear.segments(x.btc,x.cutoff),'ETHUSD':multiyear.segments(x.eth,x.cutoff)}
        run(x.db,parts,x.cutoff,None if x.deterministic else x.model,x.budget,x.rounds,x.pause)
    elif x.cmd=='status': print(json.dumps(status(x.db),indent=2,allow_nan=False))
    else:
        from .research_dashboard import serve
        serve(x.db,x.port)


if __name__=='__main__': main()

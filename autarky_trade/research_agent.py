"""Bounded, evidence-driven development research. No order or promotion capability."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import statistics
import time

from . import lab
from .core import request

PROTOCOL = 'v0.13-research-1'
SEEDS = {'alpha': 'trend-volatility', 'beta': 'reversion-recovery', 'gamma': 'breakout-volume'}
METRICS = ('mean_net', 'mean_gross', 'mean_stress', 'mean_passive', 'mean_turnover',
           'mean_cost_drag', 'worst_drawdown', 'positive_windows', 'fills', 'diagnosis')


def quantile(values, q):
    v = sorted(values)
    if not v:
        return None
    x = q * (len(v) - 1)
    lo = int(x)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (x - lo)


def signal_analysis(bars, feature, window, horizon=24):
    """Freeze quantile boundaries on warmup; evaluate nonoverlapping future labels in development."""
    if feature not in lab.FEATURES or feature == 'forecast' or type(window) is not int or not 2 <= window < lab.WARMUP - 1:
        raise ValueError('Invalid signal specification')
    if type(horizon) is not int or not 2 <= horizon <= 168:
        raise ValueError('Invalid analysis horizon')
    values = lab.Features(bars).series(feature, window)
    warm = [v for v in values[:lab.WARMUP] if v is not None]
    cuts = sorted(set(quantile(warm, q) for q in (.2, .4, .6, .8))) if warm else []
    buckets = [[] for _ in range(len(cuts) + 1)]
    for i in range(lab.WARMUP, len(bars) - horizon, horizon):
        v = values[i]
        if v is not None:
            k = sum(v > cut for cut in cuts)
            buckets[k].append(bars[i+horizon].open / bars[i].open - 1)
    return {'feature': feature, 'window': window, 'horizon': horizon,
            'warmup_end': bars[lab.WARMUP-1].ts, 'boundaries': cuts,
            'buckets': [{'count': len(b), 'mean_return': statistics.mean(b) if b else None,
                         'positive_fraction': sum(x > 0 for x in b)/len(b) if b else None} for b in buckets],
            'note': 'Development only; nonoverlapping open-to-open labels; boundaries from warmup only.'}


def forecast_analysis(bars, folds):
    f = lab.Features(bars)
    bounds = [lab.WARMUP + (len(bars)-lab.WARMUP)*i//folds for i in range(folds+1)]
    forecasts = []
    windows = []
    spec = {'horizon': 24, 'window': 72, 'ridge': 1.0}
    for start, end in zip(bounds, bounds[1:]):
        p, artifact = lab.forecast_fold(bars, f, spec, start, end)
        forecasts.extend(p[i] for i in range(start, end, 24))
        windows.append(lab.forecast_diagnostics(bars, p, start, end, 24))
    return {'spec': spec, 'quantiles': {str(q): quantile(forecasts,q) for q in (.1,.5,.9,.99)},
            'threshold_coverage': {str(t): sum(v>t for v in forecasts)/len(forecasts) for t in (0,.002,.004,.006,.012)},
            'windows': windows, 'note': 'Out-of-fold development forecasts, not independent promotion evidence.'}


def summary(result):
    return {'policy': result['policy'], 'identity': result['identity'], **{k:result[k] for k in METRICS}}


def compare(control, variant):
    a,b=control,variant
    assert a['policy']['fraction']==b['policy']['fraction']==.25
    return {'net_delta': b['mean_net']-a['mean_net'], 'gross_delta': b['mean_gross']-a['mean_gross'],
            'stress_delta': b['mean_stress']-a['mean_stress'], 'turnover_delta': b['mean_turnover']-a['mean_turnover'],
            'fills_delta': b['fills']-a['fills'], 'drawdown_delta': b['worst_drawdown']-a['worst_drawdown'],
            'window_net_deltas': [y['normal']['net_return']-x['normal']['net_return'] for x,y in zip(a['windows'],b['windows'])],
            'interpretation': 'no activity' if not b['fills'] else 'improved net and stressed return' if b['mean_net']>a['mean_net'] and b['mean_stress']>a['mean_stress'] else 'trade-off or no joint improvement',
            'note': 'Matched development comparison; not independent evidence.'}


def variants(p):
    """One executable change per candidate; no sizing changes or arbitrary code."""
    out=[]
    def add(change, **kwargs):
        try:
            v=replace(p, **kwargs)
            if v.identity != p.identity and all(x[1].identity != v.identity for x in out):
                out.append((change, v))
        except ValueError:
            pass
    add('increase minimum hold', min_hold=min(p.max_hold-1,max(12,p.min_hold*2)))
    add('increase cooldown', cooldown=min(720,max(12,p.cooldown*2)))
    add('increase maximum hold', max_hold=min(720,p.max_hold*2))
    for side in ('entry','exit'):
        rules=getattr(p,side)
        for i,r in enumerate(rules):
            if r.feature=='forecast':
                values=(.002,.004,.006,.012) if side=='entry' else (-.002,-.004)
            else:
                # Also allow loosening no-trade rules. Use a meaningful additive step at zero.
                step={'rsi':5,'volume_ratio':.15,'volatility':.002}.get(r.feature,.005)
                values=(r.threshold-step,r.threshold+step)
            for t in values:
                try:
                    rr=replace(r,threshold=round(t,8))
                    rs=rules[:i]+(rr,)+rules[i+1:]
                    add(f'{side} {r.feature}({r.window}) {r.op} threshold {r.threshold:g} -> {t:g}', **{side:rs})
                except ValueError:
                    continue
    return out


def entry_coverage(bars, policy):
    """Joint and per-rule coverage over the development decisions (not a holdout)."""
    if policy.predictor:
        return None
    f=lab.Features(bars); cols=[f.series(r.feature,r.window) for r in policy.entry]
    counts=[0]*len(cols); joint=0; observations=0; almost=[0]*len(cols)
    for i in range(lab.WARMUP,len(bars)):
        passed=[col[i] is not None and (col[i]>r.threshold if r.op=='gt' else col[i]<r.threshold) for r,col in zip(policy.entry,cols)]
        observations+=1; joint+=all(passed)
        for j,ok in enumerate(passed):
            counts[j]+=ok
            almost[j]+=all(passed[k] for k in range(len(passed)) if k!=j) and not ok
    return {'observations':observations,'joint':joint,'rules':[{'rule':asdict(r),'passes':counts[j],'blocked_only_by_this':almost[j]} for j,r in enumerate(policy.entry)]}


def ranked_variants(policy, metrics, bars, prior):
    """Prioritize causal fixes; preserve exploration, never use future promotion data."""
    choices=variants(policy)
    if not metrics['fills']:
        # Holding/cooldown cannot create the first entry. For an ALL entry, loosening
        # a threshold cannot remove a qualifying observation. Check actual coverage.
        options=[]
        base=entry_coverage(bars,policy)
        for change,v in choices:
            if not change.startswith('entry '): continue
            if policy.predictor:
                # The shared predictor has only a single entry rule.
                if v.entry[0].threshold<policy.entry[0].threshold: options.append((0,change,v))
                continue
            cov=entry_coverage(bars,v)
            if cov['joint']>base['joint']: options.append((cov['joint'],change,v))
        # If none of the one-step revisions activates the conjunction, try looser
        # thresholds explicitly (still one changed rule), not an unrelated exit.
        if not options and not policy.predictor:
            for i,r in enumerate(policy.entry):
                for scale in (2,4,8):
                    step={'rsi':5,'volume_ratio':.15,'volatility':.002}.get(r.feature,.005)*scale
                    try:
                        rr=replace(r,threshold=r.threshold-step if r.op=='gt' else r.threshold+step)
                        v=replace(policy,entry=policy.entry[:i]+(rr,)+policy.entry[i+1:])
                        cov=entry_coverage(bars,v)
                        if cov['joint']>base['joint']: options.append((cov['joint'],f'loosen entry {r.feature}({r.window}) {r.threshold:g} -> {rr.threshold:g}',v))
                    except ValueError: continue
        # Prefer activating some opportunities without opening the floodgates.
        return [(change,v) for _,change,v in sorted(options,key=lambda x:x[0])]
    # Repeated failures in a direction reduce its priority; they do not outlaw exploration.
    losses={}
    for t in prior:
        if t['control']['policy']['name']!=policy.name: continue
        key=t['change'].split(' threshold ')[0]
        if t['comparison']['net_delta']<=0 and t['comparison']['stress_delta']<=0:
            losses[key]=losses.get(key,0)+1
    return sorted(choices,key=lambda x:(losses.get(x[0].split(' threshold ')[0],0),
        0 if metrics['mean_gross']>0 and metrics['mean_net']<=0 and ('hold' in x[0] or 'cooldown' in x[0]) else 1))


def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path,timeout=30)
    c.row_factory=sqlite3.Row
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables-{'sessions','trials'}:
                raise ValueError('Use a separate research memory database')
            c.execute('''CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, created INTEGER NOT NULL,
              dataset TEXT NOT NULL, folds INTEGER NOT NULL, agent TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS trials(id INTEGER PRIMARY KEY, session INTEGER NOT NULL,
              dataset TEXT NOT NULL, folds INTEGER NOT NULL, control TEXT NOT NULL, variant TEXT NOT NULL,
              status TEXT NOT NULL, report TEXT NOT NULL, UNIQUE(dataset,folds,control,variant))''')
        return c
    except BaseException:
        c.close(); raise


def memory(c, dataset, folds):
    rows=c.execute("SELECT t.report,s.agent FROM trials t JOIN sessions s ON s.id=t.session WHERE t.dataset=? AND t.folds=? AND t.status='complete' ORDER BY t.id DESC LIMIT 40",(dataset,folds)).fetchall()
    return [dict(json.loads(r['report']),agent=r['agent']) for r in rows]


def own_history(c,dataset,folds,agent):
    """Champion selection must not forget older results when prompt memory rolls over."""
    rows=c.execute("SELECT t.report FROM trials t JOIN sessions s ON s.id=t.session WHERE t.dataset=? AND t.folds=? AND t.status='complete' AND s.agent=? ORDER BY t.id DESC",(dataset,folds,agent))
    return [json.loads(r[0]) for r in rows]


def choose(model, agent, options, evidence):
    if not model:
        return options[0], 'Deterministic control: investigate the first untested revision.'
    # Keep the grammar simple for small models; validate the menu index locally.
    schema={'type':'object','properties':{'choice':{'type':'integer','minimum':0,'maximum':len(options)-1},'reason':{'type':'string','maxLength':240}},'required':['choice','reason'],'additionalProperties':False}
    menu=[{'choice':i,'change':x['change'],'control':x['control'].identity[:12]} for i,x in enumerate(options)]
    context='You are '+agent+', directing development-only trading research. Choose one matched experiment from the menu based on measured evidence. Prefer cost efficiency, activity, stressed returns and risk; no profit claims. Do not invent evidence or change allocation. Evidence: '+json.dumps(evidence,separators=(',',':'))+' Menu: '+json.dumps(menu,separators=(',',':'))
    instruction=' Return ONLY a JSON object with choice (zero-based integer menu index) and reason (one short falsifiable sentence, at most 240 characters). Example: {"choice":0,"reason":"Test whether this revision reduces turnover."}'
    errors=[]
    for attempt in range(3):
        # Retry with JSON mode rather than the same schema; last attempt only needs a valid choice.
        prompt=context+instruction if attempt<2 else context+' Return ONLY JSON with the chosen zero-based integer index, e.g. {"choice":0}. No explanation needed.'
        payload={'model':model,'stream':False,'think':False,'format':schema if attempt==0 else 'json','options':{'temperature':0,'num_predict':2048 if attempt==0 else 4096,'num_ctx':8192},'messages':[{'role':'user','content':prompt}]}
        response=None; raw=None
        try:
            response=request('http://127.0.0.1:11434/api/chat',payload)
            raw=response['message']['content']
            if not isinstance(raw,str): raise ValueError('message.content is not text')
            body=raw.strip()
            if body.startswith('```') and body.endswith('```'):
                body=body[3:-3].strip()
                if body.startswith('json'): body=body[4:].strip()
            obj=json.loads(body)
            if not isinstance(obj,dict): raise ValueError('expected a JSON object')
            index=obj.get('choice')
            if isinstance(index,str) and index.isascii() and index.isdecimal(): index=int(index)
            if type(index) is not int or not 0<=index<len(options): raise ValueError('choice is not a valid menu index')
            reason=obj.get('reason')
            if not isinstance(reason,str) or not reason.strip():
                if attempt<2: raise ValueError('missing reason')
                reason='Model selected this experiment; no rationale returned (choice-only retry).'
            return options[index],reason.strip()[:500]
        except (ValueError,TypeError,KeyError) as exc:
            meta=response if isinstance(response,dict) else {}
            errors.append(f'attempt {attempt+1}: {exc}; done_reason={meta.get("done_reason")}; eval_count={meta.get("eval_count")}; content={repr(raw)[:300]}')
    raise ValueError('Ollama did not return a valid experiment choice: '+' | '.join(errors))


def session(path,bars,pair,cutoff,agent,model=None,budget=5,folds=6):
    if agent not in SEEDS or type(budget) is not int or not 1<=budget<=20:
        raise ValueError('Agent must be alpha/beta/gamma; budget 1..20 matched trials')
    seed=next(p for p in lab.defaults() if p.name==SEEDS[agent])
    ridge=next(p for p in lab.defaults() if p.name=='ridge-24-sign')
    # Baseline checks history, data bounds, and chronological model fitting before any model call.
    base=lab.evaluate(bars,pair,cutoff,[seed,ridge],folds=folds)
    dataset=base['dataset_sha256']
    diagnostics={'signals':[signal_analysis(bars,r.feature,r.window) for r in seed.entry], 'forecast':forecast_analysis(bars,folds)}
    record={'protocol':PROTOCOL,'agent':agent,'model':model,'budget':budget,'pair':pair,'cutoff':cutoff,
            'dataset_sha256':dataset,'folds':folds,'bars':len(bars),'development_only':True,'eligible':False,
            'baseline':[summary(x) for x in base['results']],'diagnostics':diagnostics}
    with closing(connect(path)) as c:
        with c:
            sid=c.execute('INSERT INTO sessions(created,dataset,folds,agent,status,report) VALUES(?,?,?,?,?,?)',(int(time.time()),dataset,folds,agent,'running',json.dumps(record))).lastrowid
        record['session_id']=sid
        completed=[]; status='complete'
        try:
            for _ in range(budget):
                prior=memory(c,dataset,folds)
                own=[x for x in own_history(c,dataset,folds,agent) if x['variant']['fills']]
                # Best measured active variant, with original seed always retained as a control.
                own.sort(key=lambda x:(x['variant']['mean_net'],x['variant']['mean_stress']),reverse=True)
                controls=[seed,ridge]
                if own:
                    p=lab.Policy(**own[0]['variant']['policy'])
                    if p.identity not in {v.identity for v in controls}: controls.insert(0,p)
                seen={(r[0],r[1]) for r in c.execute('SELECT control,variant FROM trials WHERE dataset=? AND folds=?',(dataset,folds))}
                metrics={x['identity']:x for x in record['baseline']}
                metrics.update({x['variant']['identity']:x['variant'] for x in own})
                # The champion includes the seed; an inferior exploration control does not replace it.
                champions=[x for x in metrics.values() if x['policy']['name']==seed.name and x['fills']]
                champion=max(champions,key=lambda x:(x['mean_net'],x['mean_stress'])) if champions else metrics[seed.identity]
                record['champion']=champion
                menus=[]
                for p in controls:
                    choices=ranked_variants(p,metrics[p.identity],bars,prior)
                    menus.append([{'control':p,'variant':v,'change':change} for change,v in choices if (p.identity,v.identity) not in seen])
                # Round-robin to give signal, exit and predictor research a chance.
                options=[]
                for i in range(max(map(len,menus),default=0)):
                    for menu in menus:
                        if i<len(menu): options.append(menu[i])
                options=options[:12]
                if not options:
                    status='exhausted'; break
                evidence={'baseline':record['baseline'],'diagnostics':diagnostics,'controls':[{'id':p.identity[:12],'policy':p.description()} for p in controls],
                          'champion':champion,'entry_coverage':entry_coverage(bars,seed),'recent_shared_trials':[{'agent':x['agent'],'change':x['change'],'control':x['control']['identity'][:12],
                            'variant':x['variant']['identity'][:12],'metrics':{k:x['variant'][k] for k in METRICS},
                            'comparison':x['comparison']} for x in prior[:4]]}
                selected,reason=choose(model,agent,options,evidence)
                p,v=selected['control'],selected['variant']
                attempt={'change':selected['change'],'reason':reason,'control_policy':asdict(p),'variant_policy':asdict(v),'development_only':True,'eligible':False}
                with c:
                    c.execute('INSERT OR IGNORE INTO trials(session,dataset,folds,control,variant,status,report) VALUES(?,?,?,?,?,?,?)',(sid,dataset,folds,p.identity,v.identity,'pending',json.dumps(attempt)))
                    # Verify ownership in case another process reserved the same trial.
                    row=c.execute('SELECT id,session FROM trials WHERE dataset=? AND folds=? AND control=? AND variant=?',(dataset,folds,p.identity,v.identity)).fetchone()
                    if row['session']!=sid: continue
                    tid=row['id']
                try:
                    result=lab.evaluate(bars,pair,cutoff,[p,v],folds=folds)
                    by_id={r['identity']:r for r in result['results']}
                    a,b=by_id[p.identity],by_id[v.identity]
                    attempt.update(control=summary(a),variant=summary(b),comparison=compare(a,b),windows={'control':a['windows'],'variant':b['windows']},trial_id=tid)
                    with c: c.execute("UPDATE trials SET status='complete',report=? WHERE id=?",(json.dumps(attempt,allow_nan=False),tid))
                    completed.append({k:attempt[k] for k in ('trial_id','change','reason','control','variant','comparison')})
                except Exception as exc:
                    attempt['error']=str(exc)
                    with c: c.execute("UPDATE trials SET status='failed',report=? WHERE id=?",(json.dumps(attempt),tid))
                    raise
        except BaseException as exc:
            status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed'
            record['error']=str(exc)
            raise
        finally:
            candidates=record['baseline']+[x['variant'] for x in own_history(c,dataset,folds,agent)]
            candidates=[x for x in candidates if x['policy']['name']==seed.name and x['fills']]
            if candidates: record['champion']=max(candidates,key=lambda x:(x['mean_net'],x['mean_stress']))
            record.update(status=status,completed_trials=len(completed),trials=completed)
            with c: c.execute('UPDATE sessions SET status=?,report=? WHERE id=?',(status,json.dumps(record,allow_nan=False),sid))
    return record


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',default='data/research-memory.sqlite')
    sub=p.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('run'); a.add_argument('--csv',required=True); a.add_argument('--pair',required=True,choices=['XBTUSD','ETHUSD']); a.add_argument('--cutoff',type=int,required=True)
    a.add_argument('--agent',choices=list(SEEDS),required=True); a.add_argument('--model'); a.add_argument('--budget',type=int,default=5); a.add_argument('--folds',type=int,default=6)
    sub.add_parser('status'); t=sub.add_parser('trial'); t.add_argument('id',type=int)
    x=p.parse_args()
    if x.cmd=='run':
        from .coordinator import exclusive, recover
        with exclusive(str(Path(x.db).resolve())+'.lock'):
            recover(x.db)
            result=session(x.db,lab.csv_history(x.csv,x.cutoff),x.pair,x.cutoff,x.agent,x.model,x.budget,x.folds)
    else:
        with closing(sqlite3.connect(Path(x.db).resolve().as_uri()+'?mode=ro',uri=True)) as c:
            if x.cmd=='status':
                result=[]
                for sid,status,raw in c.execute('SELECT id,status,report FROM sessions ORDER BY id DESC LIMIT 10'):
                    r=dict(json.loads(raw),session_id=sid,status=status)
                    r['trial_states']=[{'trial_id':t,'status':s} for t,s in c.execute('SELECT id,status FROM trials WHERE session=? ORDER BY id',(sid,))]
                    result.append(r)
            else:
                r=c.execute('SELECT status,report FROM trials WHERE id=?',(x.id,)).fetchone()
                if not r: p.error('Unknown trial')
                result=dict(json.loads(r[1]),status=r[0])
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__': main()

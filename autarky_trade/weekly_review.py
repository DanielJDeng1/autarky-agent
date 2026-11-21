"""Development-only weekly forecast attribution and risk-controlled account review."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import combinations
import json
from pathlib import Path
import random
import statistics
from . import forecast_audit as fa, lab, multiyear
from .core import Account


def account(bars, opportunities, selected, costs):
    """Fresh cash per window; enter/exit at observed opens, stop at next open."""
    a = Account()
    entries = {r['start']: r['label_end'] for j,r in enumerate(opportunities) if j in selected}
    exit_at = None
    exposure = 0
    for b in bars:
        a.mark(b.open)
        if a.units and (a.stopped or b.ts == exit_at):
            a.fill(False,b.open,b.open,costs)
        if not a.units and not a.stopped and b.ts in entries:
            a.fill(True,b.open,b.open,costs); exit_at = entries[b.ts]
        exposure += bool(a.units)
        a.mark(b.close)
    if a.units:
        raise ValueError('Missing scheduled exit in window')
    return {'net':a.cash/1000-1,'drawdown':a.drawdown,'fills':a.trades,
            'exposure':exposure/len(bars),'stopped':a.stopped}


def evaluate(parts, pair, cutoff, audit):
    fingerprint = lab.digest([pair,[[asdict(b) for b in s] for s in parts]])
    if audit.get('dataset_sha256') != fingerprint or audit.get('pair') != pair or audit.get('cutoff') != cutoff:
        raise ValueError('Forecast report and historical dataset differ')
    weekly = next(r for r in audit['results'] if r['horizon']==168)
    samples = {r['start']:r for r in weekly['samples']}
    folds = {r['start']:r for r in weekly['folds']}
    history = []; details = []; matched = []; seen = set()
    for segment in parts:
        for start in range(lab.WARMUP,len(segment)-719,720):
            bars=segment[start:start+720]; ts=bars[0].ts; fold=folds[ts]
            rows=[samples[segment[i].ts] for i in range(start,start+720-168,168)]
            if len(rows)!=4 or any(r['label_end']!=r['start']+168*3600 for r in rows):
                raise ValueError('Invalid weekly observations')
            if fold['end']!=bars[-1].ts or fold['training_last_label_end']>=ts or any(r['label_end']>=ts for r in history):
                raise ValueError('Invalid chronological fold')
            threshold=fa.calibrate(history)
            if threshold!=fold['threshold']:
                raise ValueError('Saved threshold does not match causal calibration')
            mean_threshold=fa.calibrate([dict(r,prediction=r['baseline']) for r in history])
            ridge={i for i,r in enumerate(rows) if threshold is not None and r['prediction']>threshold}
            mean={i for i,r in enumerate(rows) if mean_threshold is not None and r['baseline']>mean_threshold}
            if len(ridge)!=fold['adaptive']['selected']:
                raise ValueError('Saved selection count differs')
            seen.add(ts)
            choices=list(combinations(range(4),len(ridge)))
            item={'start':ts,'year':datetime.fromtimestamp(ts,timezone.utc).year,'selected':len(ridge),'mean_selected':len(mean),'threshold':threshold,'mean_threshold':mean_threshold}
            for label,costs in [('normal',lab.NORMAL),('stress',lab.STRESS)]:
                random_accounts=[account(bars,rows,set(c),costs) for c in choices]
                item[label]={name:account(bars,rows,indices,costs) for name,indices in [('ridge',ridge),('historical_mean',mean),('always_weekly',set(range(4))) ]}
                item[label]['matched_random']={key:statistics.mean(x[key] for x in random_accounts) for key in ('net','drawdown','fills','exposure','stopped')}
                if label=='stress':
                    matched.append([x['net'] for x in random_accounts])
            details.append(item); history.extend(rows)
    if not details or seen!=set(folds) or len(history)!=len(samples):
        raise ValueError('Forecast coverage differs')
    def summary(items):
        return {label:{name:{'mean_net':statistics.mean(w[label][name]['net'] for w in items),
                    'worst_drawdown':max(w[label][name]['drawdown'] for w in items),
                    'mean_exposure':statistics.mean(w[label][name]['exposure'] for w in items),
                    'fills':sum(w[label][name]['fills'] for w in items)} for name in ('ridge','historical_mean','always_weekly','matched_random')} for label in ('normal','stress')}
    years={str(y):summary([w for w in details if w['year']==y]) for y in sorted({w['year'] for w in details})}
    observed=statistics.mean(w['stress']['ridge']['net'] for w in details)
    rng=random.Random(42); draws=10000
    null=[statistics.mean(rng.choice(x) for x in matched) for _ in range(draws)]
    # Conditional descriptive randomization; not a time-series significance claim.
    return {'protocol':'v0.14-weekly-review-1','dataset_sha256':fingerprint,'pair':pair,'cutoff':cutoff,'windows':len(details),
            'development_only':True,'eligible':False,'summary':summary(details),'years':years,
            'matched_random_stress':{'draws':draws,'seed':42,'mean':statistics.mean(null),'observed':observed,'fraction_at_least_observed':sum(x>=observed for x in null)/draws},
            'details':details,'note':'Fresh $1000 per 30-day window, 25% of cash per selected weekly entry, 8% account drawdown stop at next open. Random selects same planned number per window; risk stops can change actual exposure. Historical mean calibrated identically on prior labels. Conditional randomization is descriptive, not independent statistical evidence.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv',required=True); p.add_argument('--audit',required=True); p.add_argument('--pair',required=True,choices=['XBTUSD','ETHUSD']); p.add_argument('--cutoff',required=True,type=int); p.add_argument('--output',required=True)
    x=p.parse_args(); out=Path(x.output)
    if out.exists(): p.error('Output exists; choose a new filename')
    report=evaluate(multiyear.segments(x.csv,x.cutoff),x.pair,x.cutoff,json.loads(Path(x.audit).read_text(encoding='utf-8-sig')))
    out.parent.mkdir(parents=True,exist_ok=True)
    temp=out.with_name(out.name+'.partial')
    with temp.open('x',encoding='utf-8') as f: json.dump(report,f,indent=2,allow_nan=False)
    temp.replace(out)
    print(json.dumps({k:v for k,v in report.items() if k!='details'},indent=2,allow_nan=False))

if __name__=='__main__': main()

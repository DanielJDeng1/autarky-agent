"""Development-only chronological forecast diagnostics; no orders or promotion."""
import argparse
import json
import math
import statistics
import time
from dataclasses import asdict
from pathlib import Path
from . import lab, multiyear

THRESHOLDS = (0., .002, .004, .006, .012, .02)
HORIZONS = (24, 72, 168)


def net_return(realized, costs):
    fee, slip = costs.fee_bps / 10000, costs.slip_bps / 10000
    return (1 + realized) * (1-slip)*(1-fee)/((1+slip)*(1+fee))-1


def selection(rows, threshold):
    picked = [r for r in rows if threshold is not None and r['prediction'] > threshold]
    def mean(values):
        return statistics.mean(values) if values else None
    return {'observations': len(rows), 'selected': len(picked),
            'coverage': len(picked)/len(rows) if rows else 0.,
            'mean_prediction': mean([r['prediction'] for r in picked]),
            'mean_gross': mean([r['realized'] for r in picked]),
            'mean_unselected_gross': mean([r['realized'] for r in rows if threshold is None or r['prediction'] <= threshold]),
            'mean_net': mean([net_return(r['realized'], lab.NORMAL) for r in picked]),
            'mean_stress': mean([net_return(r['realized'], lab.STRESS) for r in picked]),
            'mean_net_all': sum(net_return(r['realized'],lab.NORMAL) for r in picked)/len(rows) if rows else None,
            'mean_stress_all': sum(net_return(r['realized'],lab.STRESS) for r in picked)/len(rows) if rows else None}


def calibrate(rows):
    """Prior completed out-of-fold observations only; abstain without evidence."""
    if len(rows) < 24:
        return None
    candidates = [(t, selection(rows, t)) for t in THRESHOLDS]
    candidates = [(t, m) for t, m in candidates if m['selected'] >= 8 and m['mean_stress_all'] > 0]
    return max(candidates, key=lambda x: (x[1]['mean_stress_all'], x[0]))[0] if candidates else None


def audit(parts, pair, cutoff, now=None):
    now = int(time.time()) if now is None else now
    if pair not in ('XBTUSD','ETHUSD') or not parts or not all(parts) or parts[-1][-1].ts > cutoff or parts[-1][-1].ts+3600 > now:
        raise ValueError('Require closed hourly history at cutoff')
    previous = None
    for bars in parts:
        if (previous is not None and bars[0].ts <= previous+3600) or any(b.ts-a.ts != 3600 for a,b in zip(bars,bars[1:])):
            raise ValueError('Require ordered contiguous segments separated by gaps')
        previous = bars[-1].ts
    results = []
    for h in HORIZONS:
        rows = []; folds = []; adaptive = []
        for bars in parts:
            if len(bars) < lab.WARMUP+720:
                continue
            f = lab.Features(bars)
            for start in range(lab.WARMUP,len(bars)-719,720):
                end = start+720
                # Every calibration label ends strictly before this fold starts.
                history = [r for r in rows if r['label_end'] < bars[start].ts]
                threshold = calibrate(history)
                pred, artifact = lab.forecast_fold(bars,f,{'horizon':h,'window':72,'ridge':1.},start,end)
                current = []
                for i in range(start,end-h,h):
                    r = {'start':bars[i].ts,'label_end':bars[i+h].ts,'prediction':pred[i],
                         'realized':bars[i+h].open/bars[i].open-1,'baseline':artifact['intercept'],
                         'prior_regime': 'up' if bars[i-1].close/bars[i-721].close-1 > .05 else 'down' if bars[i-1].close/bars[i-721].close-1 < -.05 else 'sideways'}
                    current.append(r)
                    adaptive.append(dict(r, selected=threshold is not None and r['prediction']>threshold))
                folds.append({'start':bars[start].ts,'end':bars[end-1].ts,'training_last_label_end':artifact['last_label_end'],
                              'calibration_observations':len(history),'calibration_last_label_end':history[-1]['label_end'] if history else None,
                              'threshold':threshold,'adaptive':selection(current,threshold)})
                rows.extend(current)
        if not rows:
            raise ValueError('No eligible forecast windows')
        def rmse(key):
            return math.sqrt(statistics.mean((r['realized']-(r[key] if key else 0))**2 for r in rows))
        picked = [r for r in adaptive if r['selected']]
        results.append({'horizon':h,'observations':len(rows),'rmse':rmse('prediction'),'zero_rmse':rmse(None),
                        'mean_baseline_rmse':rmse('baseline'),'direction_accuracy':statistics.mean((r['prediction']>0)==(r['realized']>0) for r in rows),
                        'positive_return_fraction':statistics.mean(r['realized']>0 for r in rows),
                        'unconditional_mean_gross':statistics.mean(r['realized'] for r in rows),
                        'thresholds':[{'threshold':t,**selection(rows,t)} for t in THRESHOLDS],
                        'adaptive':{'selected':len(picked),'coverage':len(picked)/len(rows),
                                    'mean_net_all':sum(net_return(r['realized'],lab.NORMAL) for r in picked)/len(rows),
                                    'mean_stress_all':sum(net_return(r['realized'],lab.STRESS) for r in picked)/len(rows)},
                        'by_prior_regime':{g:selection([r for r in rows if r['prior_regime']==g],.012) for g in ('up','down','sideways')},
                        'folds':folds,'samples':rows})
    return {'protocol':'v0.14-forecast-audit-1','pair':pair,'cutoff':cutoff,'dataset_sha256':lab.digest([pair,[[asdict(b) for b in bars] for bars in parts]]),'development_only':True,'eligible':False,
            'note':'Nonoverlapping open-to-open labels; full-notional hypothetical round-trip returns, not account returns or a trading backtest. Threshold calibration uses earlier completed out-of-fold labels only. All history is development data.', 'results':results}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv',required=True); p.add_argument('--pair',required=True,choices=['XBTUSD','ETHUSD']); p.add_argument('--cutoff',required=True,type=int); p.add_argument('--output',required=True)
    x=p.parse_args(); out=Path(x.output)
    if out.exists(): p.error('Output exists; choose a new filename')
    report=audit(multiyear.segments(x.csv,x.cutoff),x.pair,x.cutoff)
    out.parent.mkdir(parents=True,exist_ok=True)
    temp=out.with_name(out.name+'.partial')
    with temp.open('x',encoding='utf-8') as f: json.dump(report,f,indent=2,allow_nan=False)
    temp.replace(out)
    print(json.dumps({**report,'results':[{k:v for k,v in r.items() if k not in ('samples','folds')} for r in report['results']]},indent=2,allow_nan=False))

if __name__=='__main__': main()

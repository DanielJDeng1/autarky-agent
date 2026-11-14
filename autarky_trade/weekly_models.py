"""Matched chronological weekly model comparison; development only."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from . import lab, multiyear, forecast_audit as fa, weekly_review
from .models import solve

MIN_TRAIN = 24
LOOKBACK = 104
HORIZONS = (24, 72, 168, 336)
HORIZON_PROTOCOL = "v0.20-horizon-1"


def horizon_slots(horizon):
    if type(horizon) is not int or horizon not in HORIZONS:
        raise ValueError("Unsupported forecast horizon")
    return (720-1)//horizon


def horizon_calibrate(rows, horizon, conservative=False):
    slots = horizon_slots(horizon)
    # Match the number of preceding evaluation windows, not the number of labels.
    if len(rows) < (18 if conservative else 12)*slots:
        return None
    minimum = max(16 if conservative else 8, (4 if conservative else 2)*slots)
    options = [(t, fa.selection(rows,t)) for t in fa.THRESHOLDS]
    options = [(t,m) for t,m in options if m["selected"] >= minimum and m["mean_stress_all"] > 0]
    return max(options,key=lambda x:(x[1]["mean_stress_all"],x[0]))[0] if options else None
SPECS = {
    'mean': (),
    'ridge_four': (('momentum',72),('distance',72),('volatility',72),('rsi',72)),
    'ridge_expanded': (('momentum',24),('momentum',168),('momentum',672),
                       ('distance',168),('distance',672),('volatility',24),
                       ('volatility',168),('rsi',72),('volume_ratio',24),('volume_ratio',168)),
}


def fit(rows, spec):
    """Fixed ridge penalty; all candidates use identical completed weekly labels."""
    y = [r['realized'] for r in rows]
    mu = statistics.mean(y)
    if not spec:
        return lambda x: mu
    cols = [[r['features'][k] for r in rows] for k in spec]
    means = [statistics.mean(c) for c in cols]
    scales = [max(1e-8, statistics.pstdev(c)) for c in cols]
    z = [[(v-m)/s for v,m,s in zip(row,means,scales)] for row in zip(*cols)]
    n,d = len(y),len(spec)
    a = [[sum(row[j]*row[k] for row in z)/n+(j==k) for k in range(d)] for j in range(d)]
    b = [sum(row[j]*(v-mu) for row,v in zip(z,y))/n for j in range(d)]
    weights = solve(a,b)
    return lambda x: mu+sum(w*(x[k]-m)/s for w,k,m,s in zip(weights,spec,means,scales))


def review(parts, pair, cutoff, now=None, *, specs=None, fitter=None, calibrator=None, feature_provider=None, horizon=None):
    # None is the unchanged legacy weekly protocol; explicit 168 uses the new protocol.
    h = 168 if horizon is None else horizon
    slots = horizon_slots(h)
    min_train = MIN_TRAIN if horizon is None else 12*slots
    lookback = LOOKBACK if horizon is None else 26*slots
    specs = SPECS if specs is None else specs
    fitter = (lambda name, rows, spec: fit(rows, spec)) if fitter is None else fitter
    calibrator = (lambda name, rows: fa.calibrate(rows) if horizon is None else horizon_calibrate(rows,h)) if calibrator is None else calibrator
    if not specs or len(specs) > 16:
        raise ValueError("Require 1..16 model specifications")
    now = int(time.time()) if now is None else now
    if pair not in ('XBTUSD','ETHUSD') or not parts or not all(parts) or parts[-1][-1].ts > cutoff or parts[-1][-1].ts+3600 > now:
        raise ValueError('Require closed hourly history at cutoff')
    previous = None
    for bars in parts:
        if (previous is not None and bars[0].ts <= previous+3600) or any(b.ts-a.ts != 3600 for a,b in zip(bars,bars[1:])):
            raise ValueError('Require ordered contiguous segments separated by gaps')
        previous = bars[-1].ts
    keys = set(k for spec in specs.values() for k in spec)
    training = []; history = {name:[] for name in specs}; windows = []
    for bars in parts:
        if len(bars)<lab.WARMUP+720:
            continue
        f=lab.Features(bars)
        series={}
        for k in keys:
            if k[0] in lab.FEATURES and k[0]!='forecast':
                series[k]=f.series(*k)
            elif feature_provider is not None:
                series[k]=feature_provider(bars,k)
                if len(series[k])!=len(bars): raise ValueError('Invalid external feature length')
            else: raise ValueError('External feature requires aligned market data')
        for start in range(lab.WARMUP,len(bars)-719,720):
            ts=bars[start].ts; end=start+720
            past=[r for r in training if r['label_end']<ts][-lookback:]
            rows=[]
            for i in range(start,end-h,h):
                # A missing volume ratio (zero preceding volume) is neutral at one.
                if any(series[k][i] is None for k in keys if k[0] not in lab.FEATURES):
                    raise ValueError('Missing aligned external feature at decision')
                x={k:series[k][i] if series[k][i] is not None else 1.0 for k in keys}
                rows.append({'start':bars[i].ts,'label_end':bars[i+h].ts,
                             'realized':bars[i+h].open/bars[i].open-1,'features':x})
            if len(past)>=min_train:
                record={'start':ts,'year':datetime.fromtimestamp(ts,timezone.utc).year,
                        'train_count':len(past),'train_last_label_end':past[-1]['label_end'],'models':{}}
                for name,spec in specs.items():
                    prior=[r for r in history[name] if r['label_end']<ts][-lookback:]
                    threshold=calibrator(name,prior)
                    predict=fitter(name,past,spec)
                    samples=[{k:r[k] for k in ('start','label_end','realized')} | {'prediction':predict(r['features'])} for r in rows]
                    picked={i for i,r in enumerate(samples) if threshold is not None and r['prediction']>threshold}
                    record['models'][name]={'threshold':threshold,'calibration_count':len(prior),
                        'calibration_last_label_end':prior[-1]['label_end'] if prior else None,
                        'samples':samples,'selected':len(picked),
                        'normal':weekly_review.account(bars[start:end],samples,picked,lab.NORMAL),
                        'stress':weekly_review.account(bars[start:end],samples,picked,lab.STRESS)}
                    history[name].extend(samples)
                windows.append(record)
            training.extend(rows)
    if not windows:
        raise ValueError('Insufficient completed training labels')
    def summarize(items):
        if not items:
            return {}
        result={}
        for name in specs:
            ms=[w['models'][name] for w in items]; rows=[r for m in ms for r in m['samples']]
            result[name]={'windows':len(items),'observations':len(rows),
                'rmse':math.sqrt(statistics.mean((r['prediction']-r['realized'])**2 for r in rows)),
                'mean_net':statistics.mean(m['normal']['net'] for m in ms),
                'mean_stress':statistics.mean(m['stress']['net'] for m in ms),
                'worst_drawdown':max(m['normal']['drawdown'] for m in ms),
                'fills':sum(m['normal']['fills'] for m in ms),
                'mean_exposure':statistics.mean(m['normal']['exposure'] for m in ms),
                'active_windows':sum(m['normal']['fills']>0 for m in ms)}
        return result
    years=sorted({w['year'] for w in windows})
    report = {'protocol':'v0.14-weekly-models-1','pair':pair,'cutoff':cutoff,
            'dataset_sha256':lab.digest([pair,[[asdict(b) for b in s] for s in parts]]),
            'development_only':True,'eligible':False,'windows':len(windows),
            'summary':summarize(windows),'post_2017':summarize([w for w in windows if w['year']>=2018]),
            'years':{str(y):summarize([w for w in windows if w['year']==y]) for y in years},
            'exclude_year':{str(y):summarize([w for w in windows if w['year']!=y]) for y in years},
            'details':windows,'note':'Same nonoverlapping completed weekly labels for all models; up to 104 training and calibration observations, minimum 24. Fixed ridge penalty 1. Fresh monthly accounts and cash abstention. Year exclusion describes concentration, not a refit or holdout.'}

    if horizon is not None:
        report.update(protocol=HORIZON_PROTOCOL,horizon_hours=h,opportunities_per_window=slots,
                      min_train_observations=min_train,lookback_observations=lookback,
                      note='Development only. Fresh 30-day accounts; nonoverlapping open-to-open labels, 12 prior observed windows minimum, rolling 26 windows. Standard calibration requires 12 prior out-of-fold windows; conservative requires 18. No position carries across window boundaries. Different horizons have different exposure and label counts.')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv',required=True); p.add_argument('--pair',required=True,choices=['XBTUSD','ETHUSD'])
    p.add_argument('--cutoff',type=int,required=True); p.add_argument('--output',required=True)
    x=p.parse_args(); out=Path(x.output)
    if out.exists(): p.error('Output exists; choose a new filename')
    result=review(multiyear.segments(x.csv,x.cutoff),x.pair,x.cutoff)
    out.parent.mkdir(parents=True,exist_ok=True)
    temp=out.with_name(out.name+'.partial')
    with temp.open('x',encoding='utf-8') as f: json.dump(result,f,indent=2,allow_nan=False)
    temp.replace(out)
    print(json.dumps({k:v for k,v in result.items() if k!='details'},indent=2,allow_nan=False))

if __name__=='__main__': main()

"""Fixed, matched model research tools. No promotion or trade execution."""
import argparse
import json
import math
from pathlib import Path
import statistics
from . import multiyear, weekly_models as wm

TREND = tuple(k for k in wm.SPECS['ridge_expanded'] if k[0] in ('momentum','distance','rsi'))
RISK = tuple(k for k in wm.SPECS['ridge_expanded'] if k[0] in ('volatility','volume_ratio'))
REGIME = ('momentum',672)
SPECS = {**wm.SPECS, 'ridge_trend_only': TREND, 'ridge_risk_only': RISK,
         'regime_mean': (REGIME,), 'similarity': wm.SPECS['ridge_expanded']}


def fit_model(name, rows, spec):
    if name == 'regime_mean':
        # Fixed prior-month trend buckets; shrink small groups toward overall mean.
        def regime(x):
            return 'up' if x[REGIME] > .05 else 'down' if x[REGIME] < -.05 else 'flat'
        mean = statistics.mean(r['realized'] for r in rows)
        groups = {g:[r['realized'] for r in rows if regime(r['features'])==g] for g in ('up','down','flat')}
        values = {g:(sum(ys)+12*mean)/(len(ys)+12) for g,ys in groups.items()}
        return lambda x: values[regime(x)]
    if name == 'similarity':
        # Smooth nonlinear local mean, training-only scaling and fixed shrinkage.
        ys = [r['realized'] for r in rows]
        mean = statistics.mean(ys)
        centers = [statistics.mean(r['features'][k] for r in rows) for k in spec]
        scales = [max(1e-8,statistics.pstdev(r['features'][k] for r in rows)) for k in spec]
        def transform(x):
            return [(x[k]-m)/s for k,m,s in zip(spec,centers,scales)]
        z = [transform(r['features']) for r in rows]
        def predict(x):
            q = transform(x)
            weights = [math.exp(-sum((a-b)**2 for a,b in zip(q,v))/(2*len(spec))) for v in z]
            return (sum(w*y for w,y in zip(weights,ys))+12*mean)/(sum(weights)+12)
        return predict
    return wm.fit(rows,spec)


def evaluate(parts, pair, cutoff, now=None):
    report = wm.review(parts,pair,cutoff,now,specs=SPECS,fitter=fit_model)
    # Paired deltas on identical windows. No model is selected or promoted.
    def compare(windows):
        out = {}
        if not windows:
            return out
        for name in SPECS:
            pairs = [(w['models'][name],w['models']['mean']) for w in windows]
            n = len(pairs)
            delta = [a['normal']['net']-b['normal']['net'] for a,b in pairs]
            stress = [a['stress']['net']-b['stress']['net'] for a,b in pairs]
            loss = [(x['prediction']-x['realized'])**2-(y['prediction']-y['realized'])**2
                    for a,b in pairs for x,y in zip(a['samples'],b['samples'])]
            # Within-window pairwise ranking, excluding tied predictions and outcomes.
            correct = total = 0
            for a,_ in pairs:
                samples = a['samples']
                for i,x in enumerate(samples):
                    for y in samples[i+1:]:
                        p = x['prediction']-y['prediction']; r = x['realized']-y['realized']
                        if p and r:
                            total += 1; correct += p*r>0
            out[name] = {'windows':n,'net_delta':statistics.mean(delta),'stress_delta':statistics.mean(stress),
                         'mse_delta':statistics.mean(loss),'net_win_windows':sum(x>0 for x in delta),
                         'timing_pairs':total,'timing_accuracy':correct/total if total else None}
        return out
    report.update(protocol='v0.15-model-discovery-1', paired_vs_mean=compare(report['details']),
                  post_2017_paired=compare([w for w in report['details'] if w['year']>=2018]),
                  specifications={name:[list(k) for k in spec] for name,spec in SPECS.items()},
                  note='Fixed candidate set; same nonoverlapping labels, rolling 104-observation training/calibration, 24 minimum, matched 25% allocation. All results development only. Timing accuracy ignores tied pairs and is descriptive, not significance evidence. No model promotion.')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv',required=True); p.add_argument('--pair',required=True,choices=['XBTUSD','ETHUSD'])
    p.add_argument('--cutoff',required=True,type=int); p.add_argument('--output',required=True)
    x=p.parse_args(); out=Path(x.output)
    if out.exists(): p.error('Output exists; choose a new filename')
    result=evaluate(multiyear.segments(x.csv,x.cutoff),x.pair,x.cutoff)
    out.parent.mkdir(parents=True,exist_ok=True); temp=out.with_name(out.name+'.partial')
    with temp.open('x',encoding='utf-8') as f: json.dump(result,f,indent=2,allow_nan=False)
    temp.replace(out)
    print(json.dumps({k:v for k,v in result.items() if k!='details'},indent=2,allow_nan=False))

if __name__=='__main__': main()

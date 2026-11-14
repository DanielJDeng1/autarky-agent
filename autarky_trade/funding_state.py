"""Fixed, causal funding-tail event study with matched-count account controls."""
import argparse
from bisect import bisect_left, bisect_right, insort
from collections import deque
from itertools import combinations
import json
from pathlib import Path
import statistics
from . import funding_research as funding, forecast_audit as fa, lab, multiyear, weekly_models as wm, weekly_review

PROTOCOL = 'v0.23-funding-state-1'
RULES = {'low_level': ('level', 'low'), 'high_level': ('level', 'high'),
         'falling_funding': ('change', 'low'), 'rising_funding': ('change', 'high')}
RANK_HISTORY = 504


def trailing_ranks(values, history=RANK_HISTORY):
    """Midrank against preceding observations only; constant history is neutral."""
    if type(history) is not int or history < 1:
        raise ValueError('Invalid rank history')
    ordered=[]; queue=deque(); output=[]
    for value in values:
        if value is None:
            ordered.clear(); queue.clear(); output.append(None); continue
        if len(queue)==history:
            output.append((bisect_left(ordered,value)+bisect_right(ordered,value))/(2*history))
            prev=queue.popleft(); ordered.pop(bisect_left(ordered,prev))
        else: output.append(None)
        insort(ordered,value); queue.append(value)
    return output


def states(bars, values):
    short=funding.feature_series(bars,values,('funding_mean',24))
    long=funding.feature_series(bars,values,('funding_mean',168))
    # Short minus the preceding, non-overlapping six-day average.
    change=[(a-b)*168/144 if a is not None and b is not None else None for a,b in zip(short,long)]
    return {'level':trailing_ranks(short),'change':trailing_ranks(change)}


def pick(ranks, side):
    if side not in ('low','high'): raise ValueError('Unknown tail')
    if any(x is None for x in ranks): raise ValueError('Insufficient causal funding rank history')
    return {i for i,x in enumerate(ranks) if (x<=.2 if side=='low' else x>=.8)}


def summarize(records):
    selected=[r for w in records for r in w['selected_returns']]
    def avg(xs): return statistics.mean(xs) if xs else None
    return {'windows':len(records),'opportunities':sum(w['opportunities'] for w in records),
            'selected':len(selected),'active_windows':sum(w['normal']['fills']>0 for w in records),
            'fills':sum(w['normal']['fills'] for w in records),
            'mean_selected_gross':avg(selected),
            'mean_selected_stress_full_notional':avg([fa.net_return(x,lab.STRESS) for x in selected]),
            'mean_net':avg([w['normal']['net'] for w in records]),
            'mean_stress':avg([w['stress']['net'] for w in records]),
            'mean_random_stress':avg([w['random_stress'] for w in records]),
            'stress_delta_random':avg([w['stress']['net']-w['random_stress'] for w in records]),
            'mean_exposure':avg([w['normal']['exposure'] for w in records]),
            'worst_drawdown':max(w['normal']['drawdown'] for w in records)}


def review(parts, dataset, cutoff, horizons=(168,336), now=None):
    if dataset.get('protocol')!=funding.PROTOCOL or dataset.get('symbols')!=funding.SYMBOLS or dataset.get('events_sha256')!=lab.digest(dataset['events']):
        raise ValueError('Funding dataset identity mismatch')
    if not horizons or len(set(horizons))!=len(horizons) or any(type(h) is not int or h not in (168,336) for h in horizons):
        raise ValueError('Require distinct weekly/two-week horizons')
    aligned,values=funding.matched(parts,dataset['events']); results=[]; details=[]; common=None
    for pair in funding.SYMBOLS:
        # Each timestamp maps to its own contiguous segment; ranks never span gaps.
        lookup={}
        for bars in aligned[pair]:
            if len(bars)<lab.WARMUP+720: continue
            ranks=states(bars,values[pair])
            for start in range(lab.WARMUP,len(bars)-719,720):
                lookup[bars[start].ts]=(bars,start,ranks)
        for h in horizons:
            base=wm.review(aligned[pair],pair,cutoff,now,specs={'mean':()},horizon=h)
            dates=[w['start'] for w in base['details']]
            if common is None: common=dates
            if dates!=common: raise ValueError('Matched evaluation dates differ')
            records={name:[] for name in RULES}
            for w in base['details']:
                bars,start,ranks=lookup[w['start']]; samples=w['models']['mean']['samples']; window=bars[start:start+720]
                indices=list(range(start,start+720-h,h))
                if len(samples)!=len(indices): raise ValueError('Invalid opportunity count')
                # Exact enumeration: at most four opportunities, six subsets of size two.
                random_by_count={}
                for name,(kind,side) in RULES.items():
                    rank=[ranks[kind][i] for i in indices]; chosen=pick(rank,side); n=len(chosen)
                    if n not in random_by_count:
                        random_by_count[n]=statistics.mean(weekly_review.account(window,samples,set(c),lab.STRESS)['net'] for c in combinations(range(len(samples)),n))
                    records[name].append({'start':w['start'],'opportunities':len(samples),'ranks':rank,'selected_indices':sorted(chosen),
                        'selected_returns':[samples[i]['realized'] for i in sorted(chosen)],
                        'normal':weekly_review.account(window,samples,chosen,lab.NORMAL),
                        'stress':weekly_review.account(window,samples,chosen,lab.STRESS),
                        'random_stress':random_by_count[n]})
            for name,rows in records.items():
                results.append({'pair':pair,'horizon_hours':h,'rule':name,**summarize(rows)})
                details.append({'pair':pair,'horizon_hours':h,'rule':name,'windows':rows})
    return {'protocol':PROTOCOL,'cutoff':cutoff,'funding_sha256':dataset['events_sha256'],
            'spot_sha256':{p:lab.digest([p,[[wm.asdict(b) for b in s] for s in aligned[p]]]) for p in funding.SYMBOLS},
            'aligned_bars':sum(map(len,aligned['XBTUSD'])),'windows':len(common),'window_starts':common,
            'rank_history_hours':RANK_HISTORY,'tail_fraction':.2,'results':results,'details':details,
            'development_only':True,'eligible':False,
            'note':'Fixed low/high funding level and change rules, no fitted return model or calibration gate. Midranks use only preceding 504 hourly signal values, with the v0.22 one-hour availability lag. Selected label returns are hypothetical full-notional returns; account results use 25% allocation and risk stop. Exact matched-count random account expectation conditions on each window selection count; not independent statistical significance. All data are development only.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--funding',required=True); p.add_argument('--btc',required=True); p.add_argument('--eth',required=True)
    p.add_argument('--cutoff',required=True,type=int); p.add_argument('--output',required=True)
    p.add_argument('--horizons',type=int,nargs='+',choices=(168,336),default=[168,336]); x=p.parse_args()
    if Path(x.output).resolve() in {Path(y).resolve() for y in (x.funding,x.btc,x.eth)} or Path(x.output).exists():
        p.error('Choose a new output distinct from inputs')
    result=review({'XBTUSD':multiyear.segments(x.btc,x.cutoff),'ETHUSD':multiyear.segments(x.eth,x.cutoff)},
                  json.loads(Path(x.funding).read_text(encoding='utf-8-sig')),x.cutoff,x.horizons)
    funding.write(x.output,result)
    print(json.dumps({k:v for k,v in result.items() if k!='details'},indent=2,allow_nan=False))


if __name__=='__main__': main()

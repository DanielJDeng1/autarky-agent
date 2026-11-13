"""Versioned, chronological experiment selection. No test data influences ranking."""
from dataclasses import asdict
import hashlib
import json
import math
from .core import Strategy, Costs, backtest
from .benchmarks import reference

PROTOCOL = 'v0.5-folds-1'
# OHLC backtests have no observed bid/ask: include a conservative proxy for half spread.
RESEARCH_COSTS = Costs(40, 20)
STRESS_COSTS = Costs(80, 40)


def identity(strategy):
    fields = asdict(strategy)
    fields.pop('name')
    fields.pop('hypothesis', None)
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def valid(metrics, minimum_fills=4):
    return metrics['return'] > 0 and metrics['trades'] >= minimum_fills and metrics['drawdown'] < .08 and not metrics['stopped']


def score(metrics):
    return metrics['return'] - metrics['drawdown']


def assess(bars, strategies, hold):
    split = int(hold*.75)
    if not 140 <= split < hold < len(bars) or hold-split < 120:
        raise ValueError('Insufficient chronological validation windows')
    bounds = [split + (hold-split)*i//3 for i in range(4)]
    rows = []
    seen = set()
    for s in strategies:
        key = identity(s)
        if key in seen:
            continue
        seen.add(key)
        train = backtest(bars,s,RESEARCH_COSTS,100,split)
        val = backtest(bars,s,RESEARCH_COSTS,split,hold)
        folds = [backtest(bars,s,RESEARCH_COSTS,a,b) for a,b in zip(bounds,bounds[1:])]
        # A fold can legitimately have few trades; require positive, risk-bounded results in 2/3.
        positives = sum(f['return']>0 and not f['stopped'] and f['drawdown']<.08 for f in folds)
        passes = valid(train) and valid(val) and positives >= 2 and all(not f['stopped'] for f in folds)
        rows.append({'strategy':asdict(s),'strategy_id':key,'train':train,'validation':val,
                     'folds':folds,'positive_folds':positives,'pretest_pass':passes,
                     'selection_score':score(val)+min(score(f) for f in folds)})
    if not rows:
        raise ValueError('No unique candidate strategies')
    rows.sort(key=lambda r:(r['pretest_pass'],r['selection_score'],r['strategy_id']),reverse=True)
    winner=rows[0]; s=Strategy(**winner['strategy'])
    test=backtest(bars,s,RESEARCH_COSTS,hold,len(bars))
    stress=backtest(bars,s,STRESS_COSTS,hold,len(bars))
    benchmarks=reference(bars,s,RESEARCH_COSTS,hold,len(bars),split)
    # The passive comparator uses the same fraction, costs and risk stop.
    checks={'pretest':winner['pretest_pass'],'holdout':valid(test),'cost_stress':valid(stress),
            'random_timing':test['return']>benchmarks['random_p75'],
            'passive_risk_adjusted':score(test)>benchmarks['fractional_buy_hold_score']}
    digest=hashlib.sha256(json.dumps([asdict(b) for b in bars],separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return {'protocol':PROTOCOL,'dataset_sha256':digest,'strategy':asdict(s),'strategy_id':winner['strategy_id'],
            'candidates':rows,'test':test,'stress':stress,'benchmarks':benchmarks,'eligible':all(checks.values()),
            'checks':checks,'fold_bounds':[bars[i].ts for i in bounds],
            'costs':asdict(RESEARCH_COSTS),'stress_costs':asdict(STRESS_COSTS),
            'note':'Exploratory chronological gate; three folds and random benchmarks do not establish significance. Only the pretest-selected candidate sees holdout.'}

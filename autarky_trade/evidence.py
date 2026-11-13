"""Dependence-aware descriptive research diagnostics, not live authorization.

Paired circular moving-block bootstrap on strategy minus cost-matched passive
log returns. Whole blocks retain local serial dependence. The interval is
conditional on this frozen candidate and sample, not adjusted for adaptive
selection across experiments. No claim of a deflated Sharpe ratio or PBO.
"""
import math
import random
from .core import backtest_path


class Passive:
    def signal(self, closes): return True


def block_interval(values, block, repeats=999, seed=641):
    n=len(values)
    if not 2<=block<=n or not 99<=repeats<=10000 or not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid bootstrap sample')
    # Circular blocks with uniform starting points; sum in O(1) per draw.
    prefix=[0.]
    for v in list(values)+list(values): prefix.append(prefix[-1]+v)
    rng=random.Random(seed); samples=[]
    q,r=divmod(n,block)
    for _ in range(repeats):
        total=0.
        for _ in range(q):
            i=rng.randrange(n); total+=prefix[i+block]-prefix[i]
        if r:
            i=rng.randrange(n); total+=prefix[i+r]-prefix[i]
        samples.append(total/n)
    samples.sort()
    return [samples[int(.025*(repeats-1))],samples[int(.975*(repeats-1))]]


def describe(bars,strategy,costs,start,end):
    _,active=backtest_path(bars,strategy,costs,start,end)
    _,passive=backtest_path(bars,Passive(),costs,start,end)
    excess=[a-b for a,b in zip(active,passive)]
    n=len(excess); mean=sum(excess)/n
    # Sensitivity to block length; at hourly data 24/72 bars ~1/3 days.
    block_sizes=sorted(set(min(n//2,max(2,b)) for b in (round(n**(1/3)),24,72)))
    intervals={str(b):block_interval(excess,b) for b in block_sizes}
    return {'observations':n,'mean_excess_log_return_per_bar':mean,'compounded_relative_return':math.expm1(sum(excess)),
            'intervals_95':intervals,'positive_lower_bounds':all(v[0]>0 for v in intervals.values()),
            'resamples':999,'seed':641,'method':'paired circular moving-block bootstrap; descriptive, not multiplicity adjusted',
            'warning':'Finite sample, regime changes, selection and dependence beyond block length can invalidate inference. Not a promotion or live gate.'}

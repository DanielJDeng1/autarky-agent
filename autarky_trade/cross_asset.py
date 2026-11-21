"""Aligned, strictly lagged market features for development research."""
import math
from . import lab

PAIRS = ('XBTUSD', 'ETHUSD')
FEATURES = ('other_momentum', 'other_volatility', 'relative_strength', 'cross_correlation')
PROTOCOL = 'v0.18-cross-asset-1'


def align(parts):
    """Intersect observed timestamps; a missing hour in either market splits both."""
    if set(parts) != set(PAIRS):
        raise ValueError('Require BTC and ETH history')
    maps = {}
    for pair in PAIRS:
        observed = {}
        previous = None
        for segment in parts[pair]:
            if not segment:
                raise ValueError('Empty segment')
            for i, b in enumerate(segment):
                if previous is not None and (b.ts <= previous or (i and b.ts != previous+3600)):
                    raise ValueError('Invalid ordered hourly history')
                observed[b.ts] = b
                previous = b.ts
        maps[pair] = observed
    result = {p: [] for p in PAIRS}
    previous = None
    for ts in sorted(maps[PAIRS[0]].keys() & maps[PAIRS[1]].keys()):
        if previous is None or ts != previous+3600:
            for p in PAIRS:
                result[p].append([])
        for p in PAIRS:
            result[p][-1].append(maps[p][ts])
        previous = ts
    if not result[PAIRS[0]]:
        raise ValueError('No shared hourly history')
    return result


def series(bars, other, feature, window):
    """At index i, use close-to-close returns ending at i-1, never bar i."""
    if feature not in FEATURES or not 2 <= window <= lab.WARMUP-1:
        raise ValueError('Invalid cross-market feature')
    if len(bars) != len(other) or any(a.ts != b.ts for a,b in zip(bars,other)):
        raise ValueError('Require exactly aligned market candles')
    if any(b.ts-a.ts != 3600 for a,b in zip(bars,bars[1:])):
        raise ValueError('Cross-market features cannot span gaps')
    if feature == 'other_momentum':
        return lab.Features(other).series('momentum',window)
    if feature == 'other_volatility':
        return lab.Features(other).series('volatility',window)
    if feature == 'relative_strength':
        a=lab.Features(bars).series('momentum',window)
        b=lab.Features(other).series('momentum',window)
        return [x-y if x is not None and y is not None else None for x,y in zip(a,b)]
    # Rolling Pearson correlation of synchronous hourly simple returns.
    a=[0.]+[y.close/x.close-1 for x,y in zip(bars,bars[1:])]
    b=[0.]+[y.close/x.close-1 for x,y in zip(other,other[1:])]
    def prefix(values):
        out=[0.]
        for x in values: out.append(out[-1]+x)
        return out
    sums=[prefix(v) for v in (a,b,[x*x for x in a],[y*y for y in b],[x*y for x,y in zip(a,b)])]
    out=[None]*len(bars)
    for i in range(window+1,len(bars)):
        x,y,xx,yy,xy=[(s[i]-s[i-window])/window for s in sums]
        vx=max(0.,xx-x*x); vy=max(0.,yy-y*y)
        # Undefined correlation for a constant return series is neutral, not a fabricated observation.
        out[i]=max(-1.,min(1.,(xy-x*y)/math.sqrt(vx*vy))) if vx>1e-16 and vy>1e-16 else 0.
    return out

"""Gap-aware fixed-window historical development review; never promotes or trades."""
import argparse
import csv
import json
import math
from dataclasses import asdict
from pathlib import Path
import statistics
import time
from . import lab, research_review
from .core import Bar, Account, Costs


def segments(path, cutoff):
    """Validate observed candles and split at missing hours, without interpolation."""
    result = []; current = []; previous = None
    with open(path, newline='', encoding='utf-8-sig') as f:
        for i, row in enumerate(csv.reader(f)):
            if i == 0 and row and row[0].lower() in ('timestamp', 'time', 'ts'):
                continue
            if len(row) not in (6, 7):
                raise ValueError(f'Invalid OHLCVT row {i+1}')
            b = Bar(int(row[0]), *(float(v) for v in row[1:6]))
            if previous is not None and b.ts <= previous:
                raise ValueError('Duplicate or unsorted timestamp')
            if b.ts > cutoff:
                break
            prices = (b.open, b.high, b.low, b.close)
            if b.ts < 0 or b.ts % 3600 or not all(math.isfinite(v) for v in (*prices, b.volume)) or min(prices) <= 0 or b.low > min(b.open, b.close) or b.high < max(b.open, b.close) or b.volume < 0:
                raise ValueError(f'Invalid candle at row {i+1}')
            if previous is not None and b.ts != previous + 3600:
                result.append(current); current = []
            current.append(b); previous = b.ts
    if current:
        result.append(current)
    if not result:
        raise ValueError('No history at cutoff')
    return result


def review(parts, policies, pair, cutoff, hours=720, now=None):
    now = int(time.time()) if now is None else now
    if pair not in ('XBTUSD', 'ETHUSD') or type(hours) is not int or not 168 <= hours <= 2160:
        raise ValueError('Invalid market or window length (168..2160 hours)')
    policies = list({p.identity: p for p in policies}.values())
    if not policies or len(policies) > 32 or any(p.fraction != .25 for p in policies):
        raise ValueError('Require 1..32 unique policies at matched 25% allocation')
    if not parts or not all(parts) or parts[-1][-1].ts > cutoff or parts[-1][-1].ts + 3600 > now:
        raise ValueError('Require closed history at cutoff')
    previous = None
    for bars in parts:
        if previous is not None and bars[0].ts <= previous + 3600:
            raise ValueError('Segments must be ordered and separated by gaps')
        if any(b.ts-a.ts != 3600 for a,b in zip(bars,bars[1:])):
            raise ValueError('Gap inside a segment')
        previous = bars[-1].ts
    records = {p.identity: [] for p in policies}; coverage = []
    for bars in parts:
        count = max(0, (len(bars) - lab.WARMUP) // hours)
        coverage.append({'first': bars[0].ts, 'last': bars[-1].ts, 'bars': len(bars), 'windows': count,
                         'warmup': lab.WARMUP if count else 0, 'evaluated': count*hours,
                         'excluded': len(bars)-count*hours})
        if not count:
            continue
        f = lab.Features(bars)
        for start in range(lab.WARMUP, lab.WARMUP + count*hours, hours):
            end = start + hours
            market = lab.window_regime(bars, start, end)
            prior = bars[start-1].close / bars[start-721].close - 1
            prior_regime = 'up' if prior > .05 else 'down' if prior < -.05 else 'sideways'
            passive = Account()
            passive.fill(True, bars[start].open, bars[start].open, Costs(40,20,.25))
            passive.fill(False, bars[end-1].close, bars[end-1].close, Costs(40,20,.25))
            for p in policies:
                pred, artifact = lab.forecast_fold(bars, f, p.predictor, start, end) if p.predictor else (None, None)
                n = lab.simulate(bars, p, start, end, lab.NORMAL, f, pred)
                s = lab.simulate(bars, p, start, end, lab.STRESS, f, pred)
                n.pop('log_returns'); s.pop('log_returns')
                records[p.identity].append({'start': bars[start].ts, 'end': bars[end-1].ts,
                    'normal': n, 'stress': s, 'passive_return': passive.cash/1000-1,
                    'market_regime': market, 'prior_regime': prior_regime, 'prior_return': prior,
                    'model_artifact': artifact})
    if not any(records.values()):
        raise ValueError('No segment has sufficient warmup and evaluation history')
    def metrics(ws):
        n = [w['normal'] for w in ws]
        return {'windows': len(ws), 'mean_net': statistics.mean(x['net_return'] for x in n),
                'mean_gross': statistics.mean(x['same_trade_path_gross_return'] for x in n),
                'mean_stress': statistics.mean(w['stress']['net_return'] for w in ws),
                'mean_passive': statistics.mean(w['passive_return'] for w in ws),
                'mean_exposure': statistics.mean(x['exposure_fraction'] for x in n),
                'mean_turnover': statistics.mean(x['turnover'] for x in n),
                'worst_drawdown': max(x['drawdown'] for x in n),
                'positive_windows': sum(x['net_return'] > 0 for x in n), 'fills': sum(x['fills'] for x in n)}
    results = []
    for p in policies:
        ws = records[p.identity]
        groups = {r: metrics([w for w in ws if w['prior_regime']==r]) for r in ('up','down','sideways') if any(w['prior_regime']==r for w in ws)}
        realized = {r: metrics([w for w in ws if w['market_regime']['direction']==r]) for r in ('up','down','sideways') if any(w['market_regime']['direction']==r for w in ws)}
        results.append({'policy': asdict(p), 'identity': p.identity, **metrics(ws), 'by_prior_regime': groups, 'by_realized_regime': realized, 'detail': ws})
    results.sort(key=lambda r: (r['mean_net'], r['mean_stress']), reverse=True)
    return {'protocol': 'v0.14-gap-review-1', 'pair': pair, 'cutoff': cutoff,
            'dataset_sha256': lab.digest([pair, [[asdict(b) for b in bars] for bars in parts]]),
            'bars': sum(len(b) for b in parts), 'evaluated_bars': sum(c['evaluated'] for c in coverage),
            'segments': len(parts), 'used_segments': sum(c['windows']>0 for c in coverage),
            'first': parts[0][0].ts, 'last': parts[-1][-1].ts, 'window_hours': hours,
            'first_evaluated': results[0]['detail'][0]['start'], 'last_evaluated': results[0]['detail'][-1]['end'],
            'windows': len(results[0]['detail']), 'coverage': coverage, 'results': results,
            'development_only': True, 'eligible': False,
            'note': 'Equal nonoverlapping windows; fresh cash each window, liquidation at final close. No trading across gaps. Prior regime uses trailing 720 closed hours; realized regime is descriptive only. Cash return is zero. Not independent promotion evidence.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv', required=True); p.add_argument('--pair', required=True, choices=['XBTUSD','ETHUSD'])
    p.add_argument('--cutoff', required=True, type=int); p.add_argument('--hours', type=int, default=720)
    p.add_argument('--output', required=True); p.add_argument('--memory'); p.add_argument('--trials', nargs='+', type=int)
    x = p.parse_args()
    if bool(x.memory) != bool(x.trials): p.error('--memory and --trials must be provided together')
    out = Path(x.output)
    if out.exists(): p.error('Output exists; choose a new report filename')
    ps = lab.defaults()
    if x.trials: ps += research_review.frozen(x.memory, x.trials, x.pair)
    result = review(segments(x.csv, x.cutoff), ps, x.pair, x.cutoff, x.hours)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name+'.partial')
    with temp.open('x', encoding='utf-8') as f: json.dump(result, f, indent=2, allow_nan=False)
    temp.replace(out)
    print(json.dumps({k:v for k,v in result.items() if k not in ('coverage','results')} | {'results':[{k:v for k,v in r.items() if k!='detail'} for r in result['results']]}, indent=2, allow_nan=False))

if __name__ == '__main__': main()

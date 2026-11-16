"""Read-only economic feasibility replay of the saved v0.20 directional review.
No new optimization, paper orders, wallet, or modification to frozen production code.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics

PROTOCOL = 'v0.29-economic-review-1'
# All fees in basis points, per side. Slippage is an assumption, not a venue quote.
SCENARIOS = {'legacy_normal': (40, 20), 'kraken_taker': (80, 20),
             'legacy_stress': (80, 40), 'okx_us_proxy': (35, 20)}
PAIRS = ('XBTUSD', 'ETHUSD')


def breakeven(fee_bps, slip_bps):
    """Gross move needed to buy and sell using multiplicative fees and slippage."""
    f, s = fee_bps / 10000, slip_bps / 10000
    if not 0 <= f < 1 or not 0 <= s < 1: raise ValueError('Invalid fees')
    return (1 + f) * (1 + s) / ((1 - f) * (1 - s)) - 1


def economics(mean_return, capital, daily_cost, days=30):
    if not all(math.isfinite(x) for x in (mean_return, capital, daily_cost, days)) or capital <= 0 or daily_cost < 0 or days <= 0:
        raise ValueError('Invalid economics')
    annual_return = mean_return * 365 / days
    return {'annual_simple_return': annual_return,
            'annual_simple_trading_usd': capital * annual_return,
            'annual_operating_usd': daily_cost * 365,
            'annual_simple_surplus_usd': capital * annual_return - daily_cost * 365,
            'required_annual_return': daily_cost * 365 / capital,
            'required_capital_usd_at_observed_return': daily_cost * 365 / annual_return if annual_return > 0 else None}


def replay(report, parts, capital=200., daily_cost=.25):
    from . import autonomous_research as old, cross_asset as cross, lab, weekly_review, weekly_models as wm
    from .core import Costs
    if report.get('protocol') != wm.HORIZON_PROTOCOL or report.get('development_only') is not True:
        raise ValueError('Require saved v0.20 historical review')
    cutoff = report['cutoff']
    original = {p: old.fingerprint(parts[p], p) for p in PAIRS}
    aligned = cross.align(parts)
    hashes = {p: old.fingerprint(aligned[p], p) for p in PAIRS}
    if report['datasets'] != hashes or report['dataset'] != lab.digest([cross.PROTOCOL, cutoff, original, hashes]):
        raise ValueError('Review and historical dataset/cutoff differ')
    windows = {p: {s[i].ts: s[i:i+720] for s in aligned[p]
                   for i in range(lab.WARMUP, len(s)-719, 720)} for p in PAIRS}
    result = []
    for node in report['results']:
        h = node['horizon_hours']; wm.horizon_slots(h)
        for pair in PAIRS:
            saved = node['reports'][pair]
            if saved['dataset_sha256'] != hashes[pair] or saved['cutoff'] != cutoff or saved['horizon_hours'] != h:
                raise ValueError('Inner review identity mismatch')
            if saved['other_dataset_sha256'] != hashes[next(p for p in PAIRS if p != pair)]:
                raise ValueError('Peer dataset identity mismatch')
            details = saved['details']
            if [w['start'] for w in details] != report['window_starts'] or len(details) != report['windows'] or not details:
                raise ValueError('Window mismatch')
            totals = {scenario: {m: [] for m in ('candidate', 'parent', 'mean')} for scenario in SCENARIOS}
            for w in details:
                bars = windows[pair][w['start']]
                if w['train_last_label_end'] >= bars[0].ts: raise ValueError('Noncausal training')
                starts = list(range(0, 720-h, h))
                for name in ('candidate', 'parent', 'mean'):
                    m = w['models'][name]; rows = m['samples']; threshold = m['threshold']
                    if len(rows) != len(starts) or (m['calibration_last_label_end'] is not None and m['calibration_last_label_end'] >= bars[0].ts):
                        raise ValueError('Invalid calibration or coverage')
                    for row, i in zip(rows, starts):
                        if row['start'] != bars[i].ts or row['label_end'] != bars[i+h].ts or not math.isclose(row['realized'], bars[i+h].open/bars[i].open-1, abs_tol=1e-10) or not math.isfinite(row['prediction']):
                            raise ValueError('Saved sample differs from candles')
                    if threshold is not None and not math.isfinite(threshold): raise ValueError('Invalid threshold')
                    selected = {i for i,r in enumerate(rows) if threshold is not None and r['prediction'] > threshold}
                    if len(selected) != m['selected']: raise ValueError('Saved entry count differs')
                    for scenario, (fee, slip) in SCENARIOS.items():
                        a = weekly_review.account(bars, rows, selected, Costs(fee, slip, .25))
                        # Exact replay of the previous scenarios is an integrity check, not a new fit.
                        prev = m.get('normal' if scenario == 'legacy_normal' else 'stress' if scenario == 'legacy_stress' else '')
                        if scenario in ('legacy_normal','legacy_stress') and (prev is None or not math.isclose(a['net'], prev['net'], abs_tol=1e-10) or a['fills'] != prev['fills']):
                            raise ValueError('Saved account replay mismatch')
                        totals[scenario][name].append(a)
            summaries = {}
            for scenario, models in totals.items():
                stats = {name: {'mean_30d_net': statistics.mean(a['net'] for a in accounts),
                                'fills': sum(a['fills'] for a in accounts),
                                'active_windows': sum(a['fills'] > 0 for a in accounts),
                                'positive_windows': sum(a['net'] > 0 for a in accounts),
                                'worst_drawdown': max(a['drawdown'] for a in accounts)} for name, accounts in models.items()}
                v = stats['candidate']['mean_30d_net']
                stats['candidate'].update(economics(v, capital, daily_cost),
                     delta_mean=v-stats['mean']['mean_30d_net'], delta_parent=v-stats['parent']['mean_30d_net'])
                summaries[scenario] = stats
            result.append({'node':node['id'], 'agent':node['agent'], 'pair':pair, 'horizon_hours':h,
                           'windows':len(details), 'scenarios':summaries})
    return {'protocol':PROTOCOL, 'dataset':report['dataset'], 'cutoff':cutoff, 'development_only':True,
            'eligible':False, 'capital_usd':capital, 'daily_operating_usd':daily_cost,
            'scenarios':{k:{'fee_bps_per_side':f,'slippage_bps_per_side':s,'gross_break_even':breakeven(f,s)} for k,(f,s) in SCENARIOS.items()},
            'results':result, 'note':'Fixed historical selections, exact chronological account replay; no retuning or holdout. OKX US proxy uses Kraken price history, not OKX execution. Annual values are linear illustrations from equal 30-day windows; not predicted or compounded returns. Actual costs and account eligibility must be verified.'}


def funding_screen(dataset, cutoff, capital=200., daily_cost=.25):
    """Fixed notional gross funding receipts, not a tradable hedge return."""
    from . import funding_research as f, lab
    if dataset.get('protocol') != f.PROTOCOL or dataset.get('symbols') != f.SYMBOLS or dataset.get('events_sha256') != lab.digest(dataset['events']):
        raise ValueError('Funding dataset identity mismatch')
    # Cutoff is the timestamp of the last included closed hourly candle.
    end_month = datetime.fromtimestamp(cutoff+3600, timezone.utc).strftime('%Y-%m')
    start, _ = f.month_bounds(end_month); months=[]
    for _ in range(12):
        month=datetime.fromtimestamp(start-1,timezone.utc).strftime('%Y-%m'); months.insert(0,month); start,_=f.month_bounds(month)
    result=[]; notional=.25*capital
    for pair in PAIRS:
        rows=dataset['events'][pair]; f.validate_events(rows); records=[]
        for month in months:
            a,b=f.month_bounds(month); r=[x for x in rows if a*1000<=x[0]<b*1000]
            if not r or r[0][0]>a*1000+r[0][1]*3600000+300000 or r[-1][0]<(b-r[-1][1]*3600-300)*1000 or any(y[0]-x[0]>max(x[1],y[1])*3600000+300000 for x,y in zip(r,r[1:])):
                raise ValueError('Incomplete funding coverage: '+pair+' '+month)
            records.append({'month':month,'settlements':len(r),'gross_rate':sum(x[2] for x in r)})
        gross=sum(x['gross_rate'] for x in records); operating=daily_cost*365
        result.append({'pair':pair,'gross_12m_rate':gross,'gross_12m_usd_at_fixed_notional':gross*notional,
                       'positive_months':sum(x['gross_rate']>0 for x in records),'notional_usd':notional,
                       'gross_minus_operating_usd':gross*notional-operating,
                       'required_gross_rate_for_operating':operating/notional,
                       'required_productive_capital_usd_at_gross_rate':operating/(.25*gross) if gross>0 else None,
                       'illustrative_after_hedge_costs':[{'round_trip_cost_fraction_of_notional':c,'funding_minus_cost_usd':notional*(gross-c)} for c in (.01,.02,.04)],
                       'monthly':records})
    return {'dataset_sha256':dataset['events_sha256'],'months':months,'results':result,
            'note':'Binance USD-M archived rates, fixed hypothetical short notional, one-year sum of actual settlement rates. No spot or perpetual price P&L, margin, liquidation, execution, eligibility or realized profits. Cost scenarios are illustrations, not Bitnomial quotes.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home',required=True);p.add_argument('--report');p.add_argument('--output',required=True)
    p.add_argument('--capital',type=float,default=200.);p.add_argument('--daily-cost',type=float,default=.25);p.add_argument('--funding')
    x=p.parse_args(); home=Path(x.home); source=Path(x.report) if x.report else home/'history/horizon-review-v20.json'; out=Path(x.output)
    if out.exists():p.error('Choose a new output file')
    from . import multiyear
    report=json.loads(source.read_text(encoding='utf-8-sig'))
    parts={pair:multiyear.segments(home/'history'/name,report['cutoff']) for pair,name in zip(PAIRS,('btc-hourly-full.csv','eth-hourly-full.csv'))}
    result=replay(report,parts,x.capital,x.daily_cost)
    result['source_report_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
    fp=Path(x.funding) if x.funding else home/'history/funding-btc-eth-v22.json'
    if fp.exists(): result['funding_only']=funding_screen(json.loads(fp.read_text(encoding='utf-8-sig')),report['cutoff'],x.capital,x.daily_cost)
    elif x.funding: p.error('Specified funding file does not exist')
    else: result['funding_only']={'available':False,'note':'No local funding archive; directional review only'}
    out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2,allow_nan=False)
    print(json.dumps({'output':str(out),'scenarios':result['scenarios'],'funding_only':result['funding_only'],'results':[
        {k:v for k,v in r.items() if k!='scenarios'} | {'kraken_taker':r['scenarios']['kraken_taker']['candidate'], 'okx_us_proxy':r['scenarios']['okx_us_proxy']['candidate']} for r in result['results']]},indent=2,allow_nan=False))

if __name__=='__main__':main()

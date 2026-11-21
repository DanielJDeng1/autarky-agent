"""Development-only economic feasibility checks for autonomous research.

No predictive claims or live-trading authority. Replays a fixed model's existing
out-of-fold entry decisions at different fee assumptions without recalibration.
"""
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import statistics

from . import economic_review as economics

PROTOCOL = 'v0.30-execution-screen-1'
# These are explicit assumptions, not actual account statements or exchange fills.
SCENARIOS = {
    'legacy_normal': (40, 20),
    'kraken_taker_proxy': (80, 20),
    'legacy_stress': (80, 40),
    'lower_fee_proxy': (35, 20),
}


def replay_directional(report, aligned_bars, horizon, capital_usd=200., daily_cost_usd=.25):
    """Replay frozen, chronological research predictions without changing thresholds.

    `aligned_bars` contains contiguous segments of the same pair used in `report`.
    The original normal/stress results are re-evaluated as an integrity control.
    A success here is *never* independent forward evidence or real income.
    """
    from . import lab, weekly_review, weekly_models as wm
    from .core import Costs
    if report.get('development_only') is not True or report.get('eligible') is not False:
        raise ValueError('Only development-only forecast reports may be screened')
    if report.get('horizon_hours') != horizon: raise ValueError('Horizon identity mismatch')
    wm.horizon_slots(horizon)
    if not aligned_bars or not all(aligned_bars): raise ValueError('Missing market bars')
    # Ensure the *entire* OHLCV history is the same as the evaluator's inputs,
    # not merely the few entry/exit labels that happen to be replayed.
    fingerprint=lab.digest([report['pair'],[[asdict(b) for b in segment] for segment in aligned_bars]])
    if fingerprint!=report.get('dataset_sha256'): raise ValueError('Research dataset fingerprint mismatch')
    if report.get('cutoff') is None or aligned_bars[-1][-1].ts > report['cutoff']:
        raise ValueError('Historical data exceeds the report cutoff')
    if not math.isfinite(capital_usd) or capital_usd <= 0 or not math.isfinite(daily_cost_usd) or daily_cost_usd < 0:
        raise ValueError('Invalid economic parameters')
    dates={bars[i].ts:bars[i:i+720] for bars in aligned_bars
           for i in range(lab.WARMUP,len(bars)-719,720)}
    details=report.get('details')
    if not details: raise ValueError('No measured research windows')
    totals={k:{m:[] for m in ('candidate','parent','mean')} for k in SCENARIOS}
    by_year={k:{} for k in SCENARIOS}
    for w in details:
        start=w['start']; bars=dates.get(start)
        if bars is None or w['train_last_label_end']>=start:
            raise ValueError('Missing window or noncausal training')
        indices=list(range(0,720-horizon,horizon))
        for name in ('candidate','parent','mean'):
            m=w['models'][name]; samples=m['samples']
            if len(samples)!=len(indices) or (m['calibration_last_label_end'] is not None and m['calibration_last_label_end']>=start):
                raise ValueError('Missing or noncausal calibration')
            for row,i in zip(samples,indices):
                if row['start']!=bars[i].ts or row['label_end']!=bars[i+horizon].ts or not math.isclose(row['realized'],bars[i+horizon].open/bars[i].open-1,abs_tol=1e-10) or not math.isfinite(row['prediction']):
                    raise ValueError('Saved prediction labels disagree with historical bars')
            threshold=m['threshold']
            if threshold is not None and not math.isfinite(threshold): raise ValueError('Invalid frozen threshold')
            selected={i for i,r in enumerate(samples) if threshold is not None and r['prediction']>threshold}
            if len(selected)!=m['selected']: raise ValueError('Saved selections disagree with frozen threshold')
            for key,(fee,slip) in SCENARIOS.items():
                trial=weekly_review.account(bars,samples,selected,Costs(fee,slip,.25))
                if key in ('legacy_normal','legacy_stress'):
                    old=m['normal' if key=='legacy_normal' else 'stress']
                    if not math.isclose(trial['net'],old['net'],abs_tol=1e-10) or trial['fills']!=old['fills']:
                        raise ValueError('Saved cost-baseline account replay mismatch')
                totals[key][name].append(trial)
                if name=='candidate': by_year[key].setdefault(str(w['year']),[]).append(trial['net'])
    scenarios={}
    for key,models in totals.items():
        stats={name:{'mean_30d_net':statistics.mean(r['net'] for r in rows),
                     'fills':sum(r['fills'] for r in rows),
                     'active_windows':sum(r['fills']>0 for r in rows),
                     'positive_windows':sum(r['net']>0 for r in rows),
                     'windows':len(rows),
                     'worst_drawdown':max(r['drawdown'] for r in rows)}
               for name,rows in models.items()}
        mean=stats['candidate']['mean_30d_net']
        stats['candidate'].update(economics.economics(mean,capital_usd,daily_cost_usd),
                                  delta_mean=mean-stats['mean']['mean_30d_net'],
                                  delta_parent=mean-stats['parent']['mean_30d_net'],
                                  positive_years=sum(statistics.mean(x)>0 for x in by_year[key].values()),
                                  measured_years=len(by_year[key]))
        stats['fee_bps_per_side'],stats['slippage_bps_per_side']=SCENARIOS[key]
        scenarios[key]=stats
    k=scenarios['kraken_taker_proxy']['candidate']; stress=scenarios['legacy_stress']['candidate']
    profitable_after_assumed_costs=k['mean_30d_net']>0 and stress['mean_30d_net']>0 and k['delta_mean']>0 and k['delta_parent']>0
    return {'protocol':PROTOCOL,'development_only':True,'eligible':False,'cutoff':report['cutoff'],
            'horizon_hours':horizon,'pair':report['pair'],'windows':len(details),
            'profitable_after_assumed_costs':profitable_after_assumed_costs,
            'historical_operating_surplus_positive':k['annual_simple_surplus_usd']>0,
            'scenarios':scenarios,
            'note':'Replay of already-used development windows with frozen predictions and thresholds. Venue fees and slippage are explicit assumptions. Annualization is simple linear arithmetic, not a forecast; no live eligibility or proof of economic sustainability.'}


def load_review(home):
    """Read optional v0.29 report and verify its source fingerprint; do not fabricate results."""
    home=Path(home); path=home/'history/economic-review-v29.json'
    if not path.is_file():
        return {'available':False,'valid':False,'reason':'Run python -m autarky_trade.economic_review --home <home> --output <home>/history/economic-review-v29.json to produce an authenticated historical cost review.','ranked':[]}
    try:
        raw=json.loads(path.read_text(encoding='utf-8-sig'))
        source=home/'history/horizon-review-v20.json'
        if raw.get('protocol')!=economics.PROTOCOL or raw.get('development_only') is not True or raw.get('eligible') is not False:
            raise ValueError('Unexpected review protocol or evidence classification')
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=raw.get('source_report_sha256'):
            raise ValueError('Source report changed since the economic review was generated')
        original=json.loads(source.read_text(encoding='utf-8-sig'))
        if raw.get('cutoff') != original.get('cutoff') or raw.get('dataset') != original.get('dataset'):
            raise ValueError('Review dataset identity differs from historical source')
        if not all(isinstance(raw.get(k),(int,float)) and math.isfinite(raw[k]) for k in ('capital_usd','daily_operating_usd')) or raw['capital_usd']<=0 or raw['daily_operating_usd']<0:
            raise ValueError('Invalid capital or operating cost in saved review')
        ranked=[]
        for r in raw['results']:
            a=r['scenarios']; k=a['kraken_taker']['candidate']; s=a['legacy_stress']['candidate']; l=a['okx_us_proxy']['candidate']
            if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (k['mean_30d_net'],k['annual_simple_surplus_usd'],s['mean_30d_net'],l['mean_30d_net'])):
                raise ValueError('Non-finite economic result')
            ranked.append({'node':r['node'],'agent':r['agent'],'pair':r['pair'],'horizon_hours':r['horizon_hours'],
                           'windows':r['windows'],'kraken_taker_30d_net':k['mean_30d_net'],
                           'kraken_taker_annual_surplus_usd':k['annual_simple_surplus_usd'],
                           'kraken_taker_fills':k['fills'],'stress_30d_net':s['mean_30d_net'],
                           'lower_fee_proxy_30d_net':l['mean_30d_net'],
                           'development_only':True})
        ranked.sort(key=lambda x:(x['kraken_taker_annual_surplus_usd'],x['kraken_taker_30d_net']),reverse=True)
        if not ranked: raise ValueError('Empty verified economic review')
        return {'available':True,'valid':True,'cutoff':raw['cutoff'],'capital_usd':raw['capital_usd'],
                'daily_operating_usd':raw['daily_operating_usd'],'required_annual_return':raw['daily_operating_usd']*365/raw['capital_usd'],
                'ranked':ranked[:20],'candidates_tested':len(ranked),
                'positive_cost_adjusted':sum(x['kraken_taker_30d_net']>0 for x in ranked),
                'positive_operating_surplus':sum(x['kraken_taker_annual_surplus_usd']>0 for x in ranked),
                'note':'These are reused historical development windows, not fresh evidence or verified venue execution.'}
    except (OSError,KeyError,TypeError,ValueError,ZeroDivisionError,UnicodeError) as exc:
        return {'available':True,'valid':False,'reason':str(exc),'ranked':[]}


def compact(review,limit=6):
    """Bound prompt size; keep unfavorable economic evidence visible to the agents."""
    if not review.get('valid'): return {k:review[k] for k in ('available','valid','reason') if k in review}
    return {k:review[k] for k in ('available','valid','capital_usd','daily_operating_usd','required_annual_return','candidates_tested','positive_cost_adjusted','positive_operating_surplus','note')} | {'ranked':review['ranked'][:limit]}


def create_review_if_missing(home, parts, cutoff):
    """Build a historical cost review once from existing authenticated inputs.

    It is deliberately not repeated on each restart; no random strategy search,
    research-fee charge, account mutation, or live brokerage interaction occurs.
    The file is made visible atomically only after the entire replay succeeds.
    """
    import os
    import tempfile
    home=Path(home); path=home/'history/economic-review-v29.json'
    if path.exists():
        result=load_review(home)
        if not result['valid']: raise ValueError('Existing review is invalid: '+result['reason']+'; preserve it and regenerate manually under a new name')
        return {'generated':False,'review':compact(result)}
    source=home/'history/horizon-review-v20.json'
    if not source.is_file(): raise ValueError('No saved horizon-review-v20.json; no cost replay available')
    source_bytes=source.read_bytes(); base=json.loads(source_bytes.decode('utf-8-sig'))
    if base.get('cutoff')!=cutoff: raise ValueError('Saved research cutoff differs from running historical dataset')
    result=economics.replay(base,parts)
    result['source_report_sha256']=hashlib.sha256(source_bytes).hexdigest()
    funding=home/'history/funding-btc-eth-v22.json'
    if funding.is_file():
        try: result['funding_only']=economics.funding_screen(json.loads(funding.read_text(encoding='utf-8-sig')),cutoff)
        except (ValueError,KeyError,TypeError) as exc:
            result['funding_only']={'available':False,'error':str(exc)}
    else: result['funding_only']={'available':False}
    fd,tmp=tempfile.mkstemp(prefix='.economic-review-',suffix='.partial',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(result,f,indent=2,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        # The runtime is single-owner; never overwrite a user-generated report.
        if path.exists(): raise ValueError('Economic review appeared during replay; preserving it')
        os.replace(tmp,path)
    finally:
        if Path(tmp).exists(): Path(tmp).unlink()
    checked=load_review(home)
    if not checked['valid']: raise ValueError('Generated economic review failed integrity checks: '+checked.get('reason','unknown'))
    return {'generated':True,'review':compact(checked)}


def compact_execution(screen):
    """Send decision-relevant findings, not a second copy of all 30-day metrics."""
    if not isinstance(screen,dict) or screen.get('protocol')!=PROTOCOL:
        return {'valid':False,'reason':'No trusted v0.30 execution screen'}
    out={'development_only':True,'eligible':False,
         'profitable_after_assumed_costs':screen['profitable_after_assumed_costs'],
         'historical_operating_surplus_positive':screen['historical_operating_surplus_positive'],
         'market':screen['pair'],'horizon_hours':screen['horizon_hours']}
    out['cost_scenarios']={}
    for name in ('kraken_taker_proxy','legacy_stress','lower_fee_proxy'):
        v=screen['scenarios'][name]['candidate']
        out['cost_scenarios'][name]={k:v[k] for k in ('mean_30d_net','annual_simple_surplus_usd','fills','positive_windows','windows')}
    return out

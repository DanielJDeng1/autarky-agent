"""Operator-owned, one-shot evaluation of frozen multi-market strategy portfolios.

This is a research review, not exchange authorization. No final data goes to an LLM.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import closing
from dataclasses import asdict
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import time

from .core import Bar, Costs, Strategy, backtest_path, validate
from .evidence import Passive, block_interval
from .research import PROTOCOL as RESEARCH_PROTOCOL, identity, RESEARCH_COSTS, STRESS_COSTS

PROTOCOL = 'v0.7-final-portfolio-1'
WARMUP = 101
MIN_DAYS = 90
MIN_FILLS = 20
MAX_DRAWDOWN = .08
MAX_SLEEVE_WEIGHT = .60
MAX_CANDLES = 20000
PAIRS = ('XBTUSD', 'ETHUSD')
INTERVALS = (15, 60, 240, 1440)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def is_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def connect(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
      CREATE TABLE IF NOT EXISTS final_plans (
        id TEXT PRIMARY KEY, created INTEGER NOT NULL, state TEXT NOT NULL,
        plan TEXT NOT NULL, result TEXT, error TEXT);
      CREATE TABLE IF NOT EXISTS final_windows (
        plan_id TEXT NOT NULL REFERENCES final_plans(id), pair TEXT NOT NULL,
        start INTEGER NOT NULL, end_exclusive INTEGER NOT NULL,
        PRIMARY KEY(plan_id, pair));
    ''')
    return c


def read_trial(path, agent, round_id, start):
    """Read-only snapshot of an actual completed eligible research trial."""
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        c.execute('BEGIN')
        cfg = c.execute('SELECT pair,interval,last_holdout,last_ts FROM treasury').fetchone()
        row = c.execute('''SELECT t.report,r.test_end,r.source FROM trials t
          JOIN rounds r ON r.id=t.round_id WHERE t.agent=? AND t.round_id=?''',
          (agent, round_id)).fetchone()
        if not cfg or not row:
            raise ValueError('Unknown source trial')
        report = json.loads(row['report'])
        if row['source'] not in ('csv', 'kraken') or report.get('protocol') != RESEARCH_PROTOCOL or report.get('eligible') is not True:
            raise ValueError('Only an eligible current-protocol actual-market trial can be frozen')
        strategy = Strategy(**report['strategy'])
        if report.get('strategy_id') != identity(strategy) or not is_digest(report.get('dataset_sha256')):
            raise ValueError('Invalid trial identity or dataset digest')
        # Include archived history, not just the last test. The archive is optional.
        last_archive = 0
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_bars'").fetchone():
            last_archive = c.execute('SELECT coalesce(max(ts),0) FROM market_bars').fetchone()[0]
        if start <= max(row['test_end'], cfg['last_holdout'], cfg['last_ts'], last_archive):
            raise ValueError('Final window overlaps known research/market history in source DB')
        if strategy.model and strategy.model['fit_end'] >= start:
            raise ValueError('Model training overlaps final evaluation')
        # Candidate count is descriptive; these are not independent trials.
        reports = [json.loads(r[0]) for r in c.execute('SELECT report FROM trials')]
        count = sum(len(r.get('candidates', [])) for r in reports)
        return dict(pair=cfg['pair'], interval=cfg['interval'], agent=agent, round_id=round_id,
                    strategy=asdict(strategy), strategy_id=identity(strategy),
                    development_end=row['test_end'], development_sha256=report['dataset_sha256'],
                    source_completed_candidate_evaluations=count)


def freeze(path, spec, now=None):
    """Register exact artifacts, weights, costs and dates before opening final data.

    Prospective windows start in the future. Sealed historical windows require a
    custodian's byte digest; they remain retrospective and cannot pass review.
    """
    now = int(time.time()) if now is None else now
    if not isinstance(spec, dict) or set(spec) != {'name', 'mode', 'start', 'end', 'legs'}:
        raise ValueError('Expected name, mode, start, end, legs')
    name, mode, start, end, inputs = (spec[k] for k in ('name','mode','start','end','legs'))
    if not isinstance(name, str) or not 1 <= len(name) <= 100 or mode not in ('prospective', 'sealed'):
        raise ValueError('Invalid plan name or mode')
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise ValueError('Invalid UTC timestamps')
    if mode == 'prospective' and start <= now:
        raise ValueError('Prospective evaluation must start after registration')
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= len(PAIRS):
        raise ValueError('Use one or two distinct supported market sleeves')
    legs = []
    for item in inputs:
        expected = {'db','agent','round_id','weight_bps'} | ({'csv_sha256'} if mode == 'sealed' else set())
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError('Invalid sleeve fields')
        w = item['weight_bps']
        if type(w) is not int or not 0 < w <= 5000 or type(item['round_id']) is not int or item['round_id'] < 1:
            raise ValueError('Invalid sleeve weight or round')
        if Path(item['db']).resolve() == Path(path).resolve():
            raise ValueError('Final ledger must be separate from research DB')
        leg = read_trial(item['db'], item['agent'], item['round_id'], start)
        if leg['pair'] not in PAIRS or leg['interval'] not in INTERVALS:
            raise ValueError('Unsupported market or interval')
        leg['weight_bps'] = w
        if mode == 'sealed':
            if not is_digest(item['csv_sha256']):
                raise ValueError('Sealed dataset requires SHA-256 of exact CSV bytes')
            leg['csv_sha256'] = item['csv_sha256']
        legs.append(leg)
    legs.sort(key=lambda x: x['pair'])
    if len({x['pair'] for x in legs}) != len(legs) or len({x['interval'] for x in legs}) != 1:
        raise ValueError('One sleeve per market and a common candle interval are required')
    if sum(x['weight_bps'] for x in legs) > 8000:
        raise ValueError('Keep at least 20% cash reserve at inception')
    step = legs[0]['interval'] * 60
    if start % step or (end-start) % step or end-start+step < MIN_DAYS*86400 or not 600 <= (end-start)//step+1 <= MAX_CANDLES:
        raise ValueError('Final window must be 90+ days, 600..20000 candles, interval aligned')
    if mode == 'sealed' and end+step > now:
        raise ValueError('A sealed historical window must already be complete')
    if start < WARMUP*step:
        raise ValueError('Insufficient warmup timestamps')
    plan = dict(protocol=PROTOCOL, research_protocol=RESEARCH_PROTOCOL, name=name, mode=mode,
                start=start, end=end, step=step, legs=legs, warmup=WARMUP,
                costs=asdict(RESEARCH_COSTS), stress_costs=asdict(STRESS_COSTS),
                minimum_fills=MIN_FILLS, maximum_drawdown=MAX_DRAWDOWN, maximum_sleeve_weight=MAX_SLEEVE_WEIGHT,
                bootstrap_days=[1,3,7], bootstrap_resamples=999)
    key = digest(canonical(plan).encode())
    c = connect(path)
    try:
        c.execute('BEGIN IMMEDIATE')
        for leg in legs:
            if c.execute('SELECT 1 FROM final_windows WHERE pair=? AND start<? AND end_exclusive>?',
                         (leg['pair'], end+step, start)).fetchone():
                raise ValueError('Final window already reserved; overlapping tests are prohibited')
        c.execute('INSERT INTO final_plans VALUES(?,?,?,?,NULL,NULL)', (key, now, 'registered', canonical(plan)))
        c.executemany('INSERT INTO final_windows VALUES(?,?,?,?)',
                      [(key, x['pair'], start, end+step) for x in legs])
        c.commit()
    finally:
        c.close()
    return {'id': key, 'state': 'registered', 'plan': plan}


def read_final(path, plan, leg):
    """Hash and parse the same bytes; require exact warmup and evaluation span."""
    with open(path, 'rb') as f:
        raw = f.read(20_000_001)
    if len(raw) > 20_000_000:
        raise ValueError('Final dataset exceeds size limit')
    sha = digest(raw)
    if leg.get('csv_sha256', sha) != sha:
        raise ValueError('Sealed dataset digest mismatch')
    rows = csv.reader(io.StringIO(raw.decode('utf-8-sig')))
    bars = []
    for i, row in enumerate(rows):
        if i == 0 and row and row[0].lower() in ('timestamp','time','ts'):
            continue
        if len(row) < 6:
            raise ValueError('Invalid OHLCV row')
        bars.append(Bar(int(row[0]), *(float(x) for x in row[1:6])))
        if len(bars) > MAX_CANDLES+WARMUP:
            raise ValueError('Too many bars')
    validate(bars, plan['step'])
    if (bars[0].ts != plan['start']-WARMUP*plan['step'] or bars[WARMUP].ts != plan['start']
            or bars[-1].ts != plan['end']):
        raise ValueError('CSV must match the exact registered interval plus 101 warmup candles')
    return bars, sha


def nav(logs):
    out = [1.]
    for x in logs:
        out.append(out[-1]*math.exp(x))
    return out


def combine(paths, weights):
    """Independent funded sleeves, fixed initial allocation, no rebalancing."""
    if (not paths or not paths[0] or len(paths) != len(weights)
            or any(len(p) != len(paths[0]) for p in paths)
            or not all(math.isfinite(w) and w>0 for w in weights) or sum(weights)>1
            or any(not math.isfinite(v) or v<=0 for p in paths for v in p)):
        raise ValueError('Unaligned portfolio paths')
    reserve = 1-sum(weights)
    values = [reserve+sum(w*v for w,v in zip(weights, row)) for row in zip(*paths)]
    peak = values[0]
    dd = 0.
    for value in values:
        peak = max(peak, value)
        dd = max(dd, 1-value/peak)
    return values, {'return': values[-1]/values[0]-1, 'drawdown': dd,
                    'initial_cash_weight': reserve,
                    'maximum_sleeve_equity_weight': max(w*p[i]/values[i] for p,w in zip(paths,weights) for i in range(len(values)))}


def correlation(a,b):
    n=len(a)
    ma,mb=sum(a)/n,sum(b)/n
    da=[x-ma for x in a]; db=[x-mb for x in b]
    denom=math.sqrt(sum(x*x for x in da)*sum(x*x for x in db))
    return None if denom==0 else max(-1.,min(1.,sum(x*y for x,y in zip(da,db))/denom))


def analyze(plan, inputs):
    """Deterministic, aligned historical evaluation. No LLM or model refit."""
    active, passive, stressed, logs, legs = [], [], [], [], []
    costs, stress_costs = Costs(**plan['costs']), Costs(**plan['stress_costs'])
    for leg in plan['legs']:
        bars, sha = read_final(inputs[leg['pair']], plan, leg)
        s = Strategy(**leg['strategy'])
        if identity(s) != leg['strategy_id']:
            raise ValueError('Frozen artifact identity mismatch')
        a, ar = backtest_path(bars,s,costs,WARMUP,len(bars))
        p, pr = backtest_path(bars,Passive(),costs,WARMUP,len(bars))
        z, zr = backtest_path(bars,s,stress_costs,WARMUP,len(bars))
        active.append(nav(ar)); passive.append(nav(pr)); stressed.append(nav(zr)); logs.append(ar)
        legs.append(dict(pair=leg['pair'], strategy_id=leg['strategy_id'], csv_sha256=sha,
                         weight_bps=leg['weight_bps'], active=a, passive=p, stress=z))
    weights=[x['weight_bps']/10000 for x in legs]
    av, am=combine(active,weights); pv, pm=combine(passive,weights); _, zm=combine(stressed,weights)
    excess=[math.log(y/x)-math.log(v/u) for x,y,u,v in zip(av,av[1:],pv,pv[1:])]
    intervals={str(d): block_interval(excess, min(len(excess)//2, max(2,d*86400//plan['step'])))
               for d in plan['bootstrap_days']}
    checks={'portfolio_positive':am['return']>0, 'beats_passive':am['return']>pm['return'],
            'portfolio_drawdown':am['drawdown']<plan['maximum_drawdown'],
            'concentration_bounded':am['maximum_sleeve_equity_weight']<=plan['maximum_sleeve_weight'],
            'stress_positive_and_bounded':zm['return']>0 and zm['drawdown']<MAX_DRAWDOWN,
            'all_sleeves_positive_and_bounded':all(x['active']['return']>0 and x['stress']['return']>0 and x['active']['trades']>=plan['minimum_fills'] and not x['active']['stopped'] and not x['stress']['stopped'] for x in legs),
            'positive_bootstrap_lower_bounds':all(v[0]>0 for v in intervals.values())}
    return {'mode':plan['mode'],'observations':len(excess),'legs':legs,'portfolio':am,
            'passive_portfolio':pm,'stress_portfolio':zm,'checks':checks,
            'paired_excess_mean_intervals_95_by_block_days':intervals,
            'sleeve_log_return_correlation':correlation(*logs) if len(logs)==2 else None,
            'research_checks_pass':all(checks.values()),
            'eligible_for_manual_review':plan['mode']=='prospective' and all(checks.values()),
            'note':'Historical OHLC replay; no depth, portfolio-wide liquidation, or actual fills. Bootstrap is descriptive and not multiplicity-adjusted. Never authorizes live trading.'}


def evaluate(path, key, inputs, now=None):
    now=int(time.time()) if now is None else now
    c=connect(path)
    try:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM final_plans WHERE id=?',(key,)).fetchone()
        if not row or row['state']!='registered':
            raise ValueError('Plan missing or already consumed')
        plan=json.loads(row['plan'])
        if digest(canonical(plan).encode())!=key or plan['protocol']!=PROTOCOL or plan['research_protocol']!=RESEARCH_PROTOCOL:
            raise ValueError('Plan integrity or version mismatch')
        if now<plan['end']+plan['step']:
            raise ValueError('Evaluation window is not complete')
        if set(inputs)!={x['pair'] for x in plan['legs']}:
            raise ValueError('Supply exactly one CSV per registered market')
        # Commit consumption before any final-data file is opened. A crash is terminal.
        c.execute("UPDATE final_plans SET state='consumed' WHERE id=?",(key,)); c.commit()
        try:
            result=analyze(plan,inputs)
            c.execute("UPDATE final_plans SET state='completed',result=? WHERE id=?",(canonical(result),key)); c.commit()
        except Exception as exc:
            c.execute("UPDATE final_plans SET state='failed',error=? WHERE id=?",(str(exc)[:1000],key)); c.commit()
            raise
        return {'id':key,'state':'completed','result':result}
    finally:
        c.close()


def status(path):
    c=connect(path)
    try:
        return [dict(id=r['id'], created=r['created'], state=r['state'], plan=json.loads(r['plan']),
                     result=json.loads(r['result']) if r['result'] else None,error=r['error'])
                for r in c.execute('SELECT * FROM final_plans ORDER BY created,id')]
    finally:
        c.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',default='data/final_evaluation.sqlite')
    sub=p.add_subparsers(dest='cmd',required=True)
    f=sub.add_parser('freeze'); f.add_argument('spec')
    e=sub.add_parser('evaluate'); e.add_argument('id'); e.add_argument('--csv',action='append',required=True,metavar='PAIR=PATH')
    sub.add_parser('status')
    a=p.parse_args()
    if a.cmd=='freeze': result=freeze(a.db,json.loads(Path(a.spec).read_text(encoding='utf-8')))
    elif a.cmd=='evaluate':
        inputs={}
        for item in a.csv:
            pair,sep,file=item.partition('=')
            if not sep or not file or pair in inputs: p.error('Use unique PAIR=PATH entries')
            inputs[pair]=file
        result=evaluate(a.db,a.id,inputs)
    else: result=status(a.db)
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__': main()

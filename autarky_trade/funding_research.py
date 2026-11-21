"""Checksum-verified public funding data and matched spot-return research (no derivatives trading)."""
import argparse
from bisect import bisect_right
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from urllib.request import urlopen, Request
from zipfile import ZipFile

from . import cross_asset as cross, lab, multiyear, weekly_models as wm

PROTOCOL = 'v0.22-funding-1'
SYMBOLS = {'XBTUSD':'BTCUSDT', 'ETHUSD':'ETHUSDT'}
BASE = 'https://data.binance.vision/data/futures/um/monthly/fundingRate/'
RISK = (('volatility',168),('volatility',672),('volume_ratio',168),('volume_ratio',672))
FUNDING = (('funding_mean',24),('funding_mean',168))
SPECS = {'mean':(), 'risk_long':RISK, 'funding_only':FUNDING, 'risk_plus_funding':RISK+FUNDING}
LAG = 3600  # Conservative availability lag; settlement timestamp is not a publication timestamp.


def month_bounds(month):
    if not isinstance(month,str) or not re.fullmatch(r'20\d\d-(0[1-9]|1[0-2])',month):
        raise ValueError('Expected YYYY-MM')
    start = datetime.strptime(month,'%Y-%m').replace(tzinfo=timezone.utc)
    end = start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
    return int(start.timestamp()),int(end.timestamp())


def months(first,last):
    a,_=month_bounds(first); b,end=month_bounds(last)
    if a>b or end>time.time(): raise ValueError('Choose ordered, completed UTC months')
    result=[]; current=first
    while True:
        result.append(current)
        if current==last: return result
        current=datetime.fromtimestamp(month_bounds(current)[1],timezone.utc).strftime('%Y-%m')


def fetch(url):
    with urlopen(Request(url,headers={'User-Agent':'Autarky-research/0.22'}),timeout=45) as r:
        data=r.read(2_000_001)
    if len(data)>2_000_000: raise ValueError('Unexpectedly large funding download')
    return data


def parse_archive(data, checksum, symbol, month):
    if symbol not in SYMBOLS.values(): raise ValueError('Unsupported funding symbol')
    name=f'{symbol}-fundingRate-{month}'
    fields=checksum.decode('ascii').strip().split()
    digest=hashlib.sha256(data).hexdigest()
    if len(fields)!=2 or fields[0].lower()!=digest or fields[1].lstrip('*')!=name+'.zip':
        raise ValueError('Funding archive checksum or filename mismatch')
    start,end=month_bounds(month)
    with ZipFile(io.BytesIO(data)) as z:
        if z.namelist()!=[name+'.csv'] or z.getinfo(name+'.csv').file_size>2_000_000:
            raise ValueError('Unexpected funding archive contents')
        reader=csv.DictReader(io.StringIO(z.read(name+'.csv').decode('utf-8-sig')))
        if reader.fieldnames!=['calc_time','funding_interval_hours','last_funding_rate']:
            raise ValueError('Unexpected funding CSV schema')
        events=[]
        for row in reader:
            ms=int(row['calc_time']); interval=int(row['funding_interval_hours']); rate=float(row['last_funding_rate'])
            if not start*1000<=ms<end*1000 or not 1<=interval<=24 or not math.isfinite(rate) or abs(rate)>1:
                raise ValueError('Invalid funding event')
            # Preserve millisecond timestamps: never round an observation backward.
            events.append([ms,interval,rate])
    validate_events(events)
    return events,digest


def validate_events(events):
    if not events: raise ValueError('No funding events')
    previous=-1
    for ms,interval,rate in events:
        if type(ms) is not int or ms<=previous or type(interval) is not int or not 1<=interval<=24 or not math.isfinite(rate) or abs(rate)>1:
            raise ValueError('Invalid or unordered funding events')
        previous=ms


def download(first,last,fetcher=fetch):
    data={}; sources=[]
    for pair,symbol in SYMBOLS.items():
        events=[]
        for month in months(first,last):
            name=f'{symbol}-fundingRate-{month}.zip'; url=BASE+symbol+'/'+name
            raw=fetcher(url); checksum=fetcher(url+'.CHECKSUM')
            rows,digest=parse_archive(raw,checksum,symbol,month)
            events.extend(rows); sources.append({'symbol':symbol,'month':month,'url':url,'sha256':digest})
        validate_events(events); data[pair]=events
    return {'protocol':PROTOCOL,'source':'Binance USD-M public monthly funding archives',
            'symbols':SYMBOLS,'first_month':first,'last_month':last,'events':data,'archives':sources,
            'events_sha256':lab.digest(data),'development_only':True,'eligible':False}


def asof(events, ts, times=None):
    # At time t, only settlements at or before t-1h are usable. Reject stale values.
    times=[r[0] for r in events] if times is None else times
    i=bisect_right(times,(ts-LAG)*1000)-1
    if i<0: return None
    ms,interval,rate=events[i]
    if (ts-LAG)*1000-ms>interval*3600*1000+300_000: return None
    return rate*8/interval


def matched(parts, events):
    if set(events)!=set(SYMBOLS): raise ValueError('Both funding series required')
    for rows in events.values(): validate_events(rows)
    times={p:[r[0] for r in events[p]] for p in SYMBOLS}
    aligned=cross.align(parts); out={p:[] for p in SYMBOLS}; values={p:{} for p in SYMBOLS}
    prev=None
    for a,b in zip(aligned['XBTUSD'],aligned['ETHUSD']):
        for x,y in zip(a,b):
            ts=x.ts; v={p:asof(events[p],ts,times[p]) for p in SYMBOLS}
            if any(r is None for r in v.values()): prev=None; continue
            if prev is None or ts!=prev+3600:
                for p in SYMBOLS: out[p].append([])
            for p,bar in zip(SYMBOLS,(x,y)):
                out[p][-1].append(bar); values[p][ts]=v[p]
            prev=ts
    if not out['XBTUSD']: raise ValueError('No jointly covered spot/funding history')
    return out,values


def feature_series(bars, values, key):
    feature,w=key
    if feature!='funding_mean' or w not in (24,168): raise ValueError('Unsupported funding feature')
    prefix=[0.]
    for b in bars: prefix.append(prefix[-1]+values[b.ts])
    # Each hourly value already has a one-hour availability lag.
    return [None if i+1<w else (prefix[i+1]-prefix[i+1-w])/w for i in range(len(bars))]


def review(parts, dataset, cutoff, horizons=(168,336), now=None):
    if dataset.get('protocol')!=PROTOCOL or dataset.get('symbols')!=SYMBOLS or dataset.get('events_sha256')!=lab.digest(dataset['events']):
        raise ValueError('Funding dataset identity mismatch')
    if not horizons or len(set(horizons))!=len(horizons): raise ValueError('Require distinct horizons')
    for h in horizons: wm.horizon_slots(h)
    matched_parts,values=matched(parts,dataset['events'])
    results=[]; dates=None
    for h in horizons:
        for p in SYMBOLS:
            r=wm.review(matched_parts[p],p,cutoff,now,specs=SPECS,horizon=h,
                        feature_provider=lambda bars,k:feature_series(bars,values[p],k))
            starts=[w['start'] for w in r['details']]
            if dates is None: dates=starts
            if starts!=dates: raise ValueError('Evaluation windows differ')
            for name,m in r['summary'].items():
                results.append({'pair':p,'horizon_hours':h,'model':name,**m,
                    'stress_delta_mean':m['mean_stress']-r['summary']['mean']['mean_stress'],
                    'stress_delta_risk':m['mean_stress']-r['summary']['risk_long']['mean_stress'],
                    'rmse_skill':1-m['rmse']/r['summary']['mean']['rmse'] if r['summary']['mean']['rmse'] else None})
    return {'protocol':PROTOCOL,'cutoff':cutoff,'funding_sha256':dataset['events_sha256'],
            'spot_sha256':{p:lab.digest([p,[[wm.asdict(b) for b in s] for s in matched_parts[p]]]) for p in SYMBOLS},
            'aligned_bars':sum(map(len,matched_parts['XBTUSD'])),'windows':len(dates),'window_starts':dates,
            'specifications':SPECS,'results':results,'development_only':True,'eligible':False,
            'note':'Binance perpetual funding is an external signal for Kraken spot, not derivatives execution. One-hour assumed availability lag; no point-in-time publication archive. Missing/stale observations split both markets. Matched historical windows; no independent holdout.'}


def write(path, result):
    path=Path(path)
    if path.exists(): raise ValueError('Output exists; choose a new path')
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.partial')
    with temp.open('x',encoding='utf-8') as f: json.dump(result,f,indent=2,allow_nan=False)
    temp.replace(path)


def main():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('download'); d.add_argument('--first',default='2021-01'); d.add_argument('--last',required=True); d.add_argument('--output',required=True)
    r=sub.add_parser('review'); r.add_argument('--funding',required=True); r.add_argument('--btc',required=True); r.add_argument('--eth',required=True); r.add_argument('--cutoff',type=int,required=True); r.add_argument('--horizons',type=int,nargs='+',choices=wm.HORIZONS,default=[168,336]); r.add_argument('--output',required=True)
    x=p.parse_args()
    if Path(x.output).exists(): p.error('Output exists; choose a new path')
    if x.cmd=='download':
        result=download(x.first,x.last); write(x.output,result)
        print(json.dumps({k:v for k,v in result.items() if k not in ('events','archives')} | {'counts':{p:len(v) for p,v in result['events'].items()},'archive_count':len(result['archives'])},indent=2))
    else:
        if Path(x.output).resolve() in {Path(v).resolve() for v in (x.funding,x.btc,x.eth)}: p.error('Output must differ from inputs')
        result=review({'XBTUSD':multiyear.segments(x.btc,x.cutoff),'ETHUSD':multiyear.segments(x.eth,x.cutoff)},json.loads(Path(x.funding).read_text(encoding='utf-8-sig')),x.cutoff,x.horizons)
        write(x.output,result); print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__': main()

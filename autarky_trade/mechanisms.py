"""Bounded public-data research for same-venue spot/perpetual funding carry.
No trading authority. Archived prices are trade-price proxies, not liquidation marks.
"""
from datetime import datetime, timezone
from pathlib import Path
import csv, hashlib, io, json, math, statistics
from zipfile import ZipFile
from . import funding_research as funding, lab
from .core import Bar

PROTOCOL='v0.28-carry-1'
BASE='https://data.binance.vision/data/'


def parse_bars(raw, checksum, symbol, month, market):
    name=f'{symbol}-1h-{month}'; sha=hashlib.sha256(raw).hexdigest()
    fields=checksum.decode('ascii').split()
    if len(fields)!=2 or fields[0].lower()!=sha or fields[1].lstrip('*')!=name+'.zip': raise ValueError('Kline checksum mismatch')
    start,end=funding.month_bounds(month); bars=[]
    with ZipFile(io.BytesIO(raw)) as z:
        if z.namelist()!=[name+'.csv'] or z.getinfo(name+'.csv').file_size>2_000_000: raise ValueError('Unexpected archive')
        for i,r in enumerate(csv.reader(io.StringIO(z.read(name+'.csv').decode('utf-8-sig')))):
            if i==0 and r[0] in ('open_time','Open time','openTime'): continue
            if len(r)!=12: raise ValueError('Unexpected kline columns')
            stamp=int(r[0]); unit=1_000_000 if stamp>=10**15 else 1000
            if stamp%unit: raise ValueError('Nonintegral kline timestamp')
            t=stamp//unit; b=Bar(t,*(float(x) for x in r[1:6]))
            if not start<=t<end or t%3600 or not all(math.isfinite(x) for x in (b.open,b.high,b.low,b.close,b.volume)) or min(b.open,b.close,b.low,b.high)<=0 or b.low>min(b.open,b.close) or b.high<max(b.open,b.close) or b.volume<0: raise ValueError('Invalid kline')
            if bars and t!=bars[-1].ts+3600: raise ValueError('Missing/duplicate kline')
            bars.append(b)
    if len(bars)!=(end-start)//3600 or bars[0].ts!=start: raise ValueError('Incomplete month')
    return bars,sha


def get_month(cache,symbol,month,market,fetcher=funding.fetch):
    folder=Path(cache)/market/symbol; folder.mkdir(parents=True,exist_ok=True)
    name=f'{symbol}-1h-{month}.zip'; path=folder/name; check=folder/(name+'.CHECKSUM')
    prefix='spot/monthly/klines/' if market=='spot' else 'futures/um/monthly/klines/'
    url=BASE+prefix+symbol+'/1h/'+name
    if path.exists() and check.exists(): raw,checksum=path.read_bytes(),check.read_bytes()
    else:
        raw,checksum=fetcher(url),fetcher(url+'.CHECKSUM')
        parse_bars(raw,checksum,symbol,month,market)
        # Atomic individual writes; a partial cache pair is fetched again.
        for p,b in ((path,raw),(check,checksum)):
            tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_bytes(b);tmp.replace(p)
    bars,sha=parse_bars(raw,checksum,symbol,month,market)
    return bars,{'market':market,'month':month,'url':url,'sha256':sha}


def window(spot,perp,events,fee_bps,slip_bps,capital=200.,maintenance=.10):
    """Equal base quantity long spot/short perp, collateral >= short entry notional.
    25% gross allocation per leg, unallocated cash remains idle. Exit last close;
    conservative intrabar margin failure closes at adverse high/low proxy.
    Funding cash flows use settlement-hour open as mark proxy (not actual mark).
    """
    if not spot or len(spot)!=len(perp) or any(a.ts!=b.ts for a,b in zip(spot,perp)): raise ValueError('Unaligned carry window')
    if any(b.ts-a.ts!=3600 for a,b in zip(spot,spot[1:])): raise ValueError('Carry gap')
    if not all(math.isfinite(v) and v>=0 for v in (fee_bps,slip_bps)) or not 0<capital or not 0<maintenance<1: raise ValueError('Invalid costs')
    fee,slip=fee_bps/10000,slip_bps/10000
    qty=.25*capital/max(spot[0].open*(1+slip),perp[0].open*(1+slip)); sb=spot[0].open*(1+slip); pb=perp[0].open*(1-slip)
    collateral=qty*perp[0].open; entryfee=qty*(sb+pb)*fee; idle=capital-qty*sb-collateral-entryfee
    if idle<0: raise ValueError('Insufficient initial collateral')
    receipts=0.; events_used=0; worst=0.; peak=capital; forced=False; index=0
    # Never claim funding at entry timestamp or after exit. Settlement strictly after entry.
    start=spot[0].ts; end=spot[-1].ts+3600; selected=[r for r in events if start*1000<r[0]<end*1000]
    for s,p in zip(spot,perp):
        # Check intrabar margin before counting any later funding in that hour.
        margin=collateral+qty*(pb-p.high)+receipts
        if margin<=maintenance*qty*p.high:
            forced=True; sp=s.low*(1-slip); pp=p.high*(1+slip)
        else:
            while index<len(selected) and selected[index][0]<(p.ts+3600)*1000:
                receipts+=qty*p.open*selected[index][2]; events_used+=1; index+=1
            sp=s.close*(1-slip); pp=p.close*(1+slip)
        value=idle+qty*sp+collateral+qty*(pb-pp)+receipts-qty*(sp+pp)*fee
        # Conservative drawdown: simultaneous adverse intrabar extremes; not a tradable mark.
        adverse=idle+qty*s.low*(1-slip)+collateral+qty*(pb-p.high*(1+slip))+receipts-qty*(s.low*(1-slip)+p.high*(1+slip))*fee
        worst=max(worst,1-adverse/peak);peak=max(peak,value)
        if forced: break
    basis=qty*((sp-sb)+(pb-pp)); fees=entryfee+qty*(sp+pp)*fee
    net=capital+basis+receipts-fees
    return {'net_return':net/capital-1,'funding_usd':receipts,'hedge_price_pnl_usd':basis,'fees_usd':fees,'drawdown_proxy':worst,'forced_exit_proxy':forced,'funding_events':events_used,'capital_usd':capital}


def review(cache,dataset,pair,cutoff,count,fetcher=funding.fetch):
    if pair not in funding.SYMBOLS or type(count) is not int or count not in (3,6,12): raise ValueError('Choose pair and 3, 6 or 12 months')
    if dataset.get('protocol')!=funding.PROTOCOL or dataset.get('symbols')!=funding.SYMBOLS or dataset.get('events_sha256')!=lab.digest(dataset['events']): raise ValueError('Funding identity mismatch')
    events=dataset['events'][pair]; funding.validate_events(events)
    last=datetime.fromtimestamp(cutoff+3600,timezone.utc).strftime('%Y-%m'); start,_=funding.month_bounds(last)
    # Only months that have completely closed by cutoff, with funding coverage.
    months=[]
    for _ in range(count):
        prev=datetime.fromtimestamp(start-1,timezone.utc).strftime('%Y-%m'); months.append(prev); start,_=funding.month_bounds(prev)
    records=[]; sources=[]; symbol=funding.SYMBOLS[pair]
    for month in reversed(months):
        a,b=funding.month_bounds(month); rows=[x for x in events if a*1000<=x[0]<b*1000]
        if not rows or rows[0][0]>a*1000+rows[0][1]*3600000+300000 or rows[-1][0]<(b-rows[-1][1]*3600-300)*1000 or any(y[0]-x[0]>max(x[1],y[1])*3600000+300000 for x,y in zip(rows,rows[1:])): raise ValueError('Incomplete funding month '+month)
        spot,src=get_month(cache,symbol,month,'spot',fetcher); perp,src2=get_month(cache,symbol,month,'perp',fetcher); sources.extend((src,src2))
        records.append({'month':month,'normal':window(spot,perp,rows,40,20),'stress':window(spot,perp,rows,80,40)})
    return {'protocol':PROTOCOL,'pair':pair,'months':len(records),'mean_normal':statistics.mean(r['normal']['net_return'] for r in records),'mean_stress':statistics.mean(r['stress']['net_return'] for r in records),'stress_positive_months':sum(r['stress']['net_return']>0 for r in records),'worst_stress_drawdown_proxy':max(r['stress']['drawdown_proxy'] for r in records),'forced_exits':sum(r['stress']['forced_exit_proxy'] for r in records),'monthly':records,'sources':sources,'funding_sha256':dataset['events_sha256'],'development_only':True,'eligible':False,'limitations':'Same-venue archived USDT prices, equal base hedge, 25% per leg, 40/20 or 80/40 bps per leg per side. Monthly fresh accounts; settlement-hour trade-price proxy, conservative margin proxy not actual liquidation. No access/eligibility, borrow, mark-price or live execution proof. No derivatives orders.'}


def spread_screen(quotes,other,now,max_age=15):
    """Cross-venue spot price screen, not arbitrage execution; prefunded inventory required."""
    results=[]
    for pair,k in quotes.items():
        c=other.get(pair)
        if not c or any(not 0<=now-x['observed']<=max_age for x in (k,c)) or abs(k['observed']-c['observed'])>max_age: continue
        for buy,sell,a,b in [('Kraken','Coinbase',k,c),('Coinbase','Kraken',c,k)]:
            gross=b['bid']/a['ask']-1
            results.append({'pair':pair,'buy':buy,'sell':sell,'gross':gross,'normal_net':b['bid']*(1-.004)*(1-.002)/(a['ask']*(1+.004)*(1+.002))-1,'stress_net':b['bid']*(1-.008)*(1-.004)/(a['ask']*(1+.008)*(1+.004))-1,'observed':now,'note':'Top of book only; no depth, transfer, rebalancing, access or executable profit guarantee'})
    return results

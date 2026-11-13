"""Unified, paper-only spot treasury. Research DBs are read-only inputs."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
import math
from pathlib import Path
import sqlite3
import time

from .core import Account, Costs, Strategy, kraken, quote, validate
from .final_eval import read_trial
from .research import identity

PAIRS = ('XBTUSD', 'ETHUSD')
COSTS = Costs()
MAX_DD = .08
MAX_GROSS = .30
MAX_MARKET = .20
MAX_SLEEVE_FUNDING = .25
MAX_MARKET_FUNDING = .40
MIN_RESERVE = .20
MAX_AGE = 90
MAX_SKEW = 30
WEEK = 7*86400


def dump(obj):
    return json.dumps(obj, sort_keys=True, allow_nan=False)


@contextmanager
def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    try:
        yield c
    finally:
        c.close()


def init(path, capital=1000., interval=60):
    if not math.isfinite(capital) or capital<=0 or interval not in (15,60,240,1440):
        raise ValueError('Invalid capital or interval')
    with connect(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS portfolio(id INTEGER PRIMARY KEY CHECK(id=1), initial REAL NOT NULL,
            reserve REAL NOT NULL, interval INTEGER NOT NULL, peak REAL NOT NULL, stopped INTEGER NOT NULL DEFAULT 0,
            last_candle INTEGER NOT NULL DEFAULT 0, last_observed INTEGER NOT NULL DEFAULT 0,
            last_rebalance INTEGER NOT NULL DEFAULT 0, marks TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS sleeves(id TEXT PRIMARY KEY, pair TEXT NOT NULL, artifact TEXT NOT NULL,
            strategy_id TEXT NOT NULL, development_end INTEGER NOT NULL, account TEXT NOT NULL,
            funding REAL NOT NULL, shares REAL NOT NULL, since INTEGER, ticks INTEGER NOT NULL DEFAULT 0,
            UNIQUE(pair,strategy_id));
        CREATE TABLE IF NOT EXISTS portfolio_cycles(id INTEGER PRIMARY KEY, observed INTEGER NOT NULL UNIQUE,
            candle INTEGER NOT NULL, quotes TEXT NOT NULL, equity REAL NOT NULL, gross REAL NOT NULL,
            stopped INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS portfolio_ledger(id INTEGER PRIMARY KEY, observed INTEGER NOT NULL,
            sleeve TEXT NOT NULL REFERENCES sleeves(id), kind TEXT NOT NULL, reason TEXT NOT NULL,
            cash_delta REAL NOT NULL, units_delta REAL NOT NULL, fee REAL NOT NULL,
            price REAL, reserve_delta REAL NOT NULL);
        ''')
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM portfolio').fetchone()
        if r:
            if r['interval']!=interval or r['initial']!=capital: raise ValueError('Portfolio already initialized with different parameters')
        else:
            c.execute('INSERT INTO portfolio(id,initial,reserve,interval,peak) VALUES(1,?,?,?,?)',(capital,capital,interval,capital))
        c.commit()


def _state(c):
    t=c.execute('SELECT * FROM portfolio').fetchone()
    if t is None: raise ValueError('Initialize portfolio first')
    rows=list(c.execute('SELECT * FROM sleeves ORDER BY id'))
    return t, rows, {r['id']:Account(**json.loads(r['account'])) for r in rows}


def _ledger(c, now, name, kind, reason, cash, units=0., fee=0., price=None, reserve=0.):
    c.execute('INSERT INTO portfolio_ledger(observed,sleeve,kind,reason,cash_delta,units_delta,fee,price,reserve_delta) VALUES(?,?,?,?,?,?,?,?,?)',
              (now,name,kind,reason,cash,units,fee,price,reserve))


def register(path, source, agent, round_id, weight_bps, now=None):
    """Freeze a current-protocol eligible research artifact; no money moves outside this DB."""
    now=int(time.time()) if now is None else now
    if Path(path).resolve()==Path(source).resolve(): raise ValueError('Separate research and portfolio DBs required')
    if type(weight_bps) is not int or not 0<weight_bps<=2500: raise ValueError('Weight must be 1..2500 bps')
    leg=read_trial(source,agent,round_id,now)
    name=f"{leg['pair']}:{agent}"
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); t,rows,accounts=_state(c)
        if t['stopped'] or t['last_observed']: raise ValueError('Register sleeves before the first cycle; start a new portfolio to change artifacts')
        if leg['interval']!=t['interval']: raise ValueError('Interval mismatch')
        if leg['pair'] not in PAIRS: raise ValueError('Unsupported market')
        amount=t['initial']*weight_bps/10000
        if t['reserve']-amount<t['initial']*MIN_RESERVE-1e-8: raise ValueError('Minimum reserve')
        if sum(r['funding'] for r in rows if r['pair']==leg['pair'])+amount>t['initial']*MAX_MARKET_FUNDING+1e-8: raise ValueError('Market funding cap')
        c.execute('INSERT INTO sleeves(id,pair,artifact,strategy_id,development_end,account,funding,shares) VALUES(?,?,?,?,?,?,?,?)',
                  (name,leg['pair'],dump(leg),leg['strategy_id'],leg['development_end'],dump(asdict(Account(cash=amount,peak=amount))),amount,amount))
        c.execute('UPDATE portfolio SET reserve=reserve-?',(amount,))
        _ledger(c,now,name,'transfer','initial allocation',amount,reserve=-amount); c.commit()
    return name


def _equity(t,rows,accounts,marks):
    exposures={p:0. for p in PAIRS}; values={}
    for r in rows:
        a=accounts[r['id']]; p=r['pair']; bid=marks.get(p,{}).get('bid',0.)
        if a.units and not bid: raise ValueError('Position has no valuation')
        exposures[p]+=a.units*bid; values[r['id']]=a.equity(bid)
    return t['reserve']+sum(values.values()),exposures,values


def _fill(c, now, r, a, buy, q, reason):
    cash,units,fees,trades=a.cash,a.units,a.fees,a.trades
    a.fill(buy,q['bid'],q['ask'],COSTS)
    if a.trades!=trades:
        d=a.units-units; fee=a.fees-fees; delta=a.cash-cash
        price=(-delta-fee)/d if d>0 else (delta+fee)/(-d)
        _ledger(c,now,r['id'],'fill',reason,delta,d,fee,price)


def cycle(path, snapshots, now=None):
    """Atomic synchronized observation. Each market has (closed bars, bid, ask, observed UTC seconds)."""
    now=int(time.time()) if now is None else now
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); t,rows,accounts=_state(c)
        pairs={r['pair'] for r in rows}; step=t['interval']*60
        if not pairs or set(snapshots)!=pairs: raise ValueError('A complete snapshot of every configured market is required')
        marks={}; histories={}; candles=set(); observed=[]
        for pair,(bars,bid,ask,ts) in snapshots.items():
            bars=validate(bars,step); end=bars[-1].ts+step
            if type(ts) is not int or not 0<=now-ts<=MAX_AGE or not 0<=ts-end<=step+120 or not all(math.isfinite(x) for x in (bid,ask)) or not 0<bid<=ask:
                raise ValueError('Invalid or stale market snapshot')
            candles.add(bars[-1].ts); observed.append(ts); marks[pair]={'bid':bid,'ask':ask,'observed':ts}; histories[pair]=bars
        if len(candles)!=1 or next(iter(candles))%step or max(observed)-min(observed)>MAX_SKEW: raise ValueError('Unsynchronized market observations')
        candle=candles.pop(); stamp=max(observed)
        previous=json.loads(t['marks'])
        if candle<t['last_candle'] or stamp<=t['last_observed'] or any(q['observed']<=previous.get(p,{}).get('observed',0) for p,q in marks.items()):
            return {'status':'duplicate or older observation'}
        new=candle>t['last_candle']; equity,exposure,_=_equity(t,rows,accounts,marks)
        peak=max(t['peak'],equity); stopped=bool(t['stopped']) or equity<=peak*(1-MAX_DD)
        stopped=stopped or sum(exposure.values())>equity*MAX_GROSS+1e-8 or any(v>equity*MAX_MARKET+1e-8 for v in exposure.values())
        # Mark each sleeve even without a new candle; exit before considering entries.
        for r in rows:
            a=accounts[r['id']]; q=marks[r['pair']]; a.mark(q['bid'])
            leg=json.loads(r['artifact']); strategy=Strategy(**leg['strategy'])
            if identity(strategy)!=r['strategy_id']: raise ValueError('Frozen artifact identity mismatch')
            eligible=candle>r['development_end'] and (not strategy.model or strategy.model['fit_end']<candle)
            signal=eligible and strategy.signal([b.close for b in histories[r['pair']]])
            if stopped or a.stopped or (new and not signal):
                _fill(c,stamp,r,a,False,q,'portfolio stop' if stopped else 'sleeve stop' if a.stopped else 'signal exit')
            a.mark(q['bid'])
        equity,exposure,_=_equity(t,rows,accounts,marks)
        stopped=stopped or equity<=peak*(1-MAX_DD)
        # Exposure is actual asset value, not allocated cash. Breaches from price moves latch stop.
        stopped=stopped or sum(exposure.values())>equity*MAX_GROSS+1e-8 or any(v>equity*MAX_MARKET+1e-8 for v in exposure.values())
        if new and not stopped:
            for r in rows:
                a=accounts[r['id']]; q=marks[r['pair']]; s=Strategy(**json.loads(r['artifact'])['strategy'])
                if a.units or a.stopped or candle<=r['development_end'] or (s.model and s.model['fit_end']>=candle) or not s.signal([b.close for b in histories[r['pair']]]): continue
                spend=a.cash*COSTS.fraction
                # Conservative bound includes full cash debit in denominator.
                if sum(exposure.values())+spend>(equity-spend)*MAX_GROSS or exposure[r['pair']]+spend>(equity-spend)*MAX_MARKET: continue
                _fill(c,stamp,r,a,True,q,'signal entry'); a.mark(q['bid'])
                if a.stopped: _fill(c,stamp,r,a,False,q,'sleeve stop')
                equity,exposure,_=_equity(t,rows,accounts,marks)
                if equity<=peak*(1-MAX_DD): stopped=True; break
        if stopped:
            for r in rows:
                a=accounts[r['id']]; _fill(c,stamp,r,a,False,marks[r['pair']],'portfolio stop'); a.mark(marks[r['pair']]['bid'])
        equity,exposure,_=_equity(t,rows,accounts,marks); peak=max(peak,equity)
        for r in rows:
            a=accounts[r['id']]; started=new and candle>r['development_end']
            c.execute('UPDATE sleeves SET account=?,since=COALESCE(since,?),ticks=ticks+? WHERE id=?',
                      (dump(asdict(a)),candle if started else None,int(started),r['id']))
        c.execute('UPDATE portfolio SET peak=?,stopped=?,last_candle=?,last_observed=?,marks=?',(peak,int(stopped),candle,stamp,dump(marks)))
        c.execute('INSERT INTO portfolio_cycles(observed,candle,quotes,equity,gross,stopped) VALUES(?,?,?,?,?,?)',(stamp,candle,dump(marks),equity,sum(exposure.values()),int(stopped)))
        c.commit()
    return {'status':'paper','equity':equity,'gross':sum(exposure.values()),'stopped':stopped,'candle':candle}


def rebalance(path, now=None):
    """Weekly, flat-only capital redistribution using frozen-strategy forward evidence."""
    now=int(time.time()) if now is None else now
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); t,rows,accounts=_state(c)
        if t['stopped'] or not 0<=now-t['last_observed']<=MAX_AGE or now-t['last_rebalance']<WEEK or any(a.units for a in accounts.values()): return {'status':'not eligible: stale, stopped, cooldown or open positions'}
        total=t['reserve']+sum(a.cash for a in accounts.values()); step=t['interval']*60
        evidence=[r for r in rows if r['since'] is not None and t['last_candle']-r['since']>=30*86400 and r['ticks']>=.95*(1+(t['last_candle']-r['since'])/step) and accounts[r['id']].trades>=20]
        def score(r):
            a=accounts[r['id']]; return a.cash/r['shares']-1-a.drawdown
        winners=[r for r in evidence if not accounts[r['id']].stopped and score(r)>0]
        losers=[r for r in evidence if accounts[r['id']].stopped or accounts[r['id']].cash/r['shares']<1]
        reserve=t['reserve']; moves=[]
        def transfer(r,amount):
            nonlocal reserve
            a=accounts[r['id']]; nav=a.cash/r['shares']; old=a.cash
            if nav<=0 or old+amount<=0: raise ValueError('Invalid transfer')
            a.cash+=amount; a.peak*=a.cash/old; reserve-=amount
            c.execute('UPDATE sleeves SET account=?,funding=funding+?,shares=shares+? WHERE id=?',(dump(asdict(a)),amount,amount/nav,r['id']))
            _ledger(c,now,r['id'],'transfer','weekly allocation',amount,reserve=-amount); moves.append((r['id'],amount))
        if losers:
            r=min(losers,key=score); a=accounts[r['id']]; amount=min(total*.025,a.cash*.25)
            if amount>=.01: transfer(r,-amount)
        if winners:
            r=max(winners,key=score); a=accounts[r['id']]; market=sum(accounts[x['id']].cash for x in rows if x['pair']==r['pair'])
            amount=min(total*.025,reserve-total*MIN_RESERVE,total*MAX_SLEEVE_FUNDING-a.cash,total*MAX_MARKET_FUNDING-market)
            if amount>=.01: transfer(r,amount)
        if moves: c.execute('UPDATE portfolio SET reserve=?,last_rebalance=?',(reserve,now)); c.commit()
        return {'status':'allocated' if moves else 'no eligible transfer','moves':moves}


def status(path):
    with connect(path) as c:
        c.execute('BEGIN'); t,rows,accounts=_state(c); marks=json.loads(t['marks']); equity,exposures,values=_equity(t,rows,accounts,marks)
        sleeves=[]; reconciled=True
        for r in rows:
            a=accounts[r['id']]; cash,units,fees=c.execute('SELECT coalesce(sum(cash_delta),0),coalesce(sum(units_delta),0),coalesce(sum(fee),0) FROM portfolio_ledger WHERE sleeve=?',(r['id'],)).fetchone()
            fills,funding=c.execute("SELECT count(CASE WHEN kind='fill' THEN 1 END),coalesce(sum(CASE WHEN kind='transfer' THEN cash_delta ELSE 0 END),0) FROM portfolio_ledger WHERE sleeve=?",(r['id'],)).fetchone()
            ok=abs(cash-a.cash)<1e-6 and abs(units-a.units)<1e-10 and abs(fees-a.fees)<1e-6 and fills==a.trades and abs(funding-r['funding'])<1e-6; reconciled &= ok
            sleeves.append(dict(id=r['id'],equity=values[r['id']],pnl=values[r['id']]-r['funding'],nav_return=values[r['id']]/r['shares']-1,drawdown=a.drawdown,fills=a.trades,stopped=a.stopped,strategy_id=r['strategy_id'],reconciled=ok))
        delta=c.execute('SELECT coalesce(sum(reserve_delta),0) FROM portfolio_ledger').fetchone()[0]; reconciled &= abs(t['initial']+delta-t['reserve'])<1e-6
        return dict(mode='PAPER ONLY',initial=t['initial'],equity=equity,pnl=equity-t['initial'],reserve=t['reserve'],exposures=exposures,drawdown=1-equity/t['peak'],stopped=bool(t['stopped']),last_observed=t['last_observed'],last_candle=t['last_candle'],sleeves=sleeves,reconciled=reconciled)


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/portfolio.sqlite'); sub=p.add_subparsers(dest='cmd',required=True)
    i=sub.add_parser('init'); i.add_argument('--capital',type=float,default=1000); i.add_argument('--interval',type=int,default=60)
    r=sub.add_parser('register'); r.add_argument('--source',required=True); r.add_argument('--agent',required=True); r.add_argument('--round',type=int,required=True); r.add_argument('--weight-bps',type=int,required=True)
    run=sub.add_parser('run'); run.add_argument('--once',action='store_true'); run.add_argument('--poll',type=int,default=60)
    sub.add_parser('status'); sub.add_parser('rebalance'); sub.add_parser('stop'); j=sub.add_parser('journal'); j.add_argument('--limit',type=int,default=30)
    x=p.parse_args(); out=lambda v:print(json.dumps(v,indent=2,allow_nan=False),flush=True)
    if x.cmd=='init': init(x.db,x.capital,x.interval); out(status(x.db)); return
    if x.cmd=='register': out({'registered':register(x.db,x.source,x.agent,x.round,x.weight_bps)}); return
    if x.cmd=='status': out(status(x.db)); return
    if x.cmd=='rebalance': out(rebalance(x.db)); return
    if x.cmd=='journal':
        if not 1<=x.limit<=1000: p.error('limit must be 1..1000')
        with connect(x.db) as c: out([dict(r) for r in c.execute('SELECT * FROM portfolio_ledger ORDER BY id DESC LIMIT ?',(x.limit,))])
        return
    if x.cmd=='stop':
        with connect(x.db) as c: c.execute('UPDATE portfolio SET stopped=1'); c.commit()
        out({'status':'stop latched; keep run active to flatten on next complete fresh snapshot'}); return
    if x.poll<30: p.error('poll must be >=30 seconds')
    with connect(x.db) as c:
        t,rows,_=_state(c); pairs=sorted({r['pair'] for r in rows}); interval=t['interval']
    if not pairs: p.error('register eligible sleeves first')
    while True:
        try:
            histories={pair:kraken(pair,interval) for pair in pairs}; snapshots={}
            for pair in pairs:
                bid,ask=quote(pair); snapshots[pair]=(histories[pair],bid,ask,int(time.time()))
            out(cycle(x.db,snapshots)); out(rebalance(x.db))
        except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error) as e:
            out({'status':'cycle skipped; no fills','error':str(e)})
            if x.once: raise
        if x.once: return
        time.sleep(x.poll)

if __name__=='__main__': main()

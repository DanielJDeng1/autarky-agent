"""Persistent, append-only closed-candle history for the configured market."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import time
from .core import Bar, kraken, load_csv, validate
from . import community as m

LIMIT = 20000


def setup(path):
    with m.db(path) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS market_bars(ts INTEGER PRIMARY KEY, open REAL NOT NULL,
            high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
            source TEXT NOT NULL, first_seen INTEGER NOT NULL)''')
        c.commit()


def ingest(path, bars, source, now=None):
    if source not in ('csv','kraken'):
        raise ValueError('Only actual market history may enter archive')
    cfg = m.config(path); interval = cfg['interval']*60
    bars = validate(bars, interval); now = int(time.time()) if now is None else now
    if bars[-1].ts+interval > now:
        raise ValueError('Archive refuses incomplete/future candles')
    setup(path)
    inserted = 0
    with m.db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        for b in bars:
            row = c.execute('SELECT open,high,low,close,volume FROM market_bars WHERE ts=?',(b.ts,)).fetchone()
            values = (b.open,b.high,b.low,b.close,b.volume)
            if row is not None:
                if tuple(row) != values:
                    raise ValueError(f'Conflicting candle at {b.ts}; verify source, do not silently revise history')
                continue
            c.execute('INSERT INTO market_bars VALUES(?,?,?,?,?,?,?,?)',(b.ts,*values,source,now))
            inserted += 1
        c.commit()
    return {'inserted':inserted,'duplicates':len(bars)-inserted,'total':info(path)['count']}


def latest(path, limit=LIMIT):
    if not 220 <= limit <= LIMIT:
        raise ValueError(f'History limit must be 220..{LIMIT}')
    cfg = m.config(path); setup(path)
    with m.db(path) as c:
        rows = list(c.execute('SELECT ts,open,high,low,close,volume FROM market_bars ORDER BY ts DESC LIMIT ?',(limit,)))
    # A gap is never interpolated or passed into backtesting.
    bars = []
    for r in rows:
        b = Bar(*r)
        if bars and bars[-1].ts-b.ts != cfg['interval']*60:
            break
        bars.append(b)
    return validate(list(reversed(bars)),cfg['interval']*60)


def info(path):
    cfg = m.config(path); setup(path)
    with m.db(path) as c:
        count,first,last = c.execute('SELECT count(*),min(ts),max(ts) FROM market_bars').fetchone()
    try: contiguous = len(latest(path))
    except ValueError: contiguous = 0
    return {'pair':cfg['pair'],'interval':cfg['interval'],'count':count,'first':first,'last':last,
            'latest_contiguous_bars_capped':contiguous,'limit':LIMIT}


def sync(path):
    cfg = m.config(path)
    ingest(path,kraken(cfg['pair'],cfg['interval']),'kraken')
    return latest(path)


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/community.sqlite')
    sub=p.add_subparsers(dest='cmd',required=True)
    imp=sub.add_parser('import'); imp.add_argument('csv')
    sub.add_parser('sync'); sub.add_parser('status')
    x=p.parse_args(); cfg=m.config(x.db)
    if x.cmd=='import': result=ingest(x.db,load_csv(x.csv,cfg['interval']*60),'csv')
    elif x.cmd=='sync': sync(x.db); result=info(x.db)
    else: result=info(x.db)
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()

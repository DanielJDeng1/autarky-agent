"""Explicitly selected development policy, separate experimental forward paper trial.
Never eligible for live trading; no private exchange API or research treasury access.
"""
from __future__ import annotations
import argparse
from contextlib import closing, contextmanager
from dataclasses import asdict
import json
import math
from pathlib import Path
import sqlite3
import time

from .core import Account, Bar, Costs, kraken, quote, validate
from .lab import Policy, Features, PROTOCOL, NORMAL
from .community import encode


@contextmanager
def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path,timeout=30)) as c:
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        yield c


def init(path, source, experiment, name, capital=1000., now=None):
    now=int(time.time()) if now is None else now
    if Path(path).resolve()==Path(source).resolve() or not math.isfinite(capital) or capital<=0: raise ValueError('Separate DB and positive capital required')
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        row=c.execute('SELECT report FROM experiments WHERE id=?',(experiment,)).fetchone()
    if not row: raise ValueError('Unknown development experiment')
    report=json.loads(row[0]); matches=[r for r in report['results'] if r['policy']['name']==name]
    if report['protocol']!=PROTOCOL or not report['development_only'] or len(matches)!=1 or report['last']+3600>now: raise ValueError('Invalid development trial')
    r=matches[0]; p=Policy(**r['policy'])
    if p.identity!=r['identity']: raise ValueError('Policy identity mismatch')
    if any(rule.window>718 for rule in p.entry+p.exit if rule.feature!='forecast'): raise ValueError('Forward REST history supports lookbacks up to 718 hours')
    artifact=r['windows'][-1]['model_artifact'] if p.predictor else None
    if p.predictor and (not artifact or artifact['last_label_end']>=r['windows'][-1]['start']): raise ValueError('Invalid frozen model')
    frozen={'pair':report['pair'],'policy':asdict(p),'identity':p.identity,'dataset_sha256':report['dataset_sha256'],'development_end':report['last'],'artifact':artifact,'experiment':experiment}
    with connect(path) as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables-{'trial','journal'}: raise ValueError('Use a separate forward paper database')
        c.executescript('''CREATE TABLE IF NOT EXISTS trial(id INTEGER PRIMARY KEY CHECK(id=1), frozen TEXT NOT NULL, created INTEGER NOT NULL, initial REAL NOT NULL, account TEXT NOT NULL, last_candle INTEGER NOT NULL DEFAULT 0,last_observed INTEGER NOT NULL DEFAULT 0,last_bid REAL,entry_candle INTEGER,next_entry INTEGER NOT NULL DEFAULT 0,stopped INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY, observed INTEGER NOT NULL,candle INTEGER NOT NULL,reason TEXT NOT NULL,bid REAL NOT NULL,ask REAL NOT NULL,before TEXT NOT NULL,after TEXT NOT NULL);''')
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM trial').fetchone(): raise ValueError('Trial already frozen; do not reset forward record')
        c.execute('INSERT INTO trial(id,frozen,created,initial,account) VALUES(1,?,?,?,?)',(encode(frozen),now,capital,encode(asdict(Account(cash=capital,peak=capital))))); c.commit()
    return status(path)


def decision(bars,p,artifact):
    # Sentinel merely supplies a decision index after the latest closed candle.
    last=bars[-1]; i=len(bars); f=Features(bars+[Bar(last.ts+3600,last.close,last.close,last.close,last.close,0.)])
    def value(r):
        if r.feature!='forecast': return f.series(r.feature,r.window)[i]
        w=artifact['window']; vals=[f.series(k,w)[i] for k in ('momentum','distance','volatility','rsi')]
        if any(v is None for v in vals): return None
        return artifact['intercept']+sum(weight*(v-m)/s for weight,v,m,s in zip(artifact['weights'],vals,artifact['means'],artifact['scales']))
    def passes(r):
        v=value(r); return v is not None and (v>r.threshold if r.op=='gt' else v<r.threshold)
    return all(passes(r) for r in p.entry),any(passes(r) for r in p.exit)


def tick(path,bars,bid,ask,now=None):
    now=int(time.time()) if now is None else now; bars=validate(list(bars),3600); candle=bars[-1].ts
    if not 0<=now-(candle+3600)<=3720 or not all(math.isfinite(v) for v in (bid,ask)) or not 0<bid<=ask: raise ValueError('Stale candle or invalid quote')
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); t=c.execute('SELECT * FROM trial').fetchone()
        if t is None: raise ValueError('Initialize trial')
        if now<=t['last_observed'] or candle<t['last_candle']: return {'status':'duplicate/older observation'}
        frozen=json.loads(t['frozen']); p=Policy(**frozen['policy'])
        if p.identity!=frozen['identity']: raise ValueError('Frozen policy changed')
        a=Account(**json.loads(t['account'])); before=asdict(a); a.mark(bid)
        stopped=bool(t['stopped']) or a.stopped; new=candle>t['last_candle']; entry=t['entry_candle']; next_entry=t['next_entry']; reason='mark'
        if new and candle+3600>t['created'] and candle>frozen['development_end']:
            signal,exit_signal=decision(bars,p,frozen['artifact'])
        else: signal=exit_signal=False
        cost=Costs(NORMAL.fee_bps,NORMAL.slip_bps,p.fraction)
        def exit_position(why):
            nonlocal entry,next_entry,reason
            a.fill(False,bid,ask,cost); entry=None; next_entry=candle+(p.cooldown+1)*3600; reason=why
        if a.units:
            age=(candle-entry)//3600
            if stopped: exit_position('risk/operator stop')
            elif new and (age>=p.max_hold or (age>=p.min_hold and exit_signal)): exit_position('max hold' if age>=p.max_hold else 'signal exit')
        elif new and signal and not stopped and candle>=next_entry:
            a.fill(True,bid,ask,cost); entry=candle; reason='entry'
        a.mark(bid)
        if a.stopped:
            stopped=True
            if a.units: exit_position('post-fill risk stop'); a.mark(bid)
        c.execute('UPDATE trial SET account=?,last_candle=?,last_observed=?,last_bid=?,entry_candle=?,next_entry=?,stopped=?',(encode(asdict(a)),candle,now,bid,entry,next_entry,int(stopped)))
        c.execute('INSERT INTO journal(observed,candle,reason,bid,ask,before,after) VALUES(?,?,?,?,?,?,?)',(now,candle,reason,bid,ask,encode(before),encode(asdict(a)))); c.commit()
    return {'status':'experimental paper','reason':reason,'candle':candle,'equity':a.equity(bid),'stopped':stopped,'fills':a.trades}


def status(path):
    with connect(path) as c:
        r=c.execute('SELECT * FROM trial').fetchone(); a=Account(**json.loads(r['account'])); f=json.loads(r['frozen'])
        return {'mode':'EXPERIMENTAL PAPER ONLY','eligible':False,'frozen':f,'initial':r['initial'],'equity':a.equity(r['last_bid'] or 0),'account':asdict(a),'last_candle':r['last_candle'],'stopped':bool(r['stopped']),'recent':[dict(x) for x in c.execute('SELECT observed,candle,reason,bid,ask FROM journal ORDER BY id DESC LIMIT 20')]}


def stop(path):
    with connect(path) as c,c: c.execute('UPDATE trial SET stopped=1')


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/lab-paper.sqlite'); s=p.add_subparsers(dest='cmd',required=True)
    a=s.add_parser('init'); a.add_argument('--source',required=True); a.add_argument('--experiment',type=int,required=True); a.add_argument('--policy',required=True); a.add_argument('--capital',type=float,default=1000)
    a=s.add_parser('run'); a.add_argument('--poll',type=int,default=60); a.add_argument('--once',action='store_true'); s.add_parser('status'); s.add_parser('stop'); x=p.parse_args()
    def out(v): print(json.dumps(v,indent=2,allow_nan=False),flush=True)
    if x.cmd=='init': out(init(x.db,x.source,x.experiment,x.policy,x.capital)); return
    if x.cmd=='status': out(status(x.db)); return
    if x.cmd=='stop': stop(x.db); out({'stopped':True,'note':'Run paper loop to flatten at next fresh quote'}); return
    if x.poll<30: p.error('poll >=30')
    pair=status(x.db)['frozen']['pair']
    while True:
        try:
            bars=kraken(pair,60); bid,ask=quote(pair); out(tick(x.db,bars,bid,ask))
        except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error) as e:
            out({'status':'cycle skipped','error':str(e)})
            if x.once: raise
        if x.once: break
        time.sleep(x.poll)

if __name__=='__main__': main()

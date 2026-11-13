"""Durable, leased multi-process research jobs. No order or wallet access."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import sqlite3
import time
import uuid

from . import community as m
from .core import Bar, kraken, load_csv, validate

LEASE = 600
MAX_ATTEMPTS = 3


def setup(path):
    with m.db(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, created INTEGER NOT NULL, source TEXT NOT NULL, test_start INTEGER NOT NULL, test_end INTEGER NOT NULL UNIQUE, hold_index INTEGER NOT NULL, bars TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(batch_id INTEGER REFERENCES batches(id), agent TEXT REFERENCES agents(id), state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, token TEXT, lease_until INTEGER, report TEXT, error TEXT, PRIMARY KEY(batch_id,agent));
        ''')


def schedule(path, bars, source, now=None):
    if source not in ('csv','kraken'): raise ValueError('Only real history can be queued')
    cfg=m.config(path); bars=validate(bars,cfg['interval']*60); now=int(time.time()) if now is None else now
    if len(bars)<m.MIN_HISTORY or bars[-1].ts+cfg['interval']*60>now: raise ValueError('Insufficient or incomplete history')
    setup(path)
    with m.db(path) as c:
        c.execute('BEGIN IMMEDIATE'); t=c.execute('SELECT * FROM treasury').fetchone()
        if t['stopped']: return {'status':'stopped'}
        if c.execute("SELECT id FROM batches WHERE state='open'").fetchone(): return {'status':'batch already open'}
        cutoff=next((i for i,b in enumerate(bars) if b.ts>t['last_holdout']),len(bars)); hold=max(int(len(bars)*.8),cutoff)
        if len(bars)-hold<m.MIN_TEST: return {'status':'waiting for fresh data','new_test_bars':len(bars)-hold}
        bid=c.execute('INSERT INTO batches(created,source,test_start,test_end,hold_index,bars,state) VALUES(?,?,?,?,?,?,?)',(now,source,bars[hold].ts,bars[-1].ts,hold,m.encode([asdict(b) for b in bars]),'open')).lastrowid
        for a in m.AGENTS: c.execute("INSERT INTO jobs(batch_id,agent,state) VALUES(?,?,'queued')",(bid,a))
        # Burn this holdout before any worker sees it, even if a worker crashes or fails.
        c.execute('UPDATE treasury SET last_holdout=?',(bars[-1].ts,)); m.event(c,'batch_scheduled',{'batch':bid,'test_end':bars[-1].ts}); c.commit()
    return {'status':'scheduled','batch':bid,'test_start':bars[hold].ts,'test_end':bars[-1].ts}


def claim(path, agent, now=None):
    if agent not in m.AGENTS: raise ValueError('Unknown agent')
    now=int(time.time()) if now is None else now; setup(path)
    with m.db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        # Reap exhausted expired leases before assigning new work.
        c.execute("UPDATE jobs SET state='failed',error='lease expired after retry limit' WHERE state='running' AND lease_until<=? AND attempts>=?",(now,MAX_ATTEMPTS))
        r=c.execute("SELECT j.*,b.bars,b.hold_index FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE b.state='open' AND j.agent=? AND j.attempts<? AND (j.state='queued' OR (j.state='running' AND j.lease_until<=?)) ORDER BY j.batch_id LIMIT 1",(agent,MAX_ATTEMPTS,now)).fetchone()
        if not r: c.commit(); return None
        token=uuid.uuid4().hex
        c.execute("UPDATE jobs SET state='running',attempts=attempts+1,token=?,lease_until=? WHERE batch_id=? AND agent=?",(token,now+LEASE,r['batch_id'],agent)); c.commit()
        return {'batch':r['batch_id'],'agent':agent,'token':token,'bars':json.loads(r['bars']),'hold':r['hold_index']}


def finish(path, job, report=None, error=None, now=None):
    now=int(time.time()) if now is None else now
    with m.db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT j.*,b.state AS batch_state FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE batch_id=? AND agent=?',(job['batch'],job['agent'])).fetchone()
        if not r or r['batch_state']!='open' or r['state']!='running' or r['token']!=job['token'] or r['lease_until']<=now: return False
        state='done' if error is None else 'failed' if r['attempts']>=MAX_ATTEMPTS else 'queued'
        c.execute('UPDATE jobs SET state=?,report=?,error=?,token=NULL,lease_until=NULL WHERE batch_id=? AND agent=?',(state,m.encode(report) if error is None else None,str(error)[:500] if error else None,job['batch'],job['agent'])); c.commit(); return True


def work(path, agent, model=None):
    j=claim(path,agent)
    if not j: return {'status':'idle','agent':agent}
    try:
        bars=[Bar(**b) for b in j['bars']]; hold=j['hold']
        with m.db(path) as c:
            row=c.execute('SELECT report FROM trials WHERE agent=? ORDER BY round_id DESC LIMIT 1',(agent,)).fetchone()
            prior=json.loads(row[0])['candidates'][:3] if row else []
        # The model only sees training summaries, never test data or execution credentials.
        report=m.assess(bars,m.candidates(m.AGENTS[agent],model,bars[:int(hold*.75)],agent,prior),hold)
        ok=finish(path,j,report=report)
        return {'status':'completed' if ok else 'lease lost','agent':agent,'batch':j['batch']}
    except Exception as exc:
        finish(path,j,error=exc)
        return {'status':'worker error','agent':agent,'error':str(exc)}


def finalize(path, now=None):
    now=int(time.time()) if now is None else now; setup(path)
    with m.db(path) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE jobs SET state='failed',error='lease expired after retry limit' WHERE state='running' AND lease_until<=? AND attempts>=?",(now,MAX_ATTEMPTS))
        b=c.execute("SELECT * FROM batches WHERE state='open' ORDER BY id LIMIT 1").fetchone()
        if not b: c.commit(); return {'status':'no open batch'}
        rows=list(c.execute('SELECT * FROM jobs WHERE batch_id=? ORDER BY agent',(b['id'],)))
        if any(r['state']=='failed' for r in rows):
            c.execute("UPDATE batches SET state='failed' WHERE id=?",(b['id'],)); m.event(c,'batch_failed',{'batch':b['id'],'holdout_consumed':True}); c.commit(); return {'status':'failed; holdout consumed','batch':b['id']}
        if any(r['state']!='done' for r in rows): c.commit(); return {'status':'waiting for workers','jobs':{r['agent']:r['state'] for r in rows}}
        rid=c.execute('INSERT INTO rounds(created,source,test_start,test_end,observations) VALUES(?,?,?,?,?)',(now,b['source'],b['test_start'],b['test_end'],len(json.loads(b['bars'])))).lastrowid
        for r in rows:
            report=json.loads(r['report']); a=r['agent']; c.execute('INSERT INTO trials VALUES(?,?,?)',(rid,a,r['report']))
            if report['eligible']:
                agent=c.execute('SELECT active,pending FROM agents WHERE id=?',(a,)).fetchone(); s=m.encode(report['strategy'])
                if s not in (agent['active'],agent['pending']): c.execute('UPDATE agents SET pending=?,pending_after=? WHERE id=?',(s,b['test_end'],a))
        c.execute("UPDATE batches SET state='done' WHERE id=?",(b['id'],)); m.event(c,'batch_finalized',{'batch':b['id'],'round':rid}); c.commit(); return {'status':'finalized','batch':b['id'],'round':rid}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/community.sqlite'); sub=p.add_subparsers(dest='cmd',required=True)
    s=sub.add_parser('schedule'); s.add_argument('--csv')
    w=sub.add_parser('worker'); w.add_argument('--agent',choices=list(m.AGENTS),required=True); w.add_argument('--model'); w.add_argument('--loop',action='store_true'); w.add_argument('--poll',type=int,default=15)
    q=sub.add_parser('coordinate'); q.add_argument('--loop',action='store_true'); q.add_argument('--poll',type=int,default=60)
    sub.add_parser('status')
    x=p.parse_args(); cfg=m.config(x.db); setup(x.db)
    def out(r): print(json.dumps(r,indent=2),flush=True)
    if x.cmd=='status':
        with m.db(x.db) as c: out([dict(r) for r in c.execute('SELECT batch_id,agent,state,attempts,error FROM jobs ORDER BY batch_id DESC,agent LIMIT 60')]); return
    if x.cmd=='schedule': out(schedule(x.db,load_csv(x.csv,cfg['interval']*60) if x.csv else kraken(cfg['pair'],cfg['interval']),'csv' if x.csv else 'kraken')); return
    if x.poll<5: p.error('poll >=5 seconds')
    while True:
        try:
            if x.cmd=='worker': out(work(x.db,x.agent,x.model))
            else:
                out(finalize(x.db))
                if not m.status(x.db)['stopped']: out(schedule(x.db,kraken(cfg['pair'],cfg['interval']),'kraken'))
        except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
            out({'status':'cycle skipped','error':str(exc)})
            if not x.loop: raise
        if not x.loop: break
        time.sleep(x.poll)

if __name__=='__main__': main()

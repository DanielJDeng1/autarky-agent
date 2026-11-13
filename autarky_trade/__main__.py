import argparse, json, sqlite3, time, hashlib, sys
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from .core import *


def dump(obj): return json.dumps(obj,indent=2,allow_nan=False)
def write(path,obj):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True); tmp=p.with_suffix(p.suffix+'.tmp'); tmp.write_text(dump(obj)); tmp.replace(p)

@contextmanager
def connect(path):
    p=Path(path); p.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(p,timeout=30)
    c.execute('CREATE TABLE IF NOT EXISTS accounts (pair TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, last_ts INTEGER NOT NULL, state TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS events (pair TEXT, ts INTEGER, observed_at INTEGER, bid REAL, ask REAL, signal INTEGER, state TEXT, PRIMARY KEY(pair,ts))'); c.commit()
    try:
        with c: yield c
    finally: c.close()

def paper_tick(report,path):
    if not report.get('paper_eligible') or report.get('source')=='synthetic': raise ValueError('No eligible real-data strategy; paper execution refused')
    pair=report['pair']; interval=report['interval']; s=Strategy(**report['selected']); costs=Costs(**report['costs']); strategy_id=hashlib.sha256(json.dumps({'strategy':asdict(s),'costs':asdict(costs),'interval':interval,'holdout_end':report['holdout_end']},sort_keys=True).encode()).hexdigest()
    bars=kraken(pair,interval); ts=bars[-1].ts
    now=int(time.time()); end=ts+interval*60
    if not 0<=now-end<=interval*60+120: raise ValueError('Stale or future candle')
    if ts<=report['holdout_end']: return {'status':'waiting for a new closed candle after holdout'}
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); row=c.execute('SELECT strategy_id,last_ts,state FROM accounts WHERE pair=?',(pair,)).fetchone()
        if row and row[0]!=strategy_id: raise ValueError('Existing paper account uses another strategy; use new database for a new experiment')
        if row and ts<=row[1]: return {'status':'already processed','last_ts':row[1]}
        a=Account(**json.loads(row[2])) if row else Account(); bid,ask=quote(pair); mid=(bid+ask)/2
        # Fill against a fresh public quote at observation time, not a historical open.
        a.mark(bid); signal=s.signal([b.close for b in bars]) and not a.stopped
        a.fill(signal,bid,ask,costs); a.mark(bid)
        state=dump(asdict(a)); c.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?,?)',(pair,strategy_id,ts,state)); c.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',(pair,ts,int(time.time()),bid,ask,int(signal),state)); c.commit()
        return {'status':'paper only','pair':pair,'candle_ts':ts,'bid':bid,'ask':ask,'equity':a.equity(bid),'pnl':a.equity(bid)-1000,'state':asdict(a),'signal':signal}

def main():
    p=argparse.ArgumentParser(description='Autarky local trading laboratory (paper only)'); sub=p.add_subparsers(dest='cmd',required=True)
    r=sub.add_parser('research'); r.add_argument('--source',choices=['synthetic','kraken','csv'],default='synthetic'); r.add_argument('--csv'); r.add_argument('--pair',choices=['XBTUSD','ETHUSD'],default='XBTUSD'); r.add_argument('--interval',type=int,choices=[15,60,240,1440],default=60); r.add_argument('--model'); r.add_argument('--out',default='data/research.json'); r.add_argument('--fee-bps',type=float,default=40); r.add_argument('--slip-bps',type=float,default=10)
    a=sub.add_parser('paper'); a.add_argument('--report',default='data/research.json'); a.add_argument('--db',default='data/paper.sqlite'); a.add_argument('--loop',action='store_true'); a.add_argument('--poll',type=int,default=60)
    b=sub.add_parser('status'); b.add_argument('--db',default='data/paper.sqlite')
    x=p.parse_args()
    if x.cmd=='research':
        if x.source=='csv' and not x.csv: p.error('--csv required')
        if x.source=='synthetic' and x.interval!=60: p.error('synthetic data uses 60 minute candles')
        if not 0<=x.fee_bps<=250 or not 0<=x.slip_bps<=250: p.error('costs must be 0–250 bps for stress test')
        bars=synthetic() if x.source=='synthetic' else kraken(x.pair,x.interval) if x.source=='kraken' else load_csv(x.csv,x.interval*60)
        candidates=list(BASELINES)
        if x.model: candidates.extend(propose(x.model,bars))
        report=evaluate(bars,candidates,Costs(x.fee_bps,x.slip_bps)); report.update(source=x.source,pair=x.pair,interval=x.interval,model=x.model,generated_at=int(time.time()))
        if x.source=='synthetic': report['paper_eligible']=False
        write(x.out,report); print(dump(report))
    elif x.cmd=='paper':
        if x.poll<30: p.error('poll must be >=30 seconds')
        report=json.loads(Path(x.report).read_text())
        if not report.get('paper_eligible') or report.get('source')=='synthetic': p.error('No eligible real-data strategy')
        while True:
            try: print(dump(paper_tick(report,x.db)),flush=True)
            except (OSError,ValueError,RuntimeError) as e:
                if not x.loop: raise
                print(f'Paper cycle skipped: {e}',file=sys.stderr,flush=True)
            if not x.loop: break
            time.sleep(x.poll)
    else:
        with connect(x.db) as c:
            print(dump([{'pair':r[0],'last_ts':r[1],'state':json.loads(r[2])} for r in c.execute('SELECT pair,last_ts,state FROM accounts')]))
if __name__=='__main__': main()

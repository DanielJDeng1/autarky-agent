"""Sequential local development research coordinator; never submits orders."""
import argparse
from contextlib import contextmanager, closing
import json
from pathlib import Path
import sys
import time
from . import lab, research_agent as agent


@contextmanager
def exclusive(path):
    """OS-managed lock releases even after a killed process; never delete lock file."""
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    # Windows byte-range locks may extend beyond EOF. Do not read/write
    # the lock byte before acquiring it: another process may own it.
    with open(path,'a+b') as f:
        f.seek(0)
        if sys.platform=='win32':
            import msvcrt
            try: msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc: raise RuntimeError('Another research coordinator/session is running') from exc
        else:
            import fcntl
            try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc: raise RuntimeError('Another research coordinator/session is running') from exc
        try: yield
        finally:
            f.seek(0)
            if sys.platform=='win32': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f,fcntl.LOCK_UN)


def recover(path):
    """Caller holds exclusive lock; preserve abandoned attempts, never fabricate results."""
    with closing(agent.connect(path)) as c,c:
        for sid,raw in c.execute("SELECT id,report FROM sessions WHERE status='running'").fetchall():
            r=json.loads(raw); r.update(status='interrupted',error='Previous process ended before finalizing session')
            completed=[json.loads(x[0]) for x in c.execute("SELECT report FROM trials WHERE session=? AND status='complete' ORDER BY id",(sid,))]
            r.update(completed_trials=len(completed),trials=[{k:x[k] for k in ('trial_id','change','reason','control','variant','comparison')} for x in completed])
            c.execute("UPDATE sessions SET status='interrupted',report=? WHERE id=?",(json.dumps(r),sid))
        c.execute("UPDATE trials SET status='interrupted' WHERE status='pending'")


def run(path,bars,pair,cutoff,model,budget=3,folds=6,rounds=1,pause=300,emit=None):
    if type(rounds) is not int or rounds<0 or not 1<=budget<=20 or pause<30:
        raise ValueError('Rounds >=0 (0 continuous), budget 1..20, pause >=30 seconds')
    emit=emit or (lambda x: print(json.dumps(x),flush=True))
    with exclusive(str(Path(path).resolve())+'.lock'):
        recover(path)
        n=0
        while rounds==0 or n<rounds:
            n+=1
            for name in agent.SEEDS:
                emit({'event':'start','round':n,'agent':name,'development_only':True})
                try:
                    report=agent.session(path,bars,pair,cutoff,name,model,budget,folds)
                    emit({'event':'complete','round':n,'agent':name,'session_id':report['session_id'],'status':report['status'],
                          'trials':report['completed_trials'],'champion':report.get('champion')})
                except (OSError,ValueError,RuntimeError) as exc:
                    emit({'event':'failed','round':n,'agent':name,'error':str(exc)})
                    # Do not spin or silently switch away from the selected model.
                    if rounds: raise
                    time.sleep(pause)
            if rounds==0 or n<rounds: time.sleep(pause)


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',required=True); p.add_argument('--csv',required=True)
    p.add_argument('--pair',choices=['XBTUSD','ETHUSD'],required=True); p.add_argument('--cutoff',type=int,required=True)
    p.add_argument('--model',default='qwen3:4b'); p.add_argument('--budget',type=int,default=3); p.add_argument('--folds',type=int,default=6)
    p.add_argument('--rounds',type=int,default=1); p.add_argument('--pause',type=int,default=300); x=p.parse_args()
    run(x.db,lab.csv_history(x.csv,x.cutoff),x.pair,x.cutoff,x.model,x.budget,x.folds,x.rounds,x.pause)

if __name__=='__main__': main()

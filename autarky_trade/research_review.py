"""Explicit historical re-evaluation of frozen development policies; no promotion."""
import argparse
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from . import lab


def frozen(path, trial_ids, pair):
    if not trial_ids or len(trial_ids)>15 or len(set(trial_ids))!=len(trial_ids):
        raise ValueError('Select 1..15 distinct completed trial IDs')
    policies={}
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        for tid in trial_ids:
            row=c.execute('SELECT t.status,t.report,s.report FROM trials t JOIN sessions s ON s.id=t.session WHERE t.id=?',(tid,)).fetchone()
            if not row or row[0]!='complete': raise ValueError(f'Trial {tid} not complete')
            report=json.loads(row[1]); session=json.loads(row[2])
            if session['pair']!=pair: raise ValueError('Trial market differs from requested pair')
            for side in ('control','variant'):
                raw=report[side]; p=lab.Policy(**raw['policy'])
                if p.identity!=raw['identity'] or p.fraction!=.25: raise ValueError('Invalid frozen identity or allocation')
                policies.setdefault(p.identity,replace(p,name=f'trial-{tid}-{side}-{p.name}'[:80]))
    return list(policies.values())


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--memory',required=True); p.add_argument('--db',required=True); p.add_argument('--csv',required=True); p.add_argument('--pair',choices=['XBTUSD','ETHUSD'],required=True); p.add_argument('--cutoff',type=int,required=True); p.add_argument('--trials',type=int,nargs='+',required=True); p.add_argument('--folds',type=int,default=6)
    x=p.parse_args()
    if Path(x.memory).resolve()==Path(x.db).resolve(): p.error('Use separate lab database')
    ps=frozen(x.memory,x.trials,x.pair); bars=lab.csv_history(x.csv,x.cutoff)
    result=lab.run(x.db,bars,x.pair,x.cutoff,ps,folds=x.folds)
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__': main()

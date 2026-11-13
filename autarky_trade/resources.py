"""Measured research usage and actual operator-entered expenses, separate from paper P&L."""
from __future__ import annotations
import argparse
import json
import math
from decimal import Decimal, InvalidOperation
import time


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS research_usage(
        token TEXT PRIMARY KEY, batch_id INTEGER NOT NULL, agent TEXT NOT NULL,
        wall_seconds REAL NOT NULL, cpu_seconds REAL NOT NULL, outcome TEXT NOT NULL,
        recorded INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS operating_expenses(
        id INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE, cents INTEGER NOT NULL CHECK(cents>0),
        category TEXT NOT NULL, recorded INTEGER NOT NULL, note TEXT NOT NULL)''')


def record_usage(path, job, wall, cpu, outcome):
    from .community import db
    if not all(math.isfinite(v) and v>=0 for v in (wall,cpu)) or outcome not in ('completed','lease lost','worker error'):
        raise ValueError('Invalid resource measurement')
    with db(path) as c:
        setup(c)
        c.execute('INSERT OR IGNORE INTO research_usage VALUES(?,?,?,?,?,?,?)',
                  (job['token'],job['batch'],job['agent'],wall,cpu,outcome,int(time.time())))
        c.commit()


def expense(path, reference, amount, category, note=''):
    from .community import db
    try: value=Decimal(str(amount))
    except InvalidOperation: raise ValueError('Invalid expense amount') from None
    if not value.is_finite() or not 0<value<=1_000_000 or value!=value.quantize(Decimal('.01')):
        raise ValueError('Expense must be positive USD with at most two decimal places')
    if category not in ('electricity','hardware','data','hosting','other') or not 1<=len(reference)<=128 or len(note)>500:
        raise ValueError('Invalid expense reference/category/note')
    cents=int(value*100)
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE'); setup(c)
        row=c.execute('SELECT cents,category,note FROM operating_expenses WHERE reference=?',(reference,)).fetchone()
        if row and tuple(row)!=(cents,category,note): raise ValueError('Expense reference already used with different details')
        c.execute('INSERT OR IGNORE INTO operating_expenses(reference,cents,category,recorded,note) VALUES(?,?,?,?,?)',
                  (reference,cents,category,int(time.time()),note)); c.commit()


def report(path):
    from .community import db, status
    with db(path) as c:
        setup(c)
        usage=[dict(r) for r in c.execute('SELECT agent,count(*) AS attempts,sum(wall_seconds) AS wall_seconds,sum(cpu_seconds) AS cpu_seconds FROM research_usage GROUP BY agent ORDER BY agent')]
        expenses=[dict(r) for r in c.execute('SELECT category,sum(cents) AS cents FROM operating_expenses GROUP BY category ORDER BY category')]
        c.commit()
    return {'mode':'PAPER ONLY','research_usage':usage,'actual_expenses_by_category':expenses,
            'actual_expenses_usd':sum(r['cents'] for r in expenses)/100,
            'simulated_trading_pnl_usd':status(path)['net_pnl'], 'real_trading_revenue_usd':0,
            'note':'Actual expense amounts are operator-entered. CPU excludes Ollama GPU/server use; wall time includes waiting. Crashes may omit telemetry. Paper P&L is not revenue; no self-sustainability claim.'}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/community.sqlite'); sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('status'); e=sub.add_parser('expense'); e.add_argument('--reference',required=True); e.add_argument('--usd',required=True); e.add_argument('--category',required=True,choices=['electricity','hardware','data','hosting','other']); e.add_argument('--note',default='')
    a=p.parse_args()
    if a.cmd=='expense': expense(a.db,a.reference,a.usd,a.category,a.note)
    print(json.dumps(report(a.db),indent=2))

if __name__=='__main__': main()

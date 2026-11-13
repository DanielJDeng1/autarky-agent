"""Research-slot allocation, based on observed forward evidence with exploration.

This limits the number of jobs, not GPU time, dollars, or exchange exposure.
The coordinator calls it inside its scheduling transaction.
"""
import json
import math

from .core import Account


def choose(c, slots, now):
    """Untried/starved agents first; then qualified forward performance; then age.

    An agent with no forward evidence is not a failure. Rotation guarantees
    continued exploration, even when another agent has a strong current trial.
    Lifetime profits, promising backtests and candidate counts are not rewards.
    """
    rows=list(c.execute('SELECT * FROM agents ORDER BY id'))
    if type(slots) is not int or not 1<=slots<=len(rows):
        raise ValueError('Invalid research slot count')
    t=c.execute('SELECT * FROM treasury').fetchone()
    fresh=bool(t['last_ts'] and t['last_bid'] and 0<=now-(t['last_ts']+t['interval']*60)<=t['interval']*60+120)
    batch=c.execute('SELECT coalesce(max(id),0) FROM batches').fetchone()[0]+1
    selected=[]
    for r in rows:
        last=c.execute('SELECT max(batch_id) FROM jobs WHERE agent=?',(r['id'],)).fetchone()[0]
        age=batch-last if last is not None else batch
        a=Account(**json.loads(r['account']))
        days=(t['last_ts']-r['active_since'])/86400 if r['active_since'] is not None else 0
        nav=a.equity(t['last_bid'] or 0)/r['shares']
        score=None
        if (fresh and r['active'] and not r['pending'] and not a.stopped and days>=30 and r['active_fills']>=20
            and r['active_ticks']>=.95*(1+days*1440/t['interval']) and r['active_start_nav']):
            gain=nav/r['active_start_nav']-1
            if math.isfinite(gain) and gain>0:
                score=gain-a.drawdown
        # A three-round starvation guard makes even a one-slot lab rotate.
        explore=last is None or age>=len(rows)
        selected.append(dict(agent=r['id'], rounds_since_scheduled=age,
                             exploration_due=explore, forward_score=score))
    # Never let a high score starve other hypotheses; among overdue agents serve oldest.
    selected.sort(key=lambda r:(not r['exploration_due'],
        -r['rounds_since_scheduled'] if r['exploration_due'] else -(r['forward_score'] if r['forward_score'] is not None else -math.inf),
        -r['rounds_since_scheduled'],r['agent']))
    return selected[:slots], selected

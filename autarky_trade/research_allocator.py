"""Bounded, evidence-driven research allocation. Never allocates trading capital."""
import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3

PROTOCOL = 'v0.21-research-allocation-1'
HORIZON_PROTOCOL = 'v0.20-horizon-1'
PAIRS = ('XBTUSD', 'ETHUSD')
HORIZONS = (24, 72, 168, 336)
AGENTS = ('alpha', 'beta', 'gamma')


def compact(m):
    """Keep numerical facts, not LLM rationale; returns are fractional."""
    keys = ('net', 'stress', 'fills', 'active_windows', 'windows', 'drawdown', 'rmse', 'vs_mean', 'vs_parent')
    return {k:m[k] for k in keys}


def quality(m):
    """Heuristic research priority, not an expected return or deployment score."""
    stress, delta = m['stress'], m['vs_mean']['mean_stress']
    fills, active, windows = m['fills'], m['active_windows'], m['windows']
    if (not all(math.isfinite(x) for x in (stress, delta)) or type(fills) is not int
        or type(active) is not int or type(windows) is not int or fills < 0 or not 0 <= active <= windows or windows <= 0):
        raise ValueError('Invalid allocation metrics')
    if not fills or not active:
        return -0.02, 'inactive'
    value = stress + .5*delta
    if active < 5 or fills < 10:
        return value-.005, 'sparse'
    return value, 'active'


def seed_arms(report):
    if report.get('protocol') != HORIZON_PROTOCOL or report.get('development_only') is not True or report.get('eligible') is not False:
        raise ValueError('Require a development-only v0.20 horizon review')
    arms = {}
    for r in report['results']:
        h, ident, agent = r['horizon_hours'], r['id'], r['agent']
        if h not in HORIZONS or type(h) is not int or type(ident) is not int or ident < 1 or agent not in AGENTS or set(r['markets']) != set(PAIRS):
            raise ValueError('Invalid review row')
        markets = {p:compact(r['markets'][p]) for p in PAIRS}
        for pair in PAIRS:
            key = f'{ident}:{h}:{pair}'
            if key in arms: raise ValueError('Duplicate review arm')
            quality(markets[pair])
            arms[key] = {'arm':key,'seed_id':ident,'horizon_hours':h,'target_market':pair,'agent':agent,
                         'parent':'node:'+str(ident),'markets':markets,'attempts':0,'failures':0,'spec':r['spec']}
    if not arms: raise ValueError('Review contains no candidates')
    return arms


def allocation_key(report):
    # Canonical seed evidence: independent of JSON whitespace and row ordering.
    arms=seed_arms(report)
    body=json.dumps([PROTOCOL,report['dataset'],report['cutoff'],arms],sort_keys=True,separators=(',',':'),allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def rank(report, history):
    """Rebuild branch priorities from completed and attempted allocator slots."""
    arms = seed_arms(report); key = allocation_key(report); attempts = 0
    for run, node in history:
        a=run.get('allocation',{})
        if a.get('key')!=key or a.get('arm') not in arms: continue
        arm=arms[a['arm']]; attempts+=1; arm['attempts']+=1
        if node is None or node.get('status')!='complete':
            arm['failures']+=1; continue
        markets=node['findings']['markets']; pair=arm['target_market']
        value,activity=quality(markets[pair]); current,old_activity=quality(arm['markets'][pair])
        # A cash-only or sparse descendant cannot displace an active parent.
        better=(activity=='active' and old_activity!='active') or (activity==old_activity and value>current)
        if better:
            arm.update(parent='node:'+str(node['id']),markets={p:compact(markets[p]) for p in PAIRS},spec=node['spec'])
        else: arm['failures']+=1
    for arm in arms.values():
        base,activity=quality(arm['markets'][arm['target_market']]); arm['activity']=activity
        arm['priority']=base-.0015*arm['failures']-.0002*arm['attempts']
    return sorted(arms.values(),key=lambda a:(a['activity']=='active',a['activity']=='sparse',a['priority'],a['arm']),reverse=True), attempts


def choose(report,history):
    arms,attempts=rank(report,history)
    # Every fourth persisted slot deliberately explores the least-tested arm.
    exploration=(attempts+1)%4==0
    if exploration:
        arm=min(arms,key=lambda a:(a['attempts'],-a['priority'],a['arm']))
    else: arm=arms[0]
    return dict(arm,selection='exploration' if exploration else 'exploitation',key=allocation_key(report),slot=attempts+1,
                parent_evidence={'markets':arm['markets'],'classification':'same_horizon_measured'})


def history(path):
    """Read all allocation runs, not the dashboard's bounded history."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        out=[]
        for rid, status, raw in c.execute('SELECT id,status,report FROM tree_runs ORDER BY id'):
            r=json.loads(raw)
            if 'allocation' not in r: continue
            rows=c.execute('SELECT id,status,report FROM tree_nodes WHERE run=? ORDER BY id',(rid,)).fetchall()
            if len(rows)>1: raise ValueError('Allocator run contains more than one experiment')
            node=dict(json.loads(rows[0][2]),id=rows[0][0],status=rows[0][1]) if rows else None
            out.append((dict(r,status=status),node))
        return out


def validate(report,path,parts,cutoff):
    from . import autonomous_research as old, cross_asset as cross, cross_attribution as attribution, lab, research_tree as tree
    seed_arms(report)
    original={p:old.fingerprint(parts[p],p) for p in PAIRS}; aligned=cross.align(parts)
    hashes={p:old.fingerprint(aligned[p],p) for p in PAIRS}
    dataset=lab.digest([cross.PROTOCOL,cutoff,original,hashes])
    if report['dataset']!=dataset or report['datasets']!=hashes or report['cutoff']!=cutoff:
        raise ValueError('Review and historical dataset/cutoff differ')
    ids=sorted({r['id'] for r in report['results']})
    nodes={n['id']:n for n in attribution.load_nodes(path,ids)}
    for r in report['results']:
        n=nodes[r['id']]
        if n['dataset']!=dataset or n['agent']!=r['agent'] or n['spec']!=r['spec'] or n['parent_spec']!=r['parent_spec']:
            raise ValueError('Review and original research node differ')
        if set(r.get('reports',{}))!=set(PAIRS): raise ValueError('Require full saved horizon review, not compact stdout')
        for p in PAIRS:
            saved=r['reports'][p]; other=next(q for q in PAIRS if q!=p)
            if (saved['protocol']!=HORIZON_PROTOCOL or saved['horizon_hours']!=r['horizon_hours'] or
                saved['dataset_sha256']!=hashes[p] or saved['other_dataset_sha256']!=hashes[other] or saved['cutoff']!=cutoff or saved['pair']!=p or
                [w['start'] for w in saved['details']]!=report['window_starts']):
                raise ValueError('Saved review window or fingerprint mismatch')
        facts=tree.facts(r['reports'])['markets']
        if any(compact(facts[p])!=compact(r['markets'][p]) for p in PAIRS):
            raise ValueError('Review metrics do not match detailed report')


def run(path,report,parts,cutoff,model='qwen3:4b',slots=4,emit=None):
    from . import research_tree as tree
    from .coordinator import exclusive
    if type(slots) is not int or not 1<=slots<=30: raise ValueError('Require 1..30 research slots')
    emit=emit or (lambda e:print(json.dumps(e,separators=(',',':'),allow_nan=False),flush=True))
    validate(report,path,parts,cutoff)
    completed=0; records=[]
    # Separate lock serializes allocators; tree.run retains its own writer lock.
    with exclusive(str(Path(path).resolve())+'.allocator.lock'):
        for _ in range(slots):
            a=choose(report,history(path))
            metadata={k:a[k] for k in ('key','arm','slot','seed_id','horizon_hours','target_market','agent','selection','parent','priority','activity','parent_evidence')}
            emit({'event':'allocate',**metadata})
            r=tree.run(path,parts,cutoff,model=model,rounds=1,budget=1,emit=emit,cross_asset=True,horizon=a['horizon_hours'],
                       agents=(a['agent'],),parent_name=a['parent'],allocation=metadata)
            completed+=r['completed']; records.append({'run_id':r['run_id'],'completed':r['completed'],**metadata})
    result={'protocol':PROTOCOL,'slots':slots,'completed':completed,'allocations':records,'development_only':True,'eligible':False}
    emit({'event':'allocation_finished',**result}); return result


def main():
    from . import multiyear
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',required=True); p.add_argument('--review',required=True)
    sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('plan'); a=sub.add_parser('run'); a.add_argument('--btc',required=True); a.add_argument('--eth',required=True); a.add_argument('--cutoff',type=int,required=True)
    a.add_argument('--model',default='qwen3:4b'); a.add_argument('--deterministic',action='store_true'); a.add_argument('--slots',type=int,default=4)
    x=p.parse_args(); report=json.loads(Path(x.review).read_text(encoding='utf-8-sig'))
    if x.cmd=='plan':
        h=history(x.db); arms,n=rank(report,h); print(json.dumps({'protocol':PROTOCOL,'attempts':n,'next':choose(report,h),'arms':arms,'development_only':True,'eligible':False},indent=2,allow_nan=False))
    else:
        run(x.db,report,{'XBTUSD':multiyear.segments(x.btc,x.cutoff),'ETHUSD':multiyear.segments(x.eth,x.cutoff)},x.cutoff,None if x.deterministic else x.model,x.slots)


if __name__=='__main__': main()

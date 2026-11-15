"""Finite simulated economy for research agents. No trading or real spending authority."""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

PROTOCOL = 'v0.24-economic-kernel-1'
SLOTS = ('alpha', 'beta', 'gamma')
DEFAULT = {'initial_cents':100000, 'productive_cents':20000, 'operating_cents':5000,
           'research_cents':100, 'upkeep_cents':25, 'grace_ticks':2,
           'conserve_failures':2, 'cooldown_ticks':2}


def pack(x):
    return json.dumps(x, sort_keys=True, separators=(',', ':'), allow_nan=False)


def connect(path, readonly=False):
    p=Path(path).resolve()
    if readonly:
        c=sqlite3.connect(p.as_uri()+'?mode=ro', uri=True, timeout=30)
    else:
        if not p.exists(): raise ValueError('Initialize the separate economic ledger first')
        c=sqlite3.connect(p, timeout=30)
    c.row_factory=sqlite3.Row
    try:
        if c.execute('SELECT protocol FROM economy_config').fetchone()[0]!=PROTOCOL:
            raise ValueError('Incompatible economic ledger')
    except BaseException:
        c.close(); raise
    return c


def init(path, config=None):
    from .coordinator import exclusive
    p=Path(path).resolve(); p.parent.mkdir(parents=True,exist_ok=True)
    cfg=dict(DEFAULT if config is None else config)
    if set(cfg)!=set(DEFAULT) or any(type(v) is not int or v<=0 for v in cfg.values()):
        raise ValueError('Require positive integer economic settings')
    if cfg['initial_cents']<3*(cfg['productive_cents']+cfg['operating_cents']) or cfg['operating_cents']<max(cfg['upkeep_cents'],cfg['research_cents']):
        raise ValueError('Insufficient initial funding')
    with exclusive(str(p)+'.lock'):
        if p.exists(): raise ValueError('Economic ledger already exists; never reset it')
        with closing(sqlite3.connect(p)) as c,c:
            c.row_factory=sqlite3.Row
            c.executescript('''CREATE TABLE economy_config(protocol TEXT NOT NULL, config TEXT NOT NULL);
                CREATE TABLE economy_accounts(name TEXT PRIMARY KEY, cents INTEGER NOT NULL CHECK(cents>=0));
                CREATE TABLE economy_agents(id TEXT PRIMARY KEY, slot TEXT NOT NULL, generation INTEGER NOT NULL,
                    predecessor TEXT, policy TEXT NOT NULL, state TEXT NOT NULL, born INTEGER NOT NULL,
                    retired INTEGER, missed INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                    cooldown_until INTEGER NOT NULL DEFAULT 0, UNIQUE(slot,generation));
                CREATE TABLE economy_entries(id INTEGER PRIMARY KEY, tick INTEGER NOT NULL, source TEXT NOT NULL,
                    destination TEXT NOT NULL, cents INTEGER NOT NULL CHECK(cents>0), kind TEXT NOT NULL, reference TEXT NOT NULL UNIQUE);
                CREATE TABLE economy_ticks(id INTEGER PRIMARY KEY, status TEXT NOT NULL, decision TEXT);
                CREATE TABLE economy_tasks(id TEXT PRIMARY KEY, tick INTEGER NOT NULL UNIQUE, agent TEXT NOT NULL,
                    state TEXT NOT NULL, fee INTEGER NOT NULL, report TEXT NOT NULL);
            ''')
            c.execute('INSERT INTO economy_config VALUES(?,?)',(PROTOCOL,pack(cfg)))
            for name,cents in [('reserve',cfg['initial_cents']),('spent',0)]:
                c.execute('INSERT INTO economy_accounts VALUES(?,?)',(name,cents))
            c.execute('INSERT INTO economy_entries(tick,source,destination,cents,kind,reference) VALUES(0,?,?,?,?,?)',
                      ('external_simulated_seed','reserve',cfg['initial_cents'],'seed','seed'))
            for slot in SLOTS: birth(c,cfg,slot,0)
    return status(p)


def balance(c,name):
    return c.execute('SELECT cents FROM economy_accounts WHERE name=?',(name,)).fetchone()[0]


def transfer(c,tick,source,dest,cents,kind,reference):
    if type(cents) is not int or cents<=0 or source==dest: raise ValueError('Invalid transfer')
    if balance(c,source)<cents: raise ValueError('Insufficient simulated funds')
    c.execute('UPDATE economy_accounts SET cents=cents-? WHERE name=?',(cents,source))
    if c.execute('UPDATE economy_accounts SET cents=cents+? WHERE name=?',(cents,dest)).rowcount!=1:
        raise ValueError('Unknown destination')
    c.execute('INSERT INTO economy_entries(tick,source,destination,cents,kind,reference) VALUES(?,?,?,?,?,?)',
              (tick,source,dest,cents,kind,reference))


def birth(c,cfg,slot,tick):
    old=c.execute('SELECT * FROM economy_agents WHERE slot=? ORDER BY generation DESC LIMIT 1',(slot,)).fetchone()
    if old and old['state']!='retired': raise ValueError('Slot still occupied')
    total=cfg['productive_cents']+cfg['operating_cents']
    if balance(c,'reserve')<total: return None
    generation=old['generation']+1 if old else 1
    ident=f'{slot}:{generation}'
    # New generations alternate between LLM and cheap deterministic selection.
    policy='ollama' if generation%2 else 'deterministic'
    c.execute('INSERT INTO economy_agents(id,slot,generation,predecessor,policy,state,born) VALUES(?,?,?,?,?,?,?)',
              (ident,slot,generation,old['id'] if old else None,policy,'active',tick))
    for bucket in ('productive','operating'):
        name=ident+':'+bucket; c.execute('INSERT INTO economy_accounts VALUES(?,0)',(name,))
        transfer(c,tick,'reserve',name,cfg[bucket+'_cents'],'birth',ident+':birth:'+bucket)
    return ident


def recover(c):
    cfg=json.loads(c.execute('SELECT config FROM economy_config').fetchone()[0])
    # Fees were prepaid. Unknown execution is not a refund or a new experiment.
    for r in c.execute("SELECT id,report,agent,tick FROM economy_tasks WHERE state='running'").fetchall():
        report=json.loads(r['report']); report['interrupted_reason']='Process ended before settlement; prepaid fee retained'
        c.execute("UPDATE economy_tasks SET state='interrupted',report=? WHERE id=?",(pack(report),r['id']))
        record_failure(c,cfg,r['agent'],r['tick'],False)
    c.execute("UPDATE economy_ticks SET status='interrupted' WHERE status='running'")


def table_exists(c,name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def production_locked(c,ident):
    return table_exists(c,'economy_production') and c.execute("SELECT 1 FROM economy_production WHERE agent=? AND state='locked'",(ident,)).fetchone() is not None


def upkeep(c,cfg,tick):
    for a in c.execute("SELECT * FROM economy_agents WHERE state!='retired' ORDER BY slot").fetchall():
        ident=a['id']; op=ident+':operating'
        if balance(c,op)>=cfg['upkeep_cents']:
            transfer(c,tick,op,'spent',cfg['upkeep_cents'],'upkeep',f'{ident}:upkeep:{tick}')
            c.execute('UPDATE economy_agents SET missed=0 WHERE id=?',(ident,))
            state='conserving' if tick<a['cooldown_until'] or balance(c,op)<cfg['research_cents'] else 'active'
            c.execute('UPDATE economy_agents SET state=? WHERE id=?',(state,ident))
        else:
            missed=a['missed']+1
            if missed<cfg['grace_ticks']:
                c.execute("UPDATE economy_agents SET state='conserving',missed=? WHERE id=?",(missed,ident))
                continue
            # A production reservation must be flattened before funds can be returned.
            if production_locked(c,ident):
                c.execute("UPDATE economy_agents SET state='liquidating',missed=? WHERE id=?",(missed,ident))
                continue
            for bucket in ('productive','operating'):
                name=ident+':'+bucket; amount=balance(c,name)
                if amount: transfer(c,tick,name,'reserve',amount,'retirement',ident+':retire:'+bucket)
            c.execute("UPDATE economy_agents SET state='retired',retired=?,missed=? WHERE id=?",(tick,missed,ident))
    # One generation per slot per tick, funded only from remaining reserve.
    for slot in SLOTS:
        if not c.execute("SELECT 1 FROM economy_agents WHERE slot=? AND state!='retired'",(slot,)).fetchone():
            birth(c,cfg,slot,tick)


def select(report, history, agents):
    from . import research_allocator as allocator
    arms,n=allocator.rank(report,history)
    allowed={a['slot']:a for a in agents if a['state']=='active'}
    arms=[a for a in arms if a['agent'] in allowed]
    if not arms: return None
    exploration=(n+1)%4==0
    arm=min(arms,key=lambda a:(a['attempts'],-a['priority'],a['arm'])) if exploration else arms[0]
    return dict(arm,selection='exploration' if exploration else 'exploitation',key=allocator.allocation_key(report),slot=n+1,
                parent_evidence={'markets':arm['markets'],'classification':'same_horizon_measured'}),allowed[arm['agent']]


def record_failure(c,cfg,ident,tick,improved):
    a=c.execute('SELECT failures FROM economy_agents WHERE id=?',(ident,)).fetchone()
    count=0 if improved else a['failures']+1
    cooldown=tick+cfg['cooldown_ticks']+1 if count>=cfg['conserve_failures'] else 0
    state='conserving' if cooldown or balance(c,ident+':operating')<cfg['research_cents'] else 'active'
    c.execute('UPDATE economy_agents SET failures=?,cooldown_until=?,state=? WHERE id=?',
              (0 if cooldown else count,cooldown,state,ident))


def execute_tick(c,cfg,report,history,worker,emit,model,watts,price):
    with c:
        tick=c.execute("INSERT INTO economy_ticks(status) VALUES('running')").lastrowid
        upkeep(c,cfg,tick)
        agents=[dict(a) for a in c.execute("SELECT * FROM economy_agents WHERE state='active'")]
        choice=select(report,history(),agents)
        if choice is None:
            c.execute("UPDATE economy_ticks SET status='complete',decision='conserve' WHERE id=?",(tick,))
            return {'tick':tick,'decision':'conserve','completed':0}
        arm,a=choice; ident=a['id']; task=uuid.uuid4().hex
        transfer(c,tick,ident+':operating','spent',cfg['research_cents'],'research',task)
        meta={k:arm[k] for k in ('key','arm','slot','seed_id','horizon_hours','target_market','agent','selection','parent','priority','activity','parent_evidence')}
        meta.update(economic_agent=ident,economic_task=task,economic_policy=a['policy'])
        payload={'allocation':meta,'policy':a['policy'],'model':model if a['policy']=='ollama' else None}
        c.execute('INSERT INTO economy_tasks VALUES(?,?,?,?,?,?)',(task,tick,ident,'running',cfg['research_cents'],pack(payload)))
        c.execute("UPDATE economy_ticks SET decision='research' WHERE id=?",(tick,))
    start=time.monotonic(); cpu=time.process_time()
    result=None; failure=None; improved=False
    try:
        emit({'event':'economic_research','tick':tick,'agent_id':ident,'prepaid_simulated_cents':cfg['research_cents'],'allocation':meta})
        result=worker(meta,payload['model'])
        # Retrieve the exact recorded allocation, not a hypothetical backtest profit.
        from . import research_allocator as allocator
        for run,node in reversed(history()):
            if run.get('allocation',{}).get('economic_task')!=task: continue
            if node and node['status']=='complete':
                market=meta['target_market']; value,activity=allocator.quality(node['findings']['markets'][market]); old,old_activity=allocator.quality(arm['markets'][market])
                improved=(activity=='active' and old_activity!='active') or (activity==old_activity and value>old)
            break
    except BaseException as exc:
        failure=exc
    wall=time.monotonic()-start; cpu=time.process_time()-cpu
    payload.update(result=result,improved_research=improved,wall_seconds=wall,python_cpu_seconds=cpu,
                   estimated_energy_usd=wall*watts*price/3600000 if watts is not None else None,
                   estimate_assumptions={'system_watts':watts,'usd_per_kwh':price} if watts is not None else None)
    if failure: payload['error']=str(failure)
    state='interrupted' if isinstance(failure,KeyboardInterrupt) else 'failed' if failure else 'complete'
    with c:
        c.execute('UPDATE economy_tasks SET state=?,report=? WHERE id=?',(state,pack(payload),task))
        c.execute('UPDATE economy_ticks SET status=? WHERE id=?',(state,tick))
        record_failure(c,cfg,ident,tick,improved)
    if failure: raise failure
    return {'tick':tick,'decision':'research','agent_id':ident,'completed':result['completed'],'improved_research':improved,'wall_seconds':wall}


def run(path,tree_path,report,parts,cutoff,model='qwen3:4b',ticks=4,watts=None,price=None,emit=None):
    from . import research_allocator as allocator, research_tree as tree
    from .coordinator import exclusive
    if type(ticks) is not int or not 1<=ticks<=30: raise ValueError('Ticks must be 1..30')
    if (watts is None)!=(price is None) or any(not math.isfinite(v) or v<0 for v in (watts,price) if v is not None):
        raise ValueError('Provide both finite nonnegative watts and electricity price, or neither')
    if Path(path).resolve()==Path(tree_path).resolve(): raise ValueError('Separate economy and research ledgers required')
    emit=emit or (lambda x:print(pack(x),flush=True))
    allocator.validate(report,tree_path,parts,cutoff)
    with exclusive(str(Path(path).resolve())+'.lock'),exclusive(str(Path(tree_path).resolve())+'.allocator.lock'),closing(connect(path)) as c:
        cfg=json.loads(c.execute('SELECT config FROM economy_config').fetchone()[0])
        if table_exists(c,'economy_clock'): raise ValueError('Use the continuous runtime for this elapsed-time economy')
        with c: recover(c)
        def worker(meta,chosen_model):
            return tree.run(tree_path,parts,cutoff,model=chosen_model,rounds=1,budget=1,emit=emit,cross_asset=True,
                            horizon=meta['horizon_hours'],agents=(meta['agent'],),parent_name=meta['parent'],allocation=meta)
        results=[]
        for _ in range(ticks):
            r=execute_tick(c,cfg,report,lambda:allocator.history(tree_path),worker,emit,model,watts,price)
            results.append(r); emit({'event':'economic_tick',**r})
    return {'protocol':PROTOCOL,'ticks':results,'economy':status(path)}


def status(path):
    with closing(connect(path,True)) as c:
        c.execute('BEGIN')
        cfg=json.loads(c.execute('SELECT config FROM economy_config').fetchone()[0]); accounts={r[0]:r[1] for r in c.execute('SELECT * FROM economy_accounts')}
        # Reconstruct every balance from the append-only transfer journal.
        rebuilt={k:0 for k in accounts}
        market_net=0
        for r in c.execute('SELECT source,destination,cents,kind FROM economy_entries ORDER BY id'):
            if r[0]=='external_simulated_market' and r[3]=='paper_gain': market_net+=r[2]
            elif r[0]!='external_simulated_seed': rebuilt[r[0]]-=r[2]
            if r[1]=='external_simulated_market' and r[3]=='paper_loss': market_net-=r[2]
            else: rebuilt[r[1]]+=r[2]
        if rebuilt!=accounts or sum(accounts.values())!=cfg['initial_cents']+market_net: raise ValueError('Economic ledger does not reconcile')
        agents=[dict(a,operating_cents=accounts[a['id']+':operating'],productive_cents=accounts[a['id']+':productive']) for a in c.execute('SELECT * FROM economy_agents ORDER BY slot,generation')]
        tasks=[dict(r,report=json.loads(r['report'])) for r in c.execute('SELECT * FROM economy_tasks ORDER BY tick DESC LIMIT 30')]
        tick=c.execute('SELECT coalesce(max(id),0) FROM economy_ticks').fetchone()[0]
    return {'protocol':PROTOCOL,'mode':'SIMULATED ECONOMY','tick':tick,'config':cfg,'reserve_cents':accounts['reserve'],
            'spent_cents':accounts['spent'],'remaining_cents':cfg['initial_cents']+market_net-accounts['spent'],'paper_net_cents':market_net,
            'reconciled':True,'agents':agents,'recent_tasks':tasks,'real_revenue_usd':0,'self_sustaining':False,
            'note':'All capital, market settlements and charges are simulated. Runtime/energy estimates are not actual bills; real revenue remains zero.'}


def actual_expenses(path):
    """Read the existing resource ledger, without creating or mutating it."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operating_expenses'").fetchone()
        if not exists: return {'recorded':False,'actual_expenses_usd':None,'note':'No expense table in supplied resource ledger'}
        rows=[{'category':r[0],'cents':r[1]} for r in c.execute('SELECT category,sum(cents) FROM operating_expenses GROUP BY category')]
    return {'recorded':bool(rows),'actual_expenses_usd':sum(r['cents'] for r in rows)/100 if rows else None,
            'categories':rows,'note':'Operator-entered expenses only; absence of records is not zero cost. No real revenue attributed.'}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',required=True); sub=p.add_subparsers(dest='cmd',required=True)
    i=sub.add_parser('init'); i.add_argument('--config'); st=sub.add_parser('status'); st.add_argument('--expenses-db'); r=sub.add_parser('run')
    for name in ('tree','review','btc','eth'): r.add_argument('--'+name,required=True)
    r.add_argument('--cutoff',required=True,type=int); r.add_argument('--model',default='qwen3:4b'); r.add_argument('--ticks',type=int,default=4)
    r.add_argument('--watts',type=float); r.add_argument('--usd-per-kwh',type=float)
    x=p.parse_args()
    if x.cmd=='init': result=init(x.db,json.loads(Path(x.config).read_text(encoding='utf-8-sig')) if x.config else None)
    elif x.cmd=='status':
        result=status(x.db)
        if x.expenses_db: result['actual_economy']=actual_expenses(x.expenses_db)
    else:
        from . import multiyear
        result=run(x.db,x.tree,json.loads(Path(x.review).read_text(encoding='utf-8-sig')),
                   {'XBTUSD':multiyear.segments(x.btc,x.cutoff),'ETHUSD':multiyear.segments(x.eth,x.cutoff)},
                   x.cutoff,x.model,x.ticks,x.watts,x.usd_per_kwh)
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__': main()

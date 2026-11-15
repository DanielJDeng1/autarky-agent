"""Autarky v0.27: continuous local trading research, collaboration and paper operations.
No live orders, private keys, or real-money claims. One foreground command, durable ledgers.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import council, economic_kernel as economy, core
from .coordinator import exclusive

PROTOCOL='v0.28-runtime-1'
DAY=86400


def pack(x): return council.pack(x)


def ensure(path):
    with closing(council.connect(path,True)) as c,c:
        c.execute('CREATE TABLE IF NOT EXISTS runtime_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS runtime_candidates(key TEXT PRIMARY KEY,agent TEXT NOT NULL,plan TEXT NOT NULL,report TEXT NOT NULL,created INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS runtime_events(id INTEGER PRIMARY KEY,created INTEGER NOT NULL,kind TEXT NOT NULL,agent TEXT,body TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS runtime_equity_history(observed INTEGER PRIMARY KEY,settled INTEGER NOT NULL,marked INTEGER NOT NULL,reserve INTEGER NOT NULL,paper_net INTEGER NOT NULL)')


def set_state(path,key,value):
    with closing(council.connect(path)) as c,c:
        c.execute('INSERT INTO runtime_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,pack(value)))


def get_state(path):
    with closing(council.connect(path)) as c:
        return {r[0]:json.loads(r[1]) for r in c.execute('SELECT key,value FROM runtime_state')}


def event(path,kind,body,agent=None):
    with closing(council.connect(path)) as c,c:
        c.execute('INSERT INTO runtime_events(created,kind,agent,body) VALUES(?,?,?,?)',(int(time.time()),kind,agent,pack(body)))


def clock_setup(c,now):
    c.execute('CREATE TABLE IF NOT EXISTS economy_clock(id INTEGER PRIMARY KEY CHECK(id=1),last_day INTEGER NOT NULL)')
    c.execute('INSERT OR IGNORE INTO economy_clock VALUES(1,?)',(now//DAY,))
    c.execute('''CREATE TABLE IF NOT EXISTS economy_production(trial TEXT PRIMARY KEY,agent TEXT NOT NULL,
        initial_cents INTEGER NOT NULL,settled_cents INTEGER NOT NULL,state TEXT NOT NULL, last_cash_cents INTEGER NOT NULL)''')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_locked_trial ON economy_production(agent) WHERE state='locked'")


def reconcile_production(c,trials):
    """Only canonical candidate_normal. Settle closed-account cash once; reserve until terminal.
    Market gains/losses are external *simulated* cash flows, not operating expenses or real revenue.
    """
    changes=[]
    for t in trials:
        ident=t['agent']; trial=t['id']; initial=round(t['initial_usd']*100)
        a=c.execute('SELECT * FROM economy_agents WHERE id=?',(ident,)).fetchone()
        r=c.execute('SELECT * FROM economy_production WHERE trial=?',(trial,)).fetchone()
        if not r:
            if not a or a['state']=='retired': continue  # old retired shadow trials cannot be retroactively funded
            if economy.balance(c,ident+':productive')!=initial: raise ValueError('Production capital differs from treasury: '+ident)
            c.execute("INSERT INTO economy_production VALUES(?,?,?,?,'locked',?)",(trial,ident,initial,initial,initial))
            r=c.execute('SELECT * FROM economy_production WHERE trial=?',(trial,)).fetchone()
        if r['state']!='locked': continue
        account=t['accounts']['candidate_normal']; cash=round(account['cash']*100)
        terminal=t['state'] in ('complete','stopped')
        if terminal and any(x['units'] for x in t['accounts'].values()): raise ValueError('Terminal trial has open position')
        if account['units']: continue
        delta=cash-r['settled_cents']; tick=c.execute('SELECT coalesce(max(id),0) FROM economy_ticks').fetchone()[0]
        if delta:
            name=ident+':productive'; ref=f'paper:{trial}:{account["trades"]}:{cash}'
            if economy.balance(c,name)+delta<0: raise ValueError('Paper settlement exceeds reserved funds')
            c.execute('UPDATE economy_accounts SET cents=cents+? WHERE name=?',(delta,name))
            source,dest,kind=('external_simulated_market',name,'paper_gain') if delta>0 else (name,'external_simulated_market','paper_loss')
            c.execute('INSERT INTO economy_entries(tick,source,destination,cents,kind,reference) VALUES(?,?,?,?,?,?)',(tick,source,dest,abs(delta),kind,ref))
            c.execute('UPDATE economy_production SET settled_cents=?,last_cash_cents=? WHERE trial=?',(cash,cash,trial))
            changes.append({'agent':ident,'trial':trial,'delta_cents':delta,'paper_only':True})
        if terminal:
            # Do not withdraw cash during a frozen trial; final surplus may fund operating costs.
            profit=max(0,cash-initial); distribution=profit//2
            if distribution: economy.transfer(c,tick,ident+':productive',ident+':operating',distribution,'paper_profit_distribution','distribution:'+trial)
            c.execute("UPDATE economy_production SET state='released' WHERE trial=?",(trial,))
            changes.append({'agent':ident,'trial':trial,'released':True,'operating_distribution_cents':distribution,'paper_only':True})
    return changes


def advance_economy(path,trials,now=None,recover_interrupted=True):
    now=int(time.time()) if now is None else now
    with exclusive(str(Path(path).resolve())+'.lock'),closing(economy.connect(path)) as c,c:
        clock_setup(c,now); cfg=json.loads(c.execute('SELECT config FROM economy_config').fetchone()[0])
        if recover_interrupted: economy.recover(c)
        changes=reconcile_production(c,trials)
        last=c.execute('SELECT last_day FROM economy_clock').fetchone()[0]
        # UTC day boundaries; catch up at most 30 per pass, never charge twice.
        for day in range(last+1,min(now//DAY,last+30)+1):
            tick=c.execute("INSERT INTO economy_ticks(status,decision) VALUES('complete','daily_upkeep')").lastrowid
            economy.upkeep(c,cfg,tick); c.execute('UPDATE economy_clock SET last_day=? WHERE id=1',(day,))
        # Cooldown uses economy ticks, but paid research no longer creates upkeep charges.
        tick=c.execute('SELECT coalesce(max(id),0) FROM economy_ticks').fetchone()[0]
        for a in c.execute("SELECT * FROM economy_agents WHERE state='conserving' AND missed=0 AND cooldown_until<=?",(tick,)).fetchall():
            if economy.balance(c,a['id']+':operating')>=cfg['research_cents']: c.execute("UPDATE economy_agents SET state='active' WHERE id=?",(a['id'],))
        return changes


def capital_history(path):
    with closing(economy.connect(path,True)) as c:
        cfg=json.loads(c.execute('SELECT config FROM economy_config').fetchone()[0]); cash=0; spent=0; market=0; reserve=0; history=[]
        for r in c.execute('SELECT id,tick,source,destination,cents,kind FROM economy_entries ORDER BY id'):
            n=r['cents']; src=r['source']; dst=r['destination']; kind=r['kind']
            if kind=='seed': cash+=n
            if kind=='paper_gain': cash+=n; market+=n
            if kind=='paper_loss': cash-=n; market-=n
            if dst=='spent': cash-=n; spent+=n
            if src=='reserve': reserve-=n
            if dst=='reserve': reserve+=n
            point={'entry':r['id'],'tick':r['tick'],'remaining_cents':cash,'reserve_cents':reserve,'spent_cents':spent,'paper_net_cents':market}
            if history and history[-1]['tick']==r['tick']: history[-1]=point
            else: history.append(point)
        if len(history)>800: history=[history[i] for i in range(0,len(history),math.ceil(len(history)/799))]+[history[-1]]
        return history


def equity_point(paths,now=None):
    now=int(time.time()) if now is None else now
    ep,pp,cp=paths['economy'],paths['production'],paths['council']
    e=economy.status(ep,include_locked=True); trials=production_status(pp)['trials']; marked=e['remaining_cents']
    for l in e['locked_production']:
        t=next((x for x in trials if x['id']==l['trial']),None)
        if not t: raise ValueError('Reserved trial missing from production history')
        marked+=round(t['accounts']['candidate_normal']['equity']*100)-l['settled_cents']
    with closing(council.connect(cp)) as c,c:
        # One observation per UTC minute; no synthetic interpolation or backfilled marks.
        c.execute('INSERT OR REPLACE INTO runtime_equity_history VALUES(?,?,?,?,?)',(now//60*60,e['remaining_cents'],marked,e['reserve_cents'],e.get('paper_net_cents',0)))


def equity_history(path,limit=1000):
    with closing(council.connect(path)) as c:
        return [dict(r) for r in reversed(c.execute('SELECT * FROM runtime_equity_history ORDER BY observed DESC LIMIT ?',(limit,)).fetchall())]


def operator_queue(path,agents):
    """Delivery receipt: a valid talk addressed to operator advances the new runtime cursor.
    Older turns may have consumed messages without responding; label as processed, not answered.
    """
    with closing(council.connect(path)) as c:
        output=[]
        for a in agents:
            if a['state']=='retired': continue
            n=c.execute('SELECT coalesce(max(inbox_through),0) FROM council_turns WHERE agent=?',(a['id'],)).fetchone()[0]
            rows=council.unread_messages(c,a['id'],'operator',100000)
            output.append({'agent':a['id'],'pending':len(rows),'oldest':rows[0]['created'] if rows else None,'messages':rows[:5],'processed_through':n})
        return output


def migrate_production(path):
    """Allow a new trial only after the previous generation-owned trial is terminal.
    Preserve the frozen evaluator code, existing plan hashes, events and IDs.
    """
    if not Path(path).is_file(): return
    with exclusive(str(Path(path).resolve())+'.lock'),closing(sqlite3.connect(path,timeout=30)) as c,c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='one_active_production'").fetchone(): return
        c.execute('ALTER TABLE production_trials RENAME TO production_trials_legacy')
        c.execute('CREATE TABLE production_trials(id TEXT PRIMARY KEY, agent TEXT NOT NULL, plan TEXT NOT NULL, economy TEXT NOT NULL, state TEXT NOT NULL, snapshot TEXT NOT NULL)')
        c.execute('INSERT INTO production_trials SELECT id,agent,plan,economy,state,snapshot FROM production_trials_legacy')
        c.execute('DROP TABLE production_trials_legacy')
        c.execute("CREATE UNIQUE INDEX one_active_production ON production_trials(agent) WHERE state NOT IN ('complete','stopped')")
        c.execute("CREATE INDEX IF NOT EXISTS production_signal_events ON production_events(id DESC) WHERE json_array_length(json_extract(report,'$.fills'))>0 OR json_extract(report,'$.decision') IS NOT NULL OR json_array_length(json_extract(report,'$.missed'))>0")


def remember_candidate(path,agent,plan,result,report):
    if report is None or not result.get('paid') or result.get('state')!='complete' or not result.get('result',{}).get('improved_research'): return None
    with closing(council.connect(path)) as c,c:
        row=c.execute("SELECT key FROM council_experiments WHERE turn=? AND agent=? AND state='complete'",(result['turn'],agent)).fetchone()
        if not row: return None
        key=row[0]; c.execute('INSERT OR IGNORE INTO runtime_candidates VALUES(?,?,?,?,?)',(key,agent,pack(plan),pack(report),int(time.time())))
        message={'experiment_key':key,'agent':agent,'note':'Qualified historical directional candidate; may request deploy_paper. No live eligibility.'}
        council.message(c,'system',agent,'candidate',pack(message)); return message


def candidate_list(path):
    with closing(council.connect(path)) as c:
        return [dict(r,plan=json.loads(r['plan'])) for r in c.execute('SELECT key,agent,plan,created FROM runtime_candidates ORDER BY created DESC LIMIT 50')]


def deployment_action(paths,agent,plan,parts,cutoff,data_identity):
    from . import production
    cp,ep,pp=paths['council'],paths['economy'],paths['production']; ident=agent['id']; trials=production_status(pp)['trials']
    if plan['action']=='stop_paper':
        matches=[t for t in trials if t['id']==plan['parameters']['trial_id'] and t['agent']==ident and t['state'] not in ('complete','stopped')]
        if len(matches)!=1: raise ValueError('Can stop only an own active paper trial')
        production.stop(pp,matches[0]['id']); return {'action':'stop_paper','trial':matches[0]['id'],'note':'Flatten at next fresh quote; no retroactive fill.'}
    key=plan['parameters']['experiment_key']
    with closing(council.connect(cp)) as c:
        row=c.execute('SELECT * FROM runtime_candidates WHERE key=? AND agent=?',(key,ident)).fetchone()
        if not row: raise ValueError('No independently qualified own research candidate')
        p=json.loads(row['plan'])['parameters']; report=json.loads(row['report'])
        if council.digest([council.PROTOCOL,data_identity,'directional',p])!=key or report.get('cutoff')!=cutoff: raise ValueError('Candidate was evaluated on a different research dataset')
    if Path(pp).is_file():
        with closing(sqlite3.connect(Path(pp).resolve().as_uri()+'?mode=ro',uri=True)) as c:
            if c.execute("SELECT 1 FROM production_trials WHERE agent=? AND json_extract(plan,'$.source_experiment')=?",(ident,key)).fetchone(): raise ValueError('This candidate has already been deployed by this generation')
    if any(t['agent']==ident and t['state'] not in ('complete','stopped') for t in trials): raise ValueError('Stop/complete the existing trial before deploying a new one')
    # Do not open a new trial before all previous closed cash is reconciled.
    advance_economy(ep,trials)
    a=next(a for a in economy.status(ep)['agents'] if a['id']==ident)
    if a['state']!='active' or a['productive_cents']<=0: raise ValueError('Agent has no active productive budget')
    # Current registration, not retroactive: freeze exact measured report and training.
    now=int(time.time()); start=(now//3600+2)*3600
    review={'dataset':data_identity,'results':[{'id':1,'horizon_hours':p['horizon'],'spec':p['spec'],'reports':{p['pair']:report}}]}
    artifact=production.freeze_artifact(review,parts,cutoff,1,p['pair'],p['horizon'],ident,a['productive_cents'],start,start+90*DAY,now)
    artifact.update(source_node=None,source_experiment=key,source='council_v27')
    if artifact['thresholds']['candidate'] is None: raise ValueError('Frozen calibration abstains; no qualifying deployment')
    if get_state(cp).get('research_paused') or get_state(cp).get('autonomy_paused'): raise ValueError('Deployment paused by operator')
    trial=production.create(pp,artifact,ep); migrate_production(pp)
    advance_economy(ep,production_status(pp)['trials'])
    return {'action':'deploy_paper','trial':trial,'experiment_key':key,'thresholds':artifact['thresholds'],'start':start,'paper_only':True}


def production_status(path):
    if not Path(path).is_file(): return {'trials':[],'note':'No frozen production trials'}
    from . import production
    return production.status(path)


def trade_events(path):
    if not Path(path).is_file(): return []
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row; rows=[]
        for r in c.execute("SELECT e.id,e.observed,e.report,t.agent,t.plan FROM production_events e JOIN production_trials t ON t.id=e.trial WHERE json_array_length(json_extract(e.report,'$.fills'))>0 OR json_extract(e.report,'$.decision') IS NOT NULL OR json_array_length(json_extract(e.report,'$.missed'))>0 ORDER BY e.id DESC LIMIT 200"):
            d=json.loads(r['report']); plan=json.loads(r['plan'])
            for f in d.get('fills',[]):
                rows.append({'id':r['id'],'time':r['observed'],'agent':r['agent'],'account':f['account'],'side':f['side'],'reason':f['reason'],'bid':d.get('bid'),'ask':d.get('ask'),'cash_after':f['after']['cash'],'units_after':f['after']['units'],'pair':plan.get('pair'),'quantity':abs(f['after']['units']-f['before']['units']),'fee_usd':f['after']['fees']-f['before']['fees']})
            if d.get('decision') or d.get('missed'):
                rows.append({'id':r['id'],'time':r['observed'],'agent':r['agent'],'decision':d.get('decision'),'missed':d.get('missed',[])})
            if len(rows)>=100: break
        return rows[:100]


def peer_profiles(snapshot,agents,economy_path=None):
    out=[]; expenses={}; settled={}
    if economy_path:
        with closing(economy.connect(economy_path,True)) as c:
            expenses={row[0]:row[1] for row in c.execute('SELECT agent,sum(fee) FROM economy_tasks GROUP BY agent')}
            for row in c.execute("SELECT source,destination,cents,kind FROM economy_entries WHERE kind IN ('paper_gain','paper_loss')"):
                ident=(row[1] if row[3]=='paper_gain' else row[0]).rsplit(':',1)[0]
                settled[ident]=settled.get(ident,0)+(row[2] if row[3]=='paper_gain' else -row[2])
    for a in agents:
        rows=[r for r in snapshot['experiments'] if r['agent']==a['id']]; counts={}; positive=0; completed=0
        for r in rows:
            payload=r['result']; action=payload.get('council_plan',{}).get('action','unknown'); counts[action]=counts.get(action,0)+1
            if r['state']=='complete': completed+=1
            if payload.get('improved_research'): positive+=1
        out.append({'agent':a['id'],'state':a['state'],'policy':a['policy'],'operating_cents':a['operating_cents'],'recent_experiments':len(rows),'completed':completed,'positive_controlled_directional':positive,'tools_attempted':counts,'research_cost_cents':expenses.get(a['id'],0),'settled_paper_net_cents':settled.get(a['id'],0),'note':'Measured recent activity; no proven live profitability.'})
    return out


def pending_peer(path,agent):
    with closing(council.connect(path)) as c: return bool(council.unread_messages(c,agent['id'],'peer',1))


def pending_operator(path,agent):
    with closing(council.connect(path)) as c: return bool(council.inbox(c,agent['id'])[2])


def choose_agent(path,agents,cursor,now,last,last_peer,research_interval):
    if not agents: return None
    ordered=agents[cursor%len(agents):]+agents[:cursor%len(agents)]
    for a in ordered:
        if pending_operator(path,a) and now-last.get(a['id'],0)>=5: return a
    for a in ordered:
        if pending_peer(path,a) and now-last_peer.get(a['id'],0)>=60: return a
    return next((a for a in ordered if now-last.get(a['id'],0)>=research_interval),None)


def recover_assignments(path,agents):
    living={a['id'] for a in agents if a['state']!='retired'}
    with closing(council.connect(path)) as c,c:
        for r in c.execute("SELECT * FROM council_assignments WHERE state IN ('pending','running')").fetchall():
            if r['state']=='pending' and r['assignee'] not in living:
                c.execute("UPDATE council_assignments SET state='cancelled',result=? WHERE id=?",(pack({'reason':'Assignee retired'}),r['id']))
            elif r['state']=='running':
                key=json.loads(r['result'] or '{}').get('experiment'); exp=c.execute('SELECT state,result FROM council_experiments WHERE key=?',(key,)).fetchone()
                if exp and exp['state']!='running': c.execute('UPDATE council_assignments SET state=?,result=? WHERE id=?',(exp['state'],pack({'experiment':key,'result':json.loads(exp['result'])}),r['id']))


def market_request(endpoint,params):
    from urllib.parse import urlencode
    from urllib.request import Request,urlopen
    req=Request('https://api.kraken.com/0/public/'+endpoint+'?'+urlencode(params),headers={'User-Agent':'Autarky/0.27'})
    with urlopen(req,timeout=10) as response: raw=json.load(response)
    if raw['error']: raise RuntimeError(raw['error'])
    return raw['result']


def market_candles(pair):
    rows=market_request('OHLC',{'pair':pair,'interval':60}); key=next(k for k in rows if k!='last')
    return core.validate([core.Bar(int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[6])) for x in rows[key][:-1]],3600)


def market_quote(pair):
    rows=market_request('Ticker',{'pair':pair}); row=next(iter(rows.values())); bid,ask=float(row['b'][0]),float(row['a'][0])
    if not math.isfinite(ask) or not 0<bid<=ask: raise ValueError('Invalid market quote')
    return bid,ask


def secondary_quote(pair):
    from urllib.request import Request,urlopen
    symbol={'XBTUSD':'BTC-USD','ETHUSD':'ETH-USD'}[pair]
    req=Request('https://api.exchange.coinbase.com/products/'+symbol+'/ticker',headers={'User-Agent':'Autarky/0.28'})
    with urlopen(req,timeout=5) as response: raw=json.load(response)
    bid,ask=float(raw['bid']),float(raw['ask'])
    if not math.isfinite(ask) or not 0<bid<=ask: raise ValueError('Invalid Coinbase quote')
    return {'bid':bid,'ask':ask,'observed':int(time.time())}


def market_loop(paths,stop,poll):
    """Independent public market worker; a slow LLM cannot block price monitoring."""
    from . import production
    cp,pp=paths['council'],paths['production']; bars={}; last_hour=None; last_sample=0; last_secondary=0; secondary={}; spreads=[]; predictions=get_state(cp).get('market',{}).get('predictions',{}); forecasts=get_state(cp).get('market',{}).get('forecasts',{})
    while not stop.is_set():
        try:
            now=int(time.time()); hour=now//3600*3600; trials=production_status(pp)['trials']; quotes={}; errors={}
            if hour!=last_hour:
                try:
                    bars={p:market_candles(p) for p in production.PAIRS}; last_hour=hour
                    if any(v[-1].ts!=hour-3600 for v in bars.values()): last_hour=None
                except Exception as exc: bars={}; errors['candles']=str(exc)
            # Informational hourly forecasts for active frozen strategies. These never authorize fills.
            if bars and last_hour==hour and Path(pp).is_file():
                with closing(sqlite3.connect(Path(pp).resolve().as_uri()+'?mode=ro',uri=True)) as db:
                    for ident,raw in db.execute("SELECT id,plan FROM production_trials WHERE state NOT IN ('complete','stopped')"):
                        if forecasts.get(ident,{}).get('hour')==hour: continue
                        plan=json.loads(raw)
                        try:
                            if production.digest(plan)!=ident or plan['code_identity']!=production.code_identity(): raise ValueError('Frozen model identity changed')
                            forecasts[ident]={'hour':hour,'agent':plan['agent'],'pair':plan['pair'],'horizon_hours':plan['horizon_hours'],'predictions':production.prediction(plan,bars,hour),'thresholds':plan['thresholds'],'informational_only':True}
                        except (ValueError,KeyError,TypeError) as exc: errors['forecast:'+plan['agent']]=str(exc)
            forecasts={k:v for k,v in forecasts.items() if any(t['id']==k and t['state'] not in ('complete','stopped') for t in trials)}
            for pair in production.PAIRS:
                try:
                    start=time.monotonic(); bid,ask=market_quote(pair)
                    if time.monotonic()-start>15: raise ValueError('Quote latency exceeded 15 seconds')
                    qtime=int(time.time()); quotes[pair]={'bid':bid,'ask':ask,'observed':qtime,'spread_bps':10000*(ask-bid)/((ask+bid)/2)}
                    if pair in bars:
                        b=bars[pair]; quotes[pair].update(last_closed=b[-1].ts,return_24h=b[-1].close/b[-25].close-1,return_168h=b[-1].close/b[-169].close-1)
                    if stop.is_set(): return
                    if Path(pp).is_file():
                        with exclusive(str(Path(pp).resolve())+'.lock'):
                            for t in trials:
                                if t['pair']!=pair or t['state'] in ('complete','stopped'): continue
                                # Existing frozen v0.25 rules are authoritative; never backfill an entry.
                                if qtime<t['start'] and not t['stop_requested']: continue
                                result=production.observe(pp,t['id'],bars or None,bid,ask,qtime)
                                if result.get('decision'): predictions[t['agent']]=result['decision']
                                if result.get('fills') or result.get('decision') or result.get('missed'): event(cp,'production',result,t['agent'])
                except Exception as exc: errors[pair]=str(exc)
            # Secondary venue is informational only; fetch after primary execution checks.
            if now-last_secondary>=60:
                last_secondary=now; secondary={}
                for pair in production.PAIRS:
                    try: secondary[pair]=secondary_quote(pair)
                    except Exception as exc: errors['coinbase:'+pair]=str(exc)
                from .mechanisms import spread_screen
                spreads=spread_screen(quotes,secondary,int(time.time()))
            set_state(cp,'market',{'secondary_quotes':secondary,'spread_screens':spreads,'observed':int(time.time()),'quotes':quotes,'errors':errors,'predictions':predictions,'forecasts':forecasts,'source':'Kraken public spot','paper_only':True})
            if int(time.time())-last_sample>=60:
                try: equity_point(paths); last_sample=int(time.time())
                except (OSError,ValueError,sqlite3.Error) as exc: errors['equity']=str(exc)
            if errors: event(cp,'market_warning',errors)
        except Exception as exc: event(cp,'market_error',{'error':str(exc)})
        stop.wait(poll)


def evidence_context(files):
    out=[]
    for f in files:
        r=json.loads(Path(f).read_text(encoding='utf-8-sig')); rows=[]
        for row in r.get('results',[])[:24]:
            short={k:v for k,v in row.items() if k in ('id','agent','pair','horizon_hours','rule','model','mean_stress','mean_net','stress_delta_random','stress_delta_risk','selected','fills','active_windows','rmse_skill')}
            if 'markets' in row: short['markets']={p:{k:v for k,v in m.items() if k in ('net','stress','fills','active_windows')} for p,m in row['markets'].items()}
            rows.append(short)
        out.append({'file':Path(f).name,'protocol':r.get('protocol'),'results':rows,'note':'Historical development only'})
    return out


def api_snapshot(paths):
    cp=paths['council']; d=council.snapshot(cp); e=economy.status(paths['economy'],include_locked=True); s=get_state(cp)
    try: p=production_status(paths['production']); trades=trade_events(paths['production'])
    except (OSError,ValueError,sqlite3.Error) as exc: p={'trials':[],'error':str(exc)}; trades=[]
    with closing(council.connect(cp)) as c:
        events=[dict(r,body=json.loads(r['body'])) for r in c.execute('SELECT * FROM runtime_events ORDER BY id DESC LIMIT 100')]
    marked=e['remaining_cents']; locked=e.pop('locked_production')
    for l in locked:
        trial=next((t for t in p['trials'] if t['id']==l['trial']),None)
        if trial: marked+=round(trial['accounts']['candidate_normal']['equity']*100)-l['settled_cents']
    expenses=paths.get('resources')
    actual=economy.actual_expenses(expenses) if expenses and Path(expenses).is_file() else {'recorded':False,'actual_expenses_usd':None}
    return dict(d,marked_pool_cents=marked,locked_production=locked,actual_expenses=actual,candidates=candidate_list(cp),protocol=PROTOCOL,economy=e,production=p,trade_events=trades,capital_history=capital_history(paths['economy']),peer_profiles=peer_profiles(d,e['agents'],paths['economy']),equity_history=equity_history(cp),operator_queue=operator_queue(cp,e['agents']),runtime=s,events=events,server_time=int(time.time()))


def serve(paths,port,stop):
    token=secrets.token_urlsafe(32); template=Path(__file__).with_name('runtime_ui.html').read_text(encoding='utf-8')
    class Handler(BaseHTTPRequestHandler):
        def valid_host(self): return self.headers.get('Host') in (f'127.0.0.1:{port}',f'localhost:{port}')
        def reply(self,body,ctype='application/json',code=200):
            self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Cache-Control','no-store'); self.send_header('Content-Security-Policy',f"default-src 'none'; script-src 'nonce-{token}'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'"); self.end_headers(); self.wfile.write(body.encode())
        def do_GET(self):
            if not self.valid_host(): return self.send_error(403)
            if self.path=='/': return self.reply(template.replace('__NONCE__',token),'text/html; charset=utf-8')
            if self.path=='/api/status':
                try: return self.reply(pack(api_snapshot(paths)))
                except Exception as exc: return self.reply(pack({'error':str(exc)}),code=503)
            return self.send_error(404)
        def do_POST(self):
            if not self.valid_host(): return self.send_error(403)
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=16000: raise ValueError('Invalid body')
                d=json.loads(self.rfile.read(n));
                if not secrets.compare_digest(d.get('token',''),token): return self.send_error(403)
                if self.path=='/api/message':
                    if not isinstance(d.get('body'),str) or len(d['body'])>2500: raise ValueError('Invalid message')
                    return self.reply(pack({'message_id':council.send(paths['council'],d['recipient'],d['body'])}))
                if self.path=='/api/stop-trial':
                    from . import production
                    trials=production_status(paths['production'])['trials']; matches=[t for t in trials if t['id']==d.get('trial') and t['state'] not in ('complete','stopped')]
                    if len(matches)!=1: raise ValueError('No active matching paper trial')
                    production.stop(paths['production'],matches[0]['id']); event(paths['council'],'operator_stop_trial',{'trial':matches[0]['id']},matches[0]['agent']); return self.reply(pack({'ok':True}))
                if self.path=='/api/control':
                    if d.get('action') not in ('pause_research','resume_research','pause_autonomy','resume_autonomy','flatten_all'): raise ValueError('Invalid control')
                    action=d['action']
                    if action=='flatten_all':
                        from . import production
                        set_state(paths['council'],'research_paused',True)
                        for t in production_status(paths['production'])['trials']:
                            if t['state'] not in ('complete','stopped'): production.stop(paths['production'],t['id'])
                    elif action.endswith('autonomy'): set_state(paths['council'],'autonomy_paused',action=='pause_autonomy')
                    else: set_state(paths['council'],'research_paused',action=='pause_research')
                    event(paths['council'],'operator_control',{'action':action}); return self.reply(pack({'ok':True}))
                return self.send_error(404)
            except (ValueError,KeyError,TypeError,sqlite3.Error) as exc: return self.reply(pack({'error':str(exc)}),code=400)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler); server.timeout=1
    try:
        while not stop.is_set(): server.handle_request()
    finally: server.server_close()


def run(paths,model,cutoff,evidence,research_interval=300,poll=30,daily_budget=3,port=8768):
    from . import multiyear, autonomous_research as old, production
    cp,ep,pp=paths['council'],paths['economy'],paths['production']; economy.status(ep)
    if not 30<=research_interval or not 15<=poll or not 0<=daily_budget<=20: raise ValueError('Research interval >=30, poll >=15, daily budget 0..20')
    parts={p:multiyear.segments(paths[k],cutoff) for p,k in zip(production.PAIRS,('btc','eth'))}; funding=json.loads(Path(paths['funding']).read_text(encoding='utf-8-sig'))
    base={'data_identity':council.digest([cutoff,{p:old.fingerprint(parts[p],p) for p in production.PAIRS},funding.get('events_sha256')]),'cutoff':cutoff,'historical_evidence':evidence_context(evidence)}
    stop=threading.Event(); last={}; last_peer={}; last_econ=0; cursor=0; job=None; executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='autarky-research')
    with exclusive(str(Path(cp).resolve())+'.runtime.lock'):
        ensure(cp)
        # Fail fast if the old production monitor still holds its lock.
        if Path(pp).is_file():
            with exclusive(str(Path(pp).resolve())+'.lock'): pass
        migrate_production(pp)
        with closing(council.connect(cp)) as c,c: council.recover_experiments(c,ep)
        set_state(cp,'research_job',None)
        for change in advance_economy(ep,production_status(pp)['trials']): event(cp,'paper_settlement',change,change['agent'])
        set_state(cp,'run',{'state':'running','started':int(time.time()),'heartbeat':int(time.time()),'daily_budget':daily_budget,'research_interval':research_interval,'poll':poll,'model':model})
        event(cp,'runtime_start',{'protocol':PROTOCOL,'paper_only':True})
        def heartbeat():
            while not stop.is_set():
                s=get_state(cp).get('run',{}); s['heartbeat']=int(time.time()); set_state(cp,'run',s); stop.wait(5)
        # Bind before workers start so an occupied port fails visibly.
        import socket
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',port))
        threads=[threading.Thread(target=market_loop,args=(paths,stop,poll),daemon=True),threading.Thread(target=heartbeat,daemon=True),threading.Thread(target=serve,args=(paths,port,stop),daemon=True)]
        for t in threads: t.start()
        print(f'Autarky runtime: http://127.0.0.1:{port} (Ctrl+C to stop)',flush=True)
        try:
            while not stop.is_set():
                now=int(time.time()); state=get_state(cp)
                if job is not None and job.done():
                    try: job.result()
                    except Exception as exc: event(cp,'research_worker_error',{'error':str(exc)})
                    job=None; set_state(cp,'research_job',None)
                if not threads[0].is_alive():
                    event(cp,'worker_restart',{'worker':'market'}); threads[0]=threading.Thread(target=market_loop,args=(paths,stop,poll),daemon=True); threads[0].start()
                try: trials=production_status(pp)['trials']; eco=economy.status(ep)
                except (OSError,ValueError,sqlite3.Error) as exc:
                    event(cp,'runtime_warning',{'error':str(exc)}); stop.wait(10); continue
                agents=[a for a in eco['agents'] if a['state']!='retired']
                if now-last_econ>=15:
                    try:
                        for change in advance_economy(ep,trials,now,recover_interrupted=job is None): event(cp,'paper_settlement',change,change['agent'])
                        for a in economy.status(ep)['agents']:
                            if a['state']=='liquidating':
                                for t in trials:
                                    if t['agent']==a['id'] and t['state'] not in ('complete','stopped') and not t['stop_requested']: production.stop(pp,t['id'])
                        last_econ=now
                    except (ValueError,RuntimeError,sqlite3.Error) as exc: event(cp,'economy_warning',{'error':str(exc)}); last_econ=now
                    eco=economy.status(ep); agents=[a for a in eco['agents'] if a['state']!='retired']
                if job is None:
                    with closing(council.connect(cp)) as c,c: council.recover_experiments(c,ep)
                    recover_assignments(cp,agents)
                chosen=choose_agent(cp,agents,cursor,now,last,last_peer,research_interval)
                if chosen and state.get('autonomy_paused',False) and not pending_operator(cp,chosen) and not pending_peer(cp,chosen): chosen=None
                if not chosen: stop.wait(2); continue
                a=chosen; ident=a['id']; cursor=agents.index(a)+1; last[ident]=now; last_peer[ident]=now
                snap=council.snapshot(cp); profiles=peer_profiles(snap,eco['agents'],ep); assignments=[r for r in snap['assignments'] if r['assignee']==ident and r['state']=='pending']
                # Count durable daily paid tasks from council experiment registrations using turn timestamps.
                with closing(council.connect(cp)) as c:
                    used=c.execute("SELECT count(*) FROM council_experiments e JOIN council_turns t ON t.id=e.turn WHERE t.created>=? AND e.state NOT IN ('rejected','duplicate')",(now//DAY*DAY,)).fetchone()[0]
                set_state(cp,'daily_research',{'day':now//DAY,'used':used,'limit':daily_budget})
                allowed=job is None and not state.get('research_paused',False) and not state.get('autonomy_paused',False) and used<daily_budget and a['state']=='active'
                market=state.get('market',{}); context={**base,'market':market,'production_trials':[t for t in trials if t['state'] not in ('complete','stopped') or t['agent']==ident][-8:],'peer_profiles':profiles,'pending_assignments':assignments,'open_projects':[{'id':p['id'],'owner':p['owner'],'title':p['title'],'hypothesis':p['hypothesis'],'updates':p['updates'][:2]} for p in snap['projects'] if p['state']=='open'][:8],'qualified_candidates':[r for r in candidate_list(cp) if r['agent']==ident][:8], 'dialogue_enabled':True,'research_budget_today':max(0,daily_budget-used),'research_worker_busy':job is not None,'market_note':'Current observations are timestamped; stale/missing data is not a live quote. Frozen paper trades execute independently. Do not claim a trade you did not execute.'}
                def activity(label): set_state(cp,'agent:'+ident,{'state':label,'since':int(time.time())})
                try:
                    captured={}
                    def worker(plan,captured=captured):
                        captured['plan']=plan
                        return council.research(plan,parts,funding,cutoff,lambda r:captured.update(report=r),cache=Path(paths['funding']).parent/'carry-archives')
                    def complete(result,ident=ident,captured=captured):
                        candidate=remember_candidate(cp,ident,captured.get('plan'),result,captured.get('report'))
                        if candidate: event(cp,'qualified_candidate',candidate,ident)
                        event(cp,'research_finished',{'turn':result.get('turn'),'state':result.get('state'),'paid':result.get('paid')},ident)
                    def dispatch(fn):
                        nonlocal job
                        if job is not None: raise RuntimeError('Research worker already occupied')
                        set_state(cp,'research_job',{'agent':ident,'started':int(time.time()),'state':'researching'})
                        job=executor.submit(fn)
                    result=council.turn(cp,ep,a,model,context,worker,allowed,activity=activity,paid_gate=lambda:job is None and not get_state(cp).get('research_paused',False),dispatch=dispatch,on_complete=complete)
                    plan=result.get('plan',{})
                    if plan.get('action') in ('deploy_paper','stop_paper'):
                        try:
                            if plan['action']=='deploy_paper' and (job is not None or get_state(cp).get('research_paused',False) or get_state(cp).get('autonomy_paused',False)): raise ValueError('Deployment paused by operator')
                            decision=deployment_action(paths,a,plan,parts,cutoff,base['data_identity']); event(cp,'paper_decision',decision,ident)
                            with closing(council.connect(cp)) as c,c: council.message(c,'system',ident,'paper_decision',pack(decision))
                        except (ValueError,RuntimeError,sqlite3.Error) as exc:
                            event(cp,'deployment_rejected',{'error':str(exc)},ident)
                            with closing(council.connect(cp)) as c,c: council.message(c,'system',ident,'paper_decision','Rejected: '+str(exc))
                    event(cp,'agent_turn',{'turn':result.get('turn'),'paid':result.get('paid',False),'state':result.get('state'),'action':result.get('plan',{}).get('action')},ident); print(pack({'agent':ident,'turn':result.get('turn'),'paid':result.get('paid',False),'state':result.get('state','complete')}),flush=True)
                    activity('waiting')
                except Exception as exc: activity('error'); event(cp,'agent_error',{'error':str(exc)},ident); print(pack({'agent':ident,'error':str(exc)}),flush=True)
                stop.wait(2)
        except KeyboardInterrupt: pass
        finally:
            stop.set()
            # A committed research task is allowed to finish; no new task starts during shutdown.
            executor.shutdown(wait=True,cancel_futures=False)
            set_state(cp,'research_job',None)
            for t in threads: t.join(timeout=2)
            s=get_state(cp).get('run',{}); s.update(state='stopped',heartbeat=int(time.time())); set_state(cp,'run',s); event(cp,'runtime_stop',{})


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--home',required=True); p.add_argument('--model',default='qwen3:4b'); p.add_argument('--cutoff',type=int,required=True); p.add_argument('--research-interval',type=int,default=300); p.add_argument('--poll',type=int,default=30); p.add_argument('--daily-budget',type=int,default=3); p.add_argument('--port',type=int,default=8768); p.add_argument('--check',action='store_true',help='Read-only installation and frozen model checks')
    x=p.parse_args(); h=Path(x.home).resolve(); paths={k:h/(k+'.sqlite') for k in ('council','economy','production')}; paths.update(resources=h/'btc-v2.sqlite',btc=h/'history/btc-hourly-full.csv',eth=h/'history/eth-hourly-full.csv',funding=h/'history/funding-btc-eth-v22.json'); evidence=[f for f in (h/'history/funding-state-v23.json',h/'history/horizon-review-v20.json') if f.exists()]
    if x.check:
        from . import production, multiyear
        checks={'paths':{k:{'path':str(v),'exists':Path(v).is_file()} for k,v in paths.items()},'paper_only':True}
        checks['economy']=economy.status(paths['economy'])['reconciled']
        checks['production_trials']=len(production_status(paths['production'])['trials'])
        checks['history_bars']={k:sum(map(len,multiyear.segments(paths[k],x.cutoff))) for k in ('btc','eth')}
        if Path(paths['production']).is_file():
            with closing(sqlite3.connect(Path(paths['production']).resolve().as_uri()+'?mode=ro',uri=True)) as c:
                checks['frozen_identity_ok']=all(production.digest(json.loads(p))==i and json.loads(p)['code_identity']==production.code_identity() for i,p in c.execute('SELECT id,plan FROM production_trials'))
        print(json.dumps(checks,indent=2)); return
    run(paths,x.model,x.cutoff,evidence,x.research_interval,x.poll,x.daily_budget,x.port)

if __name__=='__main__': main()

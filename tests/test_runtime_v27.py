import json
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
from autarky_trade import runtime as r, council, economic_kernel as e


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.cp=self.root/'council.sqlite'; self.ep=self.root/'economy.sqlite'; self.pp=self.root/'production.sqlite'
        self.paths={'council':self.cp,'economy':self.ep,'production':self.pp}; e.init(self.ep); r.ensure(self.cp)
        self.now=1700000000

    def agent(self,slot): return next(a for a in e.status(self.ep)['agents'] if a['slot']==slot and a['state']!='retired')
    def trial(self,cash=200,units=0,state='running',trades=0):
        return {'id':'trial-alpha','agent':'alpha:1','initial_usd':200,'state':state,'accounts':{'candidate_normal':{'cash':cash,'units':units,'trades':trades},'candidate_stress':{'cash':999,'units':0},'mean_normal':{'cash':1000,'units':0}}}
    def plan(self,action='talk',recipient='all',parameters=None):
        return {'action':action,'recipient':recipient,'parameters':parameters or {},'message':'Research discussion','rationale':'Test hypothesis'}
    def turn(self,slot,plan,worker=None,paid=False,**kwargs):
        return council.turn(self.cp,self.ep,self.agent(slot),'qwen',{'data_identity':'fixture'},worker,paid,provider=lambda *args:(plan,'local trace'),**kwargs)

    def test_daily_clock_not_research_ticks(self):
        r.advance_economy(self.ep,[],self.now); r.advance_economy(self.ep,[],self.now+60)
        self.assertEqual(e.status(self.ep)['spent_cents'],0)
        self.turn('alpha',self.plan('carry_screen',parameters={'pair':'XBTUSD'}),lambda p:{'mean_gross':.01},True)
        self.assertEqual(e.status(self.ep)['spent_cents'],100)
        r.advance_economy(self.ep,[],self.now+r.DAY); r.advance_economy(self.ep,[],self.now+r.DAY)
        self.assertEqual(e.status(self.ep)['spent_cents'],175)
        self.assertEqual(r.capital_history(self.ep)[-1]['remaining_cents'],99825)
        self.assertTrue(e.status(self.ep)['reconciled'])

    def test_closed_paper_cash_once_and_operating_distribution(self):
        r.advance_economy(self.ep,[self.trial()],self.now)
        self.assertEqual(r.advance_economy(self.ep,[self.trial(150,1,trades=1)],self.now),[])
        self.assertEqual(e.status(self.ep)['paper_net_cents'],0)
        gain=r.advance_economy(self.ep,[self.trial(205,0,trades=2)],self.now); self.assertEqual(gain[0]['delta_cents'],500)
        self.assertEqual(r.advance_economy(self.ep,[self.trial(205,0,trades=2)],self.now),[])
        self.assertEqual(e.status(self.ep)['paper_net_cents'],500)
        r.advance_economy(self.ep,[self.trial(202,0,trades=4)],self.now); self.assertEqual(e.status(self.ep)['paper_net_cents'],200)
        r.advance_economy(self.ep,[self.trial(202,0,'stopped',4)],self.now)
        self.assertEqual(self.agent('alpha')['operating_cents'],5100); self.assertEqual(self.agent('alpha')['productive_cents'],20100)
        self.assertEqual(e.status(self.ep)['remaining_cents'],100200); self.assertTrue(e.status(self.ep)['reconciled'])
        self.assertEqual(r.advance_economy(self.ep,[self.trial(202,0,'stopped',4)],self.now),[])
        self.assertEqual(r.capital_history(self.ep)[-1]['paper_net_cents'],200)

    def test_reserved_agent_liquidates_before_retirement(self):
        r.advance_economy(self.ep,[self.trial(150,1,trades=1)],self.now)
        with closing(e.connect(self.ep)) as c,c: e.transfer(c,0,'alpha:1:operating','spent',5000,'test','test')
        r.advance_economy(self.ep,[self.trial(150,1,trades=1)],self.now+2*r.DAY)
        self.assertEqual(self.agent('alpha')['state'],'liquidating')
        self.assertEqual(self.agent('alpha')['productive_cents'],20000)
        r.advance_economy(self.ep,[self.trial(190,0,'stopped',2)],self.now+3*r.DAY)
        self.assertEqual(self.agent('alpha')['id'],'alpha:2'); self.assertEqual(e.status(self.ep)['paper_net_cents'],-1000)
        self.assertTrue(e.status(self.ep)['reconciled'])

    def test_peer_delegation_wake_accept_result(self):
        a=self.agent('alpha'); b=self.agent('beta'); g=self.agent('gamma'); agents=[a,b,g]
        result=self.turn('alpha',self.plan('delegate','beta',{'action':'carry_screen','parameters':{'pair':'XBTUSD'},'question':'Check carry cash flow'}))
        task=result['assignment']; self.assertTrue(r.pending_peer(self.cp,b)); self.assertEqual(r.choose_agent(self.cp,agents,0,self.now,{}, {},300)['id'],'beta:1')
        council.send(self.cp,'gamma','Operator question'); self.assertEqual(r.choose_agent(self.cp,agents,0,self.now,{}, {},300)['id'],'gamma:1')
        r.advance_economy(self.ep,[],self.now)
        res=self.turn('beta',self.plan('accept_task','alpha',{'task_id':task}),lambda p:{'mean_gross':.005},True)
        self.assertTrue(res['paid']); assignment=council.snapshot(self.cp)['assignments'][0]; self.assertEqual(assignment['state'],'complete')
        self.assertEqual(assignment['assignee'],'beta:1'); self.assertTrue(r.pending_peer(self.cp,a))
        self.assertEqual(e.status(self.ep)['spent_cents'],100)
        self.assertFalse(r.pending_peer(self.cp,b))
        self.assertTrue(any(m['kind']=='assignment_result' for m in council.snapshot(self.cp)['messages']))

    def test_peer_review_decline_and_pause(self):
        r.advance_economy(self.ep,[],self.now)
        self.turn('alpha',self.plan('carry_screen',parameters={'pair':'XBTUSD'}),lambda p:{'mean_gross':.002},True)
        key=council.snapshot(self.cp)['experiments'][0]['key']; rev=self.turn('beta',self.plan('review','all',{'experiment_key':key}))
        self.assertEqual(rev['review']['author'],'alpha:1'); self.assertFalse(rev['review']['eligible'])
        d=self.turn('gamma',self.plan('delegate','beta',{'action':'review','parameters':{'experiment_key':key},'question':'Check the evidence'}))
        self.turn('beta',self.plan('decline_task','gamma',{'task_id':d['assignment']})); self.assertEqual(council.snapshot(self.cp)['assignments'][0]['state'],'declined')
        result=self.turn('alpha',self.plan('carry_screen',parameters={'pair':'ETHUSD'}),lambda p: self.fail('Paused research ran'),True,paid_gate=lambda:False)
        self.assertFalse(result['paid']); self.assertEqual(e.status(self.ep)['spent_cents'],100)

    def test_project_link_and_reproducible_peer_work(self):
        r.advance_economy(self.ep,[],self.now)
        project=self.turn('alpha',self.plan('create_project',parameters={'title':'Carry','hypothesis':'Funding might offset costs'}))['project']
        result=self.turn('alpha',self.plan('delegate','beta',{'action':'carry_screen','parameters':{'pair':'ETHUSD'},'question':'Measure receipts','project_id':project}))
        self.turn('beta',self.plan('accept_task','alpha',{'task_id':result['assignment']}),lambda p:{'mean_gross':.004},True)
        snap=council.snapshot(self.cp); key=snap['experiments'][0]['key']; self.assertEqual(snap['projects'][0]['updates'][0]['evidence'],key)
        result=self.turn('gamma',self.plan('verify','all',{'experiment_key':key}),lambda p:{'mean_gross':.004},True)
        self.assertTrue(result['result']['result']['verified_same_data'])
        self.turn('alpha',self.plan('project_update',parameters={'project_id':project,'update':'Reviewed carry evidence','state':'complete','experiment_key':key}))
        self.assertEqual(council.snapshot(self.cp)['projects'][0]['state'],'complete')
        self.assertEqual(e.status(self.ep)['spent_cents'],200)

    def test_market_adapter_discards_open_candle(self):
        bars=[[self.now//3600*3600+i*3600,'100','101','99','100','100','2',1] for i in range(221)]
        with patch.object(r,'market_request',return_value={'XXBTZUSD':bars,'last':0}):
            result=r.market_candles('XBTUSD'); self.assertEqual(len(result),220); self.assertEqual(result[-1].ts,bars[-2][0])
        with patch.object(r,'market_request',return_value={'XXBTZUSD':{'b':['100'],'a':['101']}}): self.assertEqual(r.market_quote('XBTUSD'),(100,101))

    def test_production_migration_preserves_rows_and_active_uniqueness(self):
        with sqlite3.connect(self.pp) as c:
            c.executescript('''CREATE TABLE production_trials(id TEXT PRIMARY KEY,agent TEXT NOT NULL UNIQUE,plan TEXT NOT NULL,economy TEXT NOT NULL,state TEXT NOT NULL,snapshot TEXT NOT NULL); CREATE TABLE production_events(id INTEGER PRIMARY KEY,trial TEXT NOT NULL,observed INTEGER NOT NULL,report TEXT NOT NULL,UNIQUE(trial,observed));''')
            c.execute("INSERT INTO production_trials VALUES('one','alpha:1','{}','economy','pending','{}')")
            c.execute('INSERT INTO production_events VALUES(1,?,?,?)',('one',self.now,json.dumps({'decision':{'predictions':{'candidate':.01}},'fills':[]})))
        r.migrate_production(self.pp); r.migrate_production(self.pp)
        with sqlite3.connect(self.pp) as c:
            self.assertEqual(c.execute('SELECT id,plan FROM production_trials').fetchall(),[('one','{}')])
            with self.assertRaises(sqlite3.IntegrityError): c.execute("INSERT INTO production_trials VALUES('two','alpha:1','{}','economy','pending','{}')")
            c.execute("UPDATE production_trials SET state='stopped' WHERE id='one'"); c.execute("INSERT INTO production_trials VALUES('two','alpha:1','{}','economy','pending','{}')")
        self.assertEqual(r.trade_events(self.pp)[0]['decision']['predictions']['candidate'],.01)

    def test_dashboard_http_and_persistence(self):
        import socket
        with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        stop=threading.Event(); thread=threading.Thread(target=r.serve,args=(self.paths,port,stop),daemon=True); thread.start(); self.addCleanup(thread.join,3); self.addCleanup(stop.set)
        url=f'http://127.0.0.1:{port}'
        for _ in range(40):
            try:
                with urllib.request.urlopen(url,timeout=1) as res: html=res.read().decode(); break
            except OSError: time.sleep(.05)
        else: self.fail('Dashboard did not start')
        import re
        token=re.search(r'<script nonce="([^"]+)"',html).group(1)
        self.assertIn('Community capital history',html); self.assertIn('Research assignments',html)
        req=urllib.request.Request(url+'/api/message',data=json.dumps({'token':token,'recipient':'alpha','body':'Test operator message'}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=2) as res: self.assertGreater(json.load(res)['message_id'],0)
        with urllib.request.urlopen(url+'/api/status',timeout=2) as res: status=json.load(res)
        self.assertEqual(status['messages'][0]['body'],'Test operator message'); self.assertEqual(status['economy']['remaining_cents'],100000)
        self.assertEqual(status['capital_history'][0]['reserve_cents'],25000)

    def test_no_agents_scheduler_and_restart_recovery(self):
        self.assertIsNone(r.choose_agent(self.cp,[],0,self.now,{}, {},300))
        self.assertEqual(r.choose_agent(self.cp,[self.agent('alpha')],0,self.now,{'alpha:1':self.now},{},300),None)
        d=self.turn('alpha',self.plan('delegate','beta',{'action':'carry_screen','parameters':{'pair':'XBTUSD'},'question':'Check'}))
        with closing(council.connect(self.cp)) as c,c: c.execute("UPDATE council_assignments SET state='running',result=? WHERE id=?",(json.dumps({'experiment':'fixture'}),d['assignment'])); c.execute("INSERT INTO council_experiments VALUES('fixture','beta:1',1,'task','interrupted','{}')")
        r.recover_assignments(self.cp,[self.agent('alpha'),self.agent('beta')]); self.assertEqual(council.snapshot(self.cp)['assignments'][0]['state'],'interrupted')

    def test_real_frozen_production_runtime_settlement(self):
        try:
            from autarky_trade import production as p, research_tree as tree, horizon_review as hr, core, lab, cross_asset as cross
        except ImportError:
            self.skipTest('Full repository production module required')
        from dataclasses import asdict
        def market(n,start=1700002800):
            rows=[]; price=100.
            for i in range(n):
                end=price*1.0002; rows.append(core.Bar(start+i*3600,price,end,price,end,100.)); price=end
            return rows
        bars=market(721+720*25); parts={'XBTUSD':[bars],'ETHUSD':[bars]}; cutoff=bars[-1].ts; now=cutoff+3600; start=(now//3600+2)*3600
        spec=tree.Spec((('momentum',24),('other_momentum',24))); parent=tree.Spec((('momentum',24),))
        dataset=lab.digest([cross.PROTOCOL,cutoff,{q:tree.old.fingerprint(parts[q],q) for q in p.PAIRS},{q:tree.old.fingerprint(parts[q],q) for q in p.PAIRS}])
        node=dict(id=1,dataset=dataset,agent='alpha',parent='ridge_trend_only',spec=asdict(spec),parent_spec=asdict(parent))
        review=hr.review([node],parts,cutoff,horizons=(168,),now=now)
        plan=p.freeze_artifact(review,parts,cutoff,1,'XBTUSD',168,'alpha:1',20000,start,start+90*86400,now)
        ident=p.create(self.pp,plan,self.ep); r.migrate_production(self.pp); r.advance_economy(self.ep,p.status(self.pp)['trials'],now)
        current=market(719,start-719*3600); event=p.observe(self.pp,ident,{'XBTUSD':current,'ETHUSD':current},100,101,start+1)
        self.assertTrue(event['fills']); self.assertEqual(r.advance_economy(self.ep,p.status(self.pp)['trials'],start+1),[])
        p.stop(self.pp,ident); p.observe(self.pp,ident,None,110,111,start+2)
        changes=r.advance_economy(self.ep,p.status(self.pp)['trials'],start+2); self.assertTrue(changes); self.assertGreater(e.status(self.ep)['paper_net_cents'],0)
        self.assertTrue(e.status(self.ep)['reconciled']); self.assertEqual(r.advance_economy(self.ep,p.status(self.pp)['trials'],start+2),[])
        self.assertTrue(r.trade_events(self.pp))

if __name__=='__main__': unittest.main()

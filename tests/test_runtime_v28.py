import hashlib, io, json, sqlite3, tempfile, threading, unittest
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile
from unittest.mock import patch
from autarky_trade import council, runtime, economic_kernel as economy, mechanisms, funding_research as funding, lab
from autarky_trade.core import Bar

class Runtime28(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.cp=self.root/'council.sqlite'; self.ep=self.root/'economy.sqlite'; self.pp=self.root/'production.sqlite'
        self.paths={'council':self.cp,'economy':self.ep,'production':self.pp}; economy.init(self.ep); runtime.ensure(self.cp)
        runtime.advance_economy(self.ep,[],1700000000)
    def agent(self,slot): return next(a for a in economy.status(self.ep)['agents'] if a['slot']==slot)
    def plan(self,action='talk',recipient='operator',parameters=None): return {'action':action,'recipient':recipient,'message':'Test response','rationale':'Test rationale','parameters':parameters or {}}
    def turn(self,slot,plan,**kwargs): return council.turn(self.cp,self.ep,self.agent(slot),'qwen',{'data_identity':'fixture','dialogue_enabled':True},provider=lambda *a:(plan,None),**kwargs)
    def test_operator_priority_does_not_consume_peer(self):
        with closing(council.connect(self.cp)) as c,c: council.message(c,'beta:1','alpha:1','dialogue','Please inspect my results')
        council.send(self.cp,'alpha','Please answer this')
        with self.assertRaisesRegex(ValueError,'Operator reply'): self.turn('alpha',self.plan('conserve','all'))
        self.assertEqual(runtime.operator_queue(self.cp,[self.agent('alpha')])[0]['pending'],1)
        self.turn('alpha',self.plan())
        self.assertEqual(runtime.operator_queue(self.cp,[self.agent('alpha')])[0]['pending'],0)
        self.assertTrue(runtime.pending_peer(self.cp,self.agent('alpha')))
        self.turn('alpha',self.plan('talk','beta'))
        self.assertFalse(runtime.pending_peer(self.cp,self.agent('alpha')))
    def test_async_research_and_reply_while_worker_blocks(self):
        entered=threading.Event(); release=threading.Event(); done=[]; threads=[]
        def worker(plan): entered.set(); self.assertTrue(release.wait(5)); return {'mean_gross':.01}
        def dispatch(fn):
            t=threading.Thread(target=fn); threads.append(t); t.start()
        try:
            res=self.turn('alpha',self.plan('carry_screen','all',{'pair':'XBTUSD'}),worker=worker,allow_paid=True,dispatch=dispatch,on_complete=done.append)
            self.assertEqual(res['state'],'queued'); self.assertTrue(entered.wait(3)); self.assertEqual(economy.status(self.ep)['spent_cents'],100)
            runtime.advance_economy(self.ep,[],1700000000+runtime.DAY,recover_interrupted=False)
            self.assertEqual(economy.status(self.ep)['spent_cents'],175)
            self.assertEqual(economy.status(self.ep)['recent_tasks'][0]['state'],'running')
            council.send(self.cp,'beta','Is the experiment still running?')
            result=self.turn('beta',self.plan(),dispatch=lambda fn:self.fail('Unexpected job'))
            self.assertEqual(result['plan']['recipient'],'operator'); self.assertEqual(council.snapshot(self.cp)['experiments'][0]['state'],'running')
        finally:
            release.set()
            for t in threads:t.join(5)
        self.assertEqual(done[0]['state'],'complete'); self.assertEqual(economy.status(self.ep)['spent_cents'],175)
        self.assertTrue(economy.status(self.ep)['reconciled'])
    def test_delivery_migration_keeps_previous_processed_history(self):
        council.send(self.cp,'alpha','Old message'); self.turn('alpha',self.plan())
        with sqlite3.connect(self.cp) as c:
            c.execute('DROP TABLE council_deliveries'); c.execute('DROP TABLE council_delivery_baseline')
        runtime.ensure(self.cp); self.assertFalse(runtime.pending_operator(self.cp,self.agent('alpha')))
        council.send(self.cp,'alpha','New message'); self.assertTrue(runtime.pending_operator(self.cp,self.agent('alpha')))
    def test_equity_samples_only_canonical_no_duplicate(self):
        trial={'id':'x','agent':'alpha:1','initial_usd':200,'state':'running','accounts':{'candidate_normal':{'cash':200,'units':0,'trades':0,'equity':200},'candidate_stress':{'cash':900,'units':0,'equity':900}}}
        runtime.advance_economy(self.ep,[trial],1700000000)
        trial['accounts']['candidate_normal'].update(cash=150,units=.5,equity=205,trades=1)
        with patch.object(runtime,'production_status',return_value={'trials':[trial]}):
            runtime.equity_point(self.paths,1700000000); runtime.equity_point(self.paths,1700000001)
        self.assertEqual(len(runtime.equity_history(self.cp)),1); self.assertEqual(runtime.equity_history(self.cp)[0]['marked'],100500)
        self.assertEqual(runtime.equity_history(self.cp)[0]['settled'],100000)
    def test_spread_filters_cost_and_stale(self):
        k={'XBTUSD':{'bid':100,'ask':101,'observed':100}}; c={'XBTUSD':{'bid':101.1,'ask':102,'observed':101}}
        out=mechanisms.spread_screen(k,c,102); self.assertEqual(len(out),2); self.assertGreater(out[0]['gross'],0); self.assertLess(out[0]['normal_net'],0)
        self.assertEqual(mechanisms.spread_screen(k,c,120),[])
    def test_hedge_cancels_directional_move_and_counts_funding(self):
        s=[Bar(3600,100,100,100,100,1),Bar(7200,100,110,100,110,1)]
        result=mechanisms.window(s,s,[[7200000,8,.01]],0,0)
        self.assertAlmostEqual(result['hedge_price_pnl_usd'],0); self.assertAlmostEqual(result['funding_usd'],.5); self.assertAlmostEqual(result['net_return'],.0025)
        self.assertFalse(result['forced_exit_proxy'])
        self.assertLess(mechanisms.window(s,s,[[7200000,8,.01]],40,20)['net_return'],result['net_return'])
    def test_margin_failure_and_alignment(self):
        s=[Bar(3600,100,100,100,100,1),Bar(7200,100,300,100,300,1)]
        result=mechanisms.window(s,s,[[7200000,8,.5]],0,0)
        self.assertTrue(result['forced_exit_proxy']); self.assertEqual(result['funding_events'],0)
        with self.assertRaises(ValueError): mechanisms.window(s,s[:1],[],40,20)
    def test_archive_checksum_and_full_month(self):
        a,b=funding.month_bounds('2025-01'); name='BTCUSDT-1h-2025-01'; buf=io.BytesIO()
        rows='\n'.join(','.join(map(str,[t*1000000,100,101,99,100,1,t*1000000+3599999999,1,1,1,1,0])) for t in range(a,b,3600))
        with ZipFile(buf,'w') as z:z.writestr(name+'.csv',rows)
        raw=buf.getvalue(); checksum=(hashlib.sha256(raw).hexdigest()+'  '+name+'.zip').encode()
        bars,sha=mechanisms.parse_bars(raw,checksum,'BTCUSDT','2025-01','spot'); self.assertEqual(len(bars),744);self.assertEqual(bars[0].ts,a)
        with self.assertRaises(ValueError):mechanisms.parse_bars(raw,b'0  bad','BTCUSDT','2025-01','spot')
    def test_archive_review_provenance_and_costs(self):
        a,_=funding.month_bounds('2025-01'); _,b=funding.month_bounds('2025-03'); events=[[t*1000,8,.0005] for t in range(a,b,28800)]
        dataset={'protocol':funding.PROTOCOL,'symbols':funding.SYMBOLS,'events':{p:events for p in funding.SYMBOLS}};dataset['events_sha256']=lab.digest(dataset['events'])
        def month(cache,symbol,month,market,fetcher):
            x,y=funding.month_bounds(month);return [Bar(t,100,101,99,100,1) for t in range(x,y,3600)],{'market':market,'month':month,'sha256':'fixture'}
        with patch.object(mechanisms,'get_month',side_effect=month): r=mechanisms.review(self.root,dataset,'XBTUSD',b-3600,3)
        self.assertEqual(r['months'],3); self.assertEqual(len(r['sources']),6);self.assertGreater(r['mean_normal'],r['mean_stress']);self.assertFalse(r['eligible'])
    def test_dashboard_controls_and_persistence(self):
        import socket, urllib.request, re, time
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        stop=threading.Event();t=threading.Thread(target=runtime.serve,args=(self.paths,port,stop),daemon=True);t.start()
        try:
            for _ in range(30):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/',timeout=1) as r: html=r.read().decode();break
                except OSError:time.sleep(.05)
            else:self.fail('No dashboard')
            self.assertIn('Live equity',html);self.assertIn('Flatten all paper trials',html)
            token=re.search(r'<script nonce="([^"]+)"',html).group(1)
            req=urllib.request.Request(f'http://127.0.0.1:{port}/api/control',data=json.dumps({'token':token,'action':'pause_autonomy'}).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=2) as r:self.assertTrue(json.load(r)['ok'])
            self.assertTrue(runtime.get_state(self.cp)['autonomy_paused'])
        finally:stop.set();t.join(3)

if __name__=='__main__':unittest.main()

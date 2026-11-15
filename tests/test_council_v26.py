import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from autarky_trade import council, economic_kernel as economy


class CouncilTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'council.sqlite'; self.econ=Path(self.temp.name)/'economy.sqlite'
        economy.init(self.econ)
        with closing(council.connect(self.path,True)): pass

    def agent(self,slot): return next(a for a in economy.status(self.econ)['agents'] if a['slot']==slot)

    @staticmethod
    def plan(action='talk',recipient='all',parameters=None):
        return {'action':action,'recipient':recipient,'message':'Test reply','rationale':'Test decision','parameters':parameters or {}}

    def test_addressing_peer_context_and_reasoning(self):
        council.send(self.path,'alpha','private to alpha'); council.send(self.path,'all','shared instruction')
        seen=[]
        def alpha(model,prompt):
            seen.append(json.loads(prompt)); return self.plan(recipient='beta'), 'local emitted reasoning'
        council.turn(self.path,self.econ,self.agent('alpha'),'qwen',{},provider=alpha)
        def beta(model,prompt):
            seen.append(json.loads(prompt)); return self.plan(recipient='operator'),None
        council.turn(self.path,self.econ,self.agent('beta'),'qwen',{},provider=beta)
        self.assertEqual([r['body'] for r in seen[0]['unread_operator_messages']],['private to alpha','shared instruction'])
        self.assertEqual([r['body'] for r in seen[1]['unread_operator_messages']],['shared instruction'])
        self.assertIn('Test reply',[r['body'] for r in seen[1]['recent_messages']]); self.assertNotIn('private to alpha',json.dumps(seen[1]))
        self.assertEqual(council.snapshot(self.path)['turns'][1]['reasoning'],'local emitted reasoning')
        self.assertEqual(economy.status(self.econ)['spent_cents'],0)

    def test_unread_batches_and_invalid_plan(self):
        for i in range(7): council.send(self.path,'alpha',f'message {i}')
        def bad(*args): return self.plan('carry_screen',parameters={'pair':'invalid'}),'bad reasoning'
        with self.assertRaises(ValueError): council.turn(self.path,self.econ,self.agent('alpha'),'qwen',{},provider=bad)
        seen=[]
        def good(model,prompt):
            seen.append(json.loads(prompt)); return self.plan(),None
        for _ in range(2): council.turn(self.path,self.econ,self.agent('alpha'),'qwen',{},provider=good)
        self.assertEqual([len(x['unread_operator_messages']) for x in seen],[4,3])
        self.assertEqual(council.snapshot(self.path)['turns'][-1]['source'],'ollama_rejected')

    def test_prepaid_and_dedup(self):
        p=self.plan('carry_screen',parameters={'pair':'XBTUSD'})
        def provider(*args): return p,None
        def work(plan):
            self.assertEqual(economy.status(self.econ)['spent_cents'],175)
            return {'mean_gross':.003,'development_only':True}
        r=council.turn(self.path,self.econ,self.agent('alpha'),'qwen',{'data_identity':'a'},work,True,provider)
        self.assertTrue(r['paid']); self.assertEqual(r['state'],'complete')
        r=council.turn(self.path,self.econ,self.agent('beta'),'qwen',{'data_identity':'a'},work,True,provider)
        self.assertTrue(r['duplicate']); self.assertEqual(economy.status(self.econ)['spent_cents'],175)
        self.assertTrue(economy.status(self.econ)['reconciled'])

    def test_paid_disabled_and_failure(self):
        p=self.plan('carry_screen',parameters={'pair':'ETHUSD'})
        def provider(*args): return p,None
        def work(plan): raise RuntimeError('test failure')
        self.assertFalse(council.turn(self.path,self.econ,self.agent('alpha'),'qwen',{},work,False,provider)['paid'])
        self.assertEqual(economy.status(self.econ)['spent_cents'],0)
        r=council.turn(self.path,self.econ,self.agent('alpha'),'qwen',{},work,True,provider)
        self.assertEqual(r['state'],'failed'); self.assertTrue(r['paid']); self.assertEqual(economy.status(self.econ)['spent_cents'],175)

    def test_recovery_and_html_escaping(self):
        with closing(council.connect(self.path)) as c,c:
            c.execute("INSERT INTO council_experiments VALUES('key','alpha:1',1,'council:key','running','{}')")
            council.message(c,'operator','all','operator','<script>alert(1)</script>')
            council.recover_experiments(c,self.econ)
        d=council.snapshot(self.path); self.assertEqual(d['experiments'][0]['state'],'interrupted')
        page=council.render(d,'token'); self.assertNotIn('<script>',page); self.assertIn('&lt;script&gt;',page)

    def test_directional_arbitrary_supported_spec(self):
        p=self.plan('directional',parameters={'pair':'ETHUSD','horizon':72,'spec':{'features':[['momentum',24],['other_volatility',168]],'method':'robust','transform':'absolute','calibration':'conservative'}})
        v=council.validate_plan(p); self.assertEqual(v['parameters']['horizon'],72); self.assertEqual(v['parameters']['spec']['method'],'robust')
        p['parameters']['spec']['features']=[['arbitrary_code',24]]
        with self.assertRaises(ValueError): council.validate_plan(p)

    def test_carry_screen_complete_month(self):
        from autarky_trade import funding_research as f, lab
        start,end=f.month_bounds('2021-01'); events=[[t*1000,8,.0001] for t in range(start,end,28800)]
        dataset={'protocol':f.PROTOCOL,'symbols':f.SYMBOLS,'events':{p:events for p in f.SYMBOLS}}
        dataset['events_sha256']=lab.digest(dataset['events']); r=council.carry_screen(dataset,'XBTUSD',end-3600)
        self.assertEqual(r['months'],1); self.assertAlmostEqual(r['mean_gross'],len(events)*.0001); self.assertFalse(r['eligible'])
        dataset['events']['XBTUSD']=events[3:]; dataset['events_sha256']=lab.digest(dataset['events'])
        with self.assertRaises(ValueError): council.carry_screen(dataset,'XBTUSD',end-3600)

    def test_real_directional_evaluation(self):
        from autarky_trade.core import synthetic
        bars=synthetic(721+720*14); parts={'XBTUSD':[bars],'ETHUSD':[bars]}
        p=self.plan('directional',parameters={'pair':'ETHUSD','horizon':168,'spec':{'features':[['momentum',168],['other_volatility',168]],'method':'ridge','transform':'raw','calibration':'standard'}})
        result=council.research(council.validate_plan(p),parts,None,bars[-1].ts)
        self.assertEqual(set(result['summary']),{'mean','parent','candidate'})
        self.assertEqual(result['windows'],2); self.assertFalse(result['eligible'])

if __name__=='__main__': unittest.main()

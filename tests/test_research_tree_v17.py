import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from autarky_trade import research_tree as t, autonomous_research as old, weekly_models as wm
from autarky_trade.core import Bar, synthetic


class TreeTests(unittest.TestCase):
    def test_spec_identity_and_one_dimension(self):
        p=t.roots()['risk_long']; s=t.Spec(tuple(reversed(p.features)),p.method)
        self.assertEqual(p.identity,s.identity)
        v={'parent':'risk_long','spec':{'features':list(p.features),'method':'logistic_downside','transform':'raw','calibration':'standard'},'hypothesis':'Does downside probability help?'}
        self.assertEqual(t.validate_proposal(v,{'risk_long':p},set())[1].method,'logistic_downside')
        with self.assertRaises(ValueError): t.validate_proposal(v,{'risk_long':p},{t.Spec(p.features,'logistic_downside').identity})
        v['spec']['calibration']='conservative'
        with self.assertRaises(ValueError): t.validate_proposal(v,{'risk_long':p},set())
        with self.assertRaises(ValueError): t.Spec((('forecast',24),))
        with self.assertRaises(ValueError): t.Spec((('momentum',True),))

    def test_logistic_classifiers_and_chronology(self):
        keys=(('momentum',24),)
        rows=[{'features':{keys[0]:float(i%2)},'realized':.1 if i%2 else -.1} for i in range(40)]
        for method in ('logistic_up','logistic_downside'):
            s=t.Spec(keys,method); f=t.fit(s,rows)
            self.assertGreater(f({keys[0]:1.}),f({keys[0]:0.}))
            self.assertAlmostEqual(t.fit(s,rows[1::2])({keys[0]:0.}),.1)
        b=synthetic(721+720*9); cutoff=b[-1].ts; p=t.roots()['risk_long']; s=t.Spec(p.features,'logistic_downside')
        a=t.evaluate([b],'XBTUSD',cutoff,p,s,now=cutoff+3600)
        i=721+720*8
        c=b[:i]+[Bar(x.ts,x.open*2,x.high*2,x.low*2,x.close*2,x.volume) for x in b[i:]]
        z=t.evaluate([c],'XBTUSD',cutoff,p,s,now=cutoff+3600)
        self.assertEqual(a['details'][:2],z['details'][:2])
        self.assertFalse(a['eligible'])

    def test_calibration_and_facts(self):
        rows=[{'prediction':.04,'realized':.1}]*24
        self.assertIsNotNone(t.calibrate(t.Spec((('momentum',24),)),rows))
        self.assertIsNone(t.calibrate(t.Spec((('momentum',24),),calibration='conservative'),rows))
        base={'mean_net':-.02,'mean_stress':-.03,'rmse':.1,'fills':10,'worst_drawdown':.03,'windows':10,'active_windows':5}
        r={'summary':{'mean':base,'parent':base,'candidate':dict(base,mean_stress=-.01)},'post_2017':{}}
        f=t.facts({'XBTUSD':r,'ETHUSD':r})
        self.assertEqual(f['classification'],'improves_both_but_not_profitable')
        self.assertFalse(f['eligible'])

    def test_persistent_child_and_resume(self):
        b=synthetic(721+720*9); parts={'XBTUSD':[b],'ETHUSD':[b]}; cutoff=b[-1].ts
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'tree.sqlite'; events=[]
            a=t.run(path,parts,cutoff,model=None,emit=events.append)
            self.assertEqual(a['completed'],3)
            first=t.status(path)['nodes'][-1]
            target='node:'+str(first['id'])
            def choose_child(url,payload):
                prompt=payload['messages'][0]['content']
                options=json.loads(prompt.split('Options: ',1)[1].split(' Measured parent evidence:',1)[0])
                choice=next((x['choice'] for x in options if x['parent']==target),0)
                return {'message':{'content':json.dumps({'choice':choice,'hypothesis':'Does this change help?'})}}
            with patch.object(t,'request',side_effect=choose_child):
                a=t.run(path,parts,cutoff,model='qwen3:4b',emit=events.append)
            self.assertEqual(a['completed'],3); self.assertEqual(a['rejected'],0)
            data=t.status(path); self.assertEqual(len(data['nodes']),6)
            self.assertTrue(any(n['parent']==target for n in data['nodes']))
            self.assertTrue(all(n['proposal_source']=='ollama' for n in data['nodes'][:3]))
            self.assertFalse(data['nodes'][0]['findings']['eligible'])
            data['nodes'][0]['hypothesis_unverified']='<script>bad</script>'
            self.assertNotIn('<script>bad</script>',t.render(data))

    def test_dynamic_menu_and_invalid_choice_fallback(self):
        available=t.roots(); seen={s.identity for s in available.values()}
        for agent in old.AGENTS:
            for _ in range(5):
                menu=t.candidate_menu(agent,available,seen,{})
                self.assertTrue(menu)
                self.assertEqual(len({s.identity for _,s,_ in menu}),len(menu))
                for parent,s,field in menu:
                    self.assertNotIn(s.identity,seen)
                    self.assertEqual([k for k in ('features','method','transform','calibration') if getattr(s,k)!=getattr(available[parent],k)],[field])
                parent,s,_,source=t.propose(None,agent,available,seen,{},lambda _:None)
                self.assertEqual(source,'deterministic'); seen.add(s.identity)
        errors=[]
        with patch.object(t,'request',return_value={'message':{'content':'{"choice":999,"hypothesis":"bad"}'}}):
            parent,s,h,source=t.propose('qwen3:4b','gamma',available,seen,{},errors.append)
        self.assertEqual(source,'deterministic_fallback'); self.assertEqual(len(errors),2)
        self.assertNotIn(s.identity,seen)

    def test_legacy_and_parent_equivalence(self):
        b=synthetic(721+720*9); cutoff=b[-1].ts; parts={'XBTUSD':[b],'ETHUSD':[b]}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'legacy.sqlite'; old.run(path,parts,cutoff,model=None,emit=lambda _:None)
            hashes={p:old.fingerprint(parts[p],p) for p in old.PAIRS}
            evidence=t.legacy_evidence(path,hashes,cutoff)
            self.assertIn('momentum_only',evidence)
            with self.assertRaises(ValueError): t.legacy_evidence(path,hashes,cutoff-3600)
            p=t.roots()['ridge_four']; s=t.Spec(p.features,'shrink')
            r=t.evaluate([b],'XBTUSD',cutoff,p,s,now=cutoff+3600)
            orig=wm.review([b],'XBTUSD',cutoff,now=cutoff+3600)
            self.assertEqual(r['summary']['mean'],orig['summary']['mean'])
            self.assertAlmostEqual(r['summary']['parent']['rmse'],orig['summary']['ridge_four']['rmse'])

if __name__=='__main__': unittest.main()

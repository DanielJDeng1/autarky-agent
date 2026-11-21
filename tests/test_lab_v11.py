import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from autarky_trade.core import Bar, Costs, synthetic
from autarky_trade import lab, community, archive


class LabTests(unittest.TestCase):
    def setUp(self):
        self.bars=synthetic(2500)
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=str(Path(self.tmp.name)/'lab.sqlite')
        self.policy=lab.Policy('test',(lab.Rule('momentum',2,'gt',-1),),(lab.Rule('momentum',2,'lt',5),),3,2,10)

    def test_rules_and_identity(self):
        self.assertEqual(self.policy.identity,replace(self.policy,name='different',hypothesis='different').identity)
        self.assertNotEqual(self.policy.identity,replace(self.policy,cooldown=5).identity)
        with self.assertRaises(ValueError): lab.Rule('volume_ratio',2,'gt',float('nan'))
        with self.assertRaises(ValueError): replace(self.policy,entry=())
        with self.assertRaises(ValueError): replace(self.policy,entry=(lab.Rule('forecast',24,'gt',0),))
        self.assertEqual(self.policy.description()['entry_all'],['momentum(2) > -1'])

    def test_features_prior_only(self):
        f=lab.Features(self.bars); i=800
        changed=list(self.bars); changed[i]=replace(changed[i],close=changed[i].close*2,high=changed[i].high*2)
        g=lab.Features(changed)
        for feature in lab.FEATURES:
            if feature=='forecast': continue
            self.assertEqual(f.series(feature,24)[i],g.series(feature,24)[i])
        self.assertAlmostEqual(f.series('breakout',24)[i],self.bars[i-1].close/max(b.high for b in self.bars[i-25:i-1])-1)

    def test_hold_and_cooldown(self):
        r=lab.simulate(self.bars,self.policy,721,733,Costs(0,0))
        # Entries 721,727; exits 724,730; second cooldown ends at 733.
        self.assertEqual(r['fills'],4)
        self.assertEqual(r['round_trips'],2)
        self.assertEqual(r['reasons']['signal_exit'],2)
        self.assertEqual(r['cooldown_blocked'],4)
        self.assertAlmostEqual(r['exposure_fraction'],.5)

    def test_cost_drag_same_path(self):
        r=lab.simulate(self.bars,self.policy,721,900)
        self.assertGreater(r['cost_drag_return'],0)
        self.assertGreater(r['fees'],0)
        self.assertGreater(r['execution_friction'],0)
        self.assertAlmostEqual(r['same_trade_path_gross_return']-r['net_return'],r['cost_drag_return'])

    def test_stop_overrides_hold(self):
        bars=[Bar(1700000000+i*3600,100,100,100,100,1) for i in range(900)]
        bars[722]=Bar(bars[722].ts,40,40,40,40,1)
        p=replace(self.policy,min_hold=100,max_hold=200)
        r=lab.simulate(bars,p,721,730,Costs(0,0))
        self.assertTrue(r['stopped']); self.assertEqual(r['reasons']['risk'],1)
        self.assertEqual(r['fills'],2)

    def test_forecast_boundary(self):
        spec={'horizon':24,'window':72,'ridge':1.}
        f=lab.Features(self.bars); pred,a=lab.forecast_fold(self.bars,f,spec,1000,1100)
        self.assertEqual(a['last_label_end'],self.bars[999].ts)
        bars=list(self.bars); bars[1000]=replace(bars[1000],open=bars[1000].open*2,high=bars[1000].high*2)
        p,b=lab.forecast_fold(bars,lab.Features(bars),spec,1000,1100)
        self.assertEqual(a,b); self.assertEqual(pred[1000],p[1000])

    def test_repeated_development_not_promotion(self):
        end=self.bars[-1].ts
        a=lab.run(self.db,self.bars,'XBTUSD',end,[self.policy],folds=3)
        b=lab.run(self.db,self.bars,'XBTUSD',end,[self.policy],folds=3)
        self.assertFalse(a['eligible']); self.assertTrue(a['development_only'])
        self.assertEqual(a['dataset_sha256'],b['dataset_sha256']); self.assertEqual(b['prior_dataset_experiments'],1)
        self.assertEqual(a['results'],b['results'])
        with closing(sqlite3.connect(self.db)) as c: self.assertEqual(c.execute('SELECT count(*) FROM experiments').fetchone()[0],2)

    def test_cutoff_and_archive_read_only(self):
        source=str(Path(self.tmp.name)/'community.sqlite'); community.init(source)
        archive.ingest(source,self.bars,'csv')
        cutoff=self.bars[1800].ts
        pair,bars=lab.history(source,cutoff)
        self.assertEqual(pair,'XBTUSD'); self.assertEqual(bars[-1].ts,cutoff)
        lab.run(self.db,bars,pair,cutoff,[self.policy],folds=3)
        self.assertEqual(community.config(source)['last_holdout'],0)
        with self.assertRaises(ValueError): lab.run(self.db,self.bars,pair,cutoff,[self.policy],folds=3)

    def test_default_policies_and_zero_activity(self):
        r=lab.run(self.db,self.bars,'XBTUSD',self.bars[-1].ts,lab.defaults(),folds=3)
        self.assertEqual(len(r['results']),7)
        p=next(x for x in r['results'] if x['policy']['name']=='ridge-24-cost-aware')
        self.assertEqual(p['windows'][0]['model_artifact']['horizon'],24)
        self.assertLess(p['windows'][0]['model_artifact']['last_label_end'],p['windows'][0]['start'])
        z=replace(self.policy,entry=(lab.Rule('momentum',2,'gt',5),))
        r=lab.run(self.db,self.bars,'XBTUSD',self.bars[-1].ts,[z],folds=3)
        self.assertEqual(r['results'][0]['diagnosis'],'no trades')

    def test_forecast_diagnostics_and_matched_ablation(self):
        report=lab.run(self.db,self.bars,'XBTUSD',self.bars[-1].ts,lab.defaults(),folds=3)
        pair=report['comparisons'][0]
        self.assertEqual(pair['change'],'entry forecast threshold only: 0 to 0.012')
        p=next(r for r in report['results'] if r['policy']['name']=='ridge-24-cost-aware')
        for w in p['windows']:
            self.assertGreater(w['forecast_accuracy']['observations'],0)
            self.assertGreaterEqual(w['forecast_accuracy']['zero_forecast_rmse'],0)
            self.assertIn(w['market_regime']['direction'],('up','down','sideways'))
        self.assertAlmostEqual(p['mean_gross']-p['mean_net'],p['mean_cost_drag'])

    def test_ollama_structured_retry(self):
        obj={'policies':[{'name':'new','entry':[{'feature':'momentum','window':24,'op':'gt','threshold':.01}], 'exit':[{'feature':'momentum','window':24,'op':'lt','threshold':0}], 'min_hold':6,'cooldown':6,'max_hold':72,'fraction':.25,'hypothesis':'test'}]}
        with patch('autarky_trade.lab.request',side_effect=[{'message':{'content':''}},{'message':{'content':json.dumps(obj)}}]) as req:
            p=lab.propose('qwen3:4b',self.bars)
        self.assertEqual(p[0].entry[0].window,24)
        self.assertFalse(req.call_args.args[1]['think'])
        self.assertEqual(req.call_count,2)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        from autarky_trade import lab_paper
        self.paper=lab_paper; self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=str(Path(self.tmp.name)/'lab.sqlite'); self.forward=str(Path(self.tmp.name)/'forward.sqlite')
        self.bars=synthetic(2600); self.dev=self.bars[:2000]; self.now=self.dev[-1].ts+3600
        self.p=lab.Policy('always-entry',(lab.Rule('momentum',2,'gt',-1),),(lab.Rule('momentum',2,'lt',5),),3,2,10)
        lab.run(self.db,self.dev,'XBTUSD',self.dev[-1].ts,[self.p],folds=3,now=self.now)
        self.paper.init(self.forward,self.db,1,self.p.name,now=self.now)

    def tick(self,i,bid=None,offset=0):
        b=self.bars[i]; bid=b.close if bid is None else bid
        return self.paper.tick(self.forward,self.bars[i-500:i+1],bid,bid,b.ts+3600+offset)

    def test_forward_only_and_duplicate(self):
        self.assertEqual(self.tick(1999)['fills'],0)
        self.assertEqual(self.tick(2000)['reason'],'entry')
        self.assertEqual(self.tick(2000,offset=30)['fills'],1)
        self.assertEqual(self.tick(2001)['fills'],1)
        self.assertEqual(self.tick(2002)['fills'],1)
        self.assertEqual(self.tick(2003)['reason'],'signal exit')
        self.assertEqual(self.tick(2004)['fills'],2)
        self.assertEqual(self.tick(2005)['fills'],2)
        self.assertEqual(self.tick(2006)['fills'],3)
        self.assertFalse(self.paper.status(self.forward)['eligible'])
        with self.assertRaises(ValueError): self.paper.init(self.forward,self.db,1,self.p.name,now=self.now)

    def test_intrabar_risk_and_stop(self):
        self.tick(2000)
        r=self.tick(2000,bid=self.bars[2000].close*.4,offset=30)
        self.assertTrue(r['stopped']); self.assertEqual(r['fills'],2)
        self.assertEqual(self.paper.status(self.forward)['account']['units'],0)
        self.assertEqual(self.tick(2001)['fills'],2)

    def test_operator_stop(self):
        self.tick(2000); self.paper.stop(self.forward)
        self.assertEqual(self.tick(2000,offset=30)['reason'],'risk/operator stop')
        self.assertEqual(self.paper.status(self.forward)['account']['units'],0)

    def test_gap_no_replay(self):
        self.tick(2000); r=self.tick(2015)
        self.assertEqual(r['reason'],'max hold'); self.assertEqual(r['fills'],2)

    def test_csv_gap(self):
        import csv
        path=Path(self.tmp.name)/'history.csv'
        with path.open('w',newline='') as f:
            w=csv.writer(f); w.writerow(['timestamp','open','high','low','close','volume'])
            for b in self.bars[:100]+self.bars[101:]: w.writerow([b.ts,b.open,b.high,b.low,b.close,b.volume])
        b=lab.csv_history(path,self.bars[2300].ts)
        self.assertEqual(b[0].ts,self.bars[101].ts); self.assertEqual(b[-1].ts,self.bars[2300].ts)

if __name__=="__main__": unittest.main()

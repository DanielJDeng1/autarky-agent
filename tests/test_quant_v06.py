import json
import math
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from autarky_trade import community as m, jobs, research, resources
from autarky_trade.core import Strategy, Costs, Bar, synthetic, backtest, backtest_path
from autarky_trade.models import fit, predict, rolling_validation
from autarky_trade.evidence import block_interval, describe


class QuantTests(unittest.TestCase):
    def setUp(self):
        self.bars=synthetic(); self.temp=tempfile.TemporaryDirectory(); self.path=str(Path(self.temp.name)/'lab.sqlite'); m.init(self.path)
    def tearDown(self): self.temp.cleanup()
    def model(self, bars=None):
        artifact=fit((bars or self.bars)[:600],20,.1)
        return Strategy('ridge',2,20,'model',model=artifact)
    def test_fit_ignores_holdout_and_reproduces(self):
        first=self.model(); changed=self.bars[:600]+[replace(b,open=b.open*10,close=b.close*10) for b in self.bars[600:]]
        self.assertEqual(first,self.model(changed))
        self.assertEqual(first.model['fit_end'],self.bars[599].ts)
        self.assertEqual(first.model['samples'],578)
        self.assertEqual(Strategy(**json.loads(json.dumps(asdict(first)))),first)
    def test_reject_forged_or_future_fit(self):
        s=self.model(); artifact=dict(s.model); artifact['intercept']+=.1
        with self.assertRaises(ValueError): research.assess(self.bars,[replace(s,model=artifact)],800)
        with self.assertRaises(ValueError): research.assess(self.bars,[replace(s,model=fit(self.bars[:601],20,.1))],800)
    def test_model_identity_changes_after_refit(self):
        a=self.model(); b=replace(a,model=fit(self.bars[:601],20,.1))
        self.assertNotEqual(research.identity(a),research.identity(b))
        self.assertFalse(m.same_strategy(m.encode(asdict(a)),m.encode(asdict(b))))
        self.assertTrue(m.same_strategy(m.encode(asdict(a)),m.encode(asdict(replace(a,name='renamed')))))
    def test_model_proposal_is_bounded(self):
        payload={'strategies':[],'predictors':[{'window':10,'ridge':.5,'threshold':.001},{'window':True,'ridge':.5,'threshold':0},{'window':5,'ridge':-1,'threshold':0}]}
        with patch.object(m,'request',return_value={'message':{'content':json.dumps(payload)}}):
            candidates=m.candidates('trend','qwen',self.bars[:600],'alpha',[])
        self.assertEqual(sum(s.kind=='ridge' for s in candidates),2)
        self.assertTrue(all(s.model['fit_end']==self.bars[599].ts for s in candidates if s.model))
    def test_equity_path_reconciles_to_return(self):
        s=self.model(); metrics,log_returns=backtest_path(self.bars,s,Costs(),800,1000)
        self.assertEqual(metrics,backtest(self.bars,s,Costs(),800,1000))
        self.assertAlmostEqual(math.expm1(sum(log_returns)),metrics['return'])
        self.assertEqual(len(log_returns),200)
    def test_bootstrap_constant_and_reproducibility(self):
        self.assertTrue(all(abs(v-.01)<1e-12 for v in block_interval([.01]*200,24)))
        r=describe(self.bars,self.model(),Costs(),800,1000)
        self.assertEqual(r,describe(self.bars,self.model(),Costs(),800,1000))
        self.assertEqual(r['observations'],200)
    def test_renew_cannot_resurrect_lease(self):
        now=self.bars[-1].ts+3600; jobs.schedule(self.path,self.bars,'csv',now)
        j=jobs.claim(self.path,'alpha',now); self.assertTrue(jobs.renew(self.path,j,now+100))
        self.assertIsNone(jobs.claim(self.path,'alpha',now+jobs.LEASE))
        self.assertFalse(jobs.renew(self.path,j,now+100+jobs.LEASE))
        k=jobs.claim(self.path,'alpha',now+100+jobs.LEASE); self.assertNotEqual(k['token'],j['token'])
        self.assertFalse(jobs.renew(self.path,j,now+100+jobs.LEASE))
    def test_usage_and_expense_not_paper_profit(self):
        now=self.bars[-1].ts+3600; jobs.schedule(self.path,self.bars,'csv',now)
        self.assertEqual(jobs.work(self.path,'alpha')['status'],'completed')
        resources.expense(self.path,'bill-1','2.50','electricity'); resources.expense(self.path,'bill-1','2.50','electricity')
        r=resources.report(self.path); self.assertEqual(r['actual_expenses_usd'],2.5); self.assertEqual(r['real_trading_revenue_usd'],0); self.assertEqual(r['simulated_trading_pnl_usd'],0); self.assertEqual(r['research_usage'][0]['attempts'],1)
        with self.assertRaises(ValueError): resources.expense(self.path,'bill-1','3.00','electricity')
        with self.assertRaises(ValueError): resources.expense(self.path,'x','0.001','electricity')
    def test_rolling_refit_has_strict_temporal_boundary(self):
        a=self.model().model
        folds=rolling_validation(self.bars,a,[600,660,720,800],Costs())
        for f in folds: self.assertLess(f['fit_end'],f['start'])
        modified=self.bars[:660]+[replace(b,open=b.open*2,close=b.close*2) for b in self.bars[660:]]
        changed=rolling_validation(modified,a,[600,660,720,800],Costs())
        self.assertEqual(folds[0],changed[0])
        self.assertEqual(folds[1]['train_sha256'],changed[1]['train_sha256'])
    def test_open_gap_risk_stop_uses_observed_open(self):
        class Always:
            def signal(self, closes): return True
        bars=[Bar(0,100,100,100,100,1),Bar(3600,1,100,1,100,1)]
        result=backtest(bars,Always(),Costs(0,0),0,2)
        self.assertTrue(result['stopped']); self.assertAlmostEqual(result['return'],-.2475)
    def test_odd_length_synthetic_demo(self):
        result=m.research(self.path,synthetic(603),'synthetic'); self.assertEqual(result['status'],'synthetic demonstration only')

if __name__=='__main__': unittest.main()

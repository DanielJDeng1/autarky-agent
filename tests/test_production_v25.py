import json
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from autarky_trade import production as p, economic_kernel as e, research_tree as tree, cross_asset as cross, horizon_review as hr, weekly_models as wm, lab
from autarky_trade.core import Bar, Account
from dataclasses import asdict


def market(n,start=1700002800):
    rows=[]; price=100.
    for i in range(n):
        end=price*1.0002
        rows.append(Bar(start+i*3600,price,end,price,end,100.)); price=end
    return rows


def snapshot(capital=200):
    return dict(accounts={k:asdict(Account(cash=capital,peak=capital)) for k in ('candidate_normal','candidate_stress','mean_normal','mean_stress')},exit_at={},cursor=0,missed=0,last_observed=0,last_bid=None,max_quote_gap=0,late_exits=0,operator_stop=False)


class ProductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bars=market(721+720*25)
        cls.parts={'XBTUSD':[bars],'ETHUSD':[[Bar(b.ts,b.open*2,b.high*2,b.low*2,b.close*2,b.volume) for b in bars]]}
        cls.cutoff=bars[-1].ts; cls.now=cls.cutoff+3600; cls.start=(cls.now//3600+2)*3600
        cls.spec=tree.Spec((('momentum',24),('other_momentum',24)))
        cls.parent=tree.Spec((('momentum',24),))
        cls.dataset=lab.digest([cross.PROTOCOL,cls.cutoff,{q:tree.old.fingerprint(cls.parts[q],q) for q in p.PAIRS},{q:tree.old.fingerprint(cls.parts[q],q) for q in p.PAIRS}])
        node=dict(id=1,dataset=cls.dataset,agent='alpha',parent='ridge_trend_only',spec=asdict(cls.spec),parent_spec=asdict(cls.parent))
        cls.review=json.loads(json.dumps(hr.review([node],cls.parts,cls.cutoff,horizons=(168,),now=cls.now)))
        cls.plan=p.freeze_artifact(cls.review,cls.parts,cls.cutoff,1,'XBTUSD',168,'alpha:1',20000,cls.start,cls.start+90*86400,cls.now)

    def bars(self, decision):
        b=market(719,decision-719*3600)
        return {'XBTUSD':b,'ETHUSD':[Bar(x.ts,x.open*2,x.high*2,x.low*2,x.close*2,x.volume) for x in b]}

    def test_freeze_train_calibration_and_future_boundary(self):
        self.assertEqual(len(self.plan['training']),100)
        self.assertTrue(all(x['label_end']<=self.cutoff for x in self.plan['training']))
        self.assertEqual(len(self.plan['calibration']['candidate']),52)
        self.assertIsNotNone(self.plan['thresholds']['candidate'])
        self.assertEqual(len(p.schedule(self.start,self.plan['end'],168)),12)
        self.assertEqual(p.schedule(self.start,self.plan['end'],168)[4],self.start+p.WINDOW)
        with self.assertRaises(ValueError): p.freeze_artifact(self.review,self.parts,self.cutoff,1,'XBTUSD',168,'alpha:1',20000,self.now,self.start+90*86400,self.now)

    def test_entry_exit_at_observed_quote_and_costs(self):
        s,ev=p.step(self.plan,snapshot(),self.start+20,100,101,self.bars(self.start))
        self.assertEqual(len(ev['fills']),4)
        self.assertEqual(s['cursor'],1)
        self.assertLess(s['accounts']['candidate_stress']['units'],s['accounts']['candidate_normal']['units'])
        due=self.start+168*3600
        s,ev=p.step(self.plan,s,due+300,110,111)
        self.assertEqual(len(ev['fills']),4)
        self.assertEqual(s['cursor'],2)
        self.assertEqual(s['missed'],1)
        self.assertEqual(s['late_exits'],4)
        self.assertGreater(s['accounts']['candidate_normal']['cash'],200)
        self.assertTrue(all(a['units']==0 for a in s['accounts'].values()))

    def test_missing_candles_no_backfill_and_risk_stop(self):
        s,ev=p.step(self.plan,snapshot(),self.start+10,100,101,self.bars(self.start+3600))
        self.assertIn('data_error',ev); self.assertEqual(s['cursor'],0)
        s,ev=p.step(self.plan,s,self.start+121,100,101,self.bars(self.start))
        self.assertEqual(s['missed'],1); self.assertEqual(ev['fills'],[])
        due=self.start+168*3600
        s,ev=p.step(self.plan,s,due+1,100,101,self.bars(due))
        self.assertEqual(len(ev['fills']),4)
        s,ev=p.step(self.plan,s,due+2,50,51)
        self.assertTrue(all(a['stopped'] and not a['units'] for a in s['accounts'].values()))
        self.assertEqual(len(ev['fills']),4)

    def test_frozen_cash_gate(self):
        plan=dict(self.plan,thresholds={'candidate':None,'mean':None})
        s,ev=p.step(plan,snapshot(),self.start,100,101,self.bars(self.start))
        self.assertEqual(ev['fills'],[]); self.assertEqual(s['cursor'],1)
        self.assertEqual(s['accounts']['candidate_normal']['cash'],200)

    def test_persistent_ownership_stop_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            econ=Path(d)/'economy.sqlite'; db=Path(d)/'production.sqlite'
            e.init(econ); before=e.status(econ)
            ident=p.create(db,self.plan,econ)
            self.assertEqual(p.owner(econ,'alpha:1'),20000)
            with self.assertRaises(sqlite3.IntegrityError): p.create(db,self.plan,econ)
            p.observe(db,ident,self.bars(self.start),100,101,self.start+1)
            self.assertTrue(p.observe(db,ident,self.bars(self.start),100,101,self.start+1)['duplicate'])
            p.stop(db,ident)
            r=p.observe(db,ident,None,110,111,self.start+2)
            self.assertEqual(r['state'],'stopped')
            self.assertEqual(len(r['fills']),4)
            self.assertEqual(e.status(econ),before)
            with sqlite3.connect(db) as c:
                c.execute('UPDATE production_trials SET plan=?',(p.pack(dict(self.plan,initial_cents=100)),))
            with self.assertRaisesRegex(ValueError,'Frozen plan changed'): p.status(db)

    def test_retired_generation_cannot_continue(self):
        with tempfile.TemporaryDirectory() as d:
            econ=Path(d)/'economy.sqlite'; db=Path(d)/'production.sqlite'; e.init(econ)
            ident=p.create(db,self.plan,econ)
            p.observe(db,ident,self.bars(self.start),100,101,self.start+1)
            with closing(e.connect(econ)) as c,c: c.execute("UPDATE economy_agents SET state='retired' WHERE id='alpha:1'")
            r=p.observe(db,ident,None,100,101,self.start+2)
            self.assertEqual(r['state'],'stopped'); self.assertEqual(len(r['fills']),4)

if __name__=='__main__': unittest.main()

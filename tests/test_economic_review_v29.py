import copy
import json
import unittest
from dataclasses import asdict
from autarky_trade import economic_review as er, horizon_review as hr, research_tree as tree, cross_asset as cross, autonomous_research as old, lab
from autarky_trade.core import Account, Bar, Costs


class EconomicReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bars=[]; price=100.
        for i in range(lab.WARMUP+720*25):
            end=price*1.0002
            bars.append(Bar(1700002800+i*3600,price,end,price,end,100.))
            price=end
        cls.parts={'XBTUSD':[bars], 'ETHUSD':[[Bar(b.ts,b.open*2,b.high*2,b.low*2,b.close*2,b.volume) for b in bars]]}
        cls.cutoff=bars[-1].ts
        hashes={p:old.fingerprint(cls.parts[p],p) for p in er.PAIRS}
        dataset=lab.digest([cross.PROTOCOL,cls.cutoff,hashes,hashes])
        node={'id':6,'agent':'alpha','parent':'trend','dataset':dataset,
              'spec':asdict(tree.Spec((('momentum',24),('other_momentum',24)))),
              'parent_spec':asdict(tree.Spec((('momentum',24),)))}
        cls.report=json.loads(json.dumps(hr.review([node],cls.parts,cls.cutoff,horizons=(336,),now=cls.cutoff+3600)))

    def test_break_even_matches_account(self):
        for f,s in er.SCENARIOS.values():
            a=Account(); costs=Costs(f,s,.25)
            a.fill(True,100,100,costs)
            a.fill(False,100*(1+er.breakeven(f,s)),100*(1+er.breakeven(f,s)),costs)
            self.assertAlmostEqual(a.cash,1000.)

    def test_economics(self):
        x=er.economics(.01,200,.25)
        self.assertAlmostEqual(x['annual_simple_trading_usd'],200*.01*365/30)
        self.assertAlmostEqual(x['annual_operating_usd'],91.25)
        self.assertLess(x['annual_simple_surplus_usd'],0)
        self.assertAlmostEqual(x['required_annual_return'],.45625)
        self.assertIsNone(er.economics(0,200,.25)['required_capital_usd_at_observed_return'])
        with self.assertRaises(ValueError): er.economics(.01,0,.25)

    def test_real_evaluator_replay(self):
        r=er.replay(self.report,self.parts)
        self.assertFalse(r['eligible'])
        self.assertEqual(len(r['results']),2)
        for row in r['results']:
            m=row['scenarios']; saved=self.report['results'][0]['reports'][row['pair']]['summary']['candidate']
            self.assertGreater(m['legacy_normal']['candidate']['fills'],0)
            self.assertAlmostEqual(m['legacy_normal']['candidate']['mean_30d_net'],saved['mean_net'])
            self.assertAlmostEqual(m['legacy_stress']['candidate']['mean_30d_net'],saved['mean_stress'])
            self.assertLess(m['kraken_taker']['candidate']['mean_30d_net'],m['legacy_normal']['candidate']['mean_30d_net'])
            self.assertGreater(m['okx_us_proxy']['candidate']['mean_30d_net'],m['kraken_taker']['candidate']['mean_30d_net'])

    def test_funding_screen(self):
        from autarky_trade import funding_research as f
        start,_=f.month_bounds('2025-07'); _,end=f.month_bounds('2026-06')
        rows=[[t*1000,8,.0001] for t in range(start,end,8*3600)]
        dataset={'protocol':f.PROTOCOL,'symbols':f.SYMBOLS,'events':{p:rows for p in er.PAIRS}}
        dataset['events_sha256']=lab.digest(dataset['events'])
        result=er.funding_screen(dataset,end-3600)
        self.assertEqual(result['months'][0],'2025-07')
        self.assertEqual(result['months'][-1],'2026-06')
        self.assertAlmostEqual(result['results'][0]['gross_12m_usd_at_fixed_notional'],len(rows)*.0001*50)
        self.assertLess(result['results'][0]['gross_minus_operating_usd'],0)
        dataset['events']['XBTUSD']=rows[10:];dataset['events_sha256']=lab.digest(dataset['events'])
        with self.assertRaisesRegex(ValueError,'Incomplete funding coverage'):er.funding_screen(dataset,end-3600)

    def test_reject_changed_data(self):
        r=copy.deepcopy(self.report);r['datasets']['XBTUSD']='fake'
        with self.assertRaisesRegex(ValueError,'dataset'):er.replay(r,self.parts)

    def test_reject_changed_label(self):
        r=copy.deepcopy(self.report);r['results'][0]['reports']['XBTUSD']['details'][0]['models']['candidate']['samples'][0]['realized']+=.1
        with self.assertRaisesRegex(ValueError,'sample'):er.replay(r,self.parts)

    def test_reject_future_training(self):
        r=copy.deepcopy(self.report);w=r['results'][0]['reports']['XBTUSD']['details'][0];w['train_last_label_end']=w['start']
        with self.assertRaisesRegex(ValueError,'Noncausal'):er.replay(r,self.parts)

    def test_reject_changed_account(self):
        r=copy.deepcopy(self.report);r['results'][0]['reports']['XBTUSD']['details'][0]['models']['candidate']['normal']['net']+=.1
        with self.assertRaisesRegex(ValueError,'replay mismatch'):er.replay(r,self.parts)


if __name__=='__main__':unittest.main()

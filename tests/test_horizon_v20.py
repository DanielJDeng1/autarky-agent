import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from autarky_trade import weekly_models as wm, research_tree as tree, cross_asset as cross, horizon_review as hr, autonomous_research as old, lab
from autarky_trade.core import Bar, synthetic


class HorizonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.btc=synthetic(721+720*15)
        cls.eth=[Bar(b.ts,b.open*2,b.high*2,b.low*2,b.close*2,b.volume) for b in cls.btc]
        cls.cutoff=cls.btc[-1].ts
        cls.parts={'XBTUSD':[cls.btc],'ETHUSD':[cls.eth]}

    def test_horizons_share_dates_and_completed_labels(self):
        dates=None
        for h in wm.HORIZONS:
            r=wm.review([self.btc],'XBTUSD',self.cutoff,now=self.cutoff+3600,specs={'mean':()},horizon=h)
            self.assertEqual(r['windows'],3)
            self.assertEqual(r['protocol'],wm.HORIZON_PROTOCOL)
            self.assertEqual(r['opportunities_per_window'],(720-1)//h)
            starts=[w['start'] for w in r['details']]
            if dates is None: dates=starts
            self.assertEqual(dates,starts)
            for w in r['details']:
                self.assertLess(w['train_last_label_end'],w['start'])
                m=w['models']['mean']; self.assertEqual(len(m['samples']),(720-1)//h)
                for s in m['samples']:
                    self.assertEqual(s['label_end']-s['start'],h*3600)
                    self.assertLess(s['label_end'],w['start']+720*3600)
                if m['calibration_last_label_end'] is not None:
                    self.assertLess(m['calibration_last_label_end'],w['start'])
        legacy=wm.review([self.btc],'XBTUSD',self.cutoff,now=self.cutoff+3600,specs={'mean':()})
        self.assertEqual(legacy['windows'],9)
        self.assertEqual(legacy['protocol'],'v0.14-weekly-models-1')
        self.assertNotIn('horizon_hours',legacy)
        with self.assertRaises(ValueError): wm.horizon_slots(48)

    def test_scaled_calibration(self):
        rows=[{'prediction':.04,'realized':.1}]*1000
        for h in wm.HORIZONS:
            slots=wm.horizon_slots(h)
            self.assertIsNone(wm.horizon_calibrate(rows[:12*slots-1],h))
            self.assertIsNotNone(wm.horizon_calibrate(rows[:12*slots],h))
            self.assertIsNone(wm.horizon_calibrate(rows[:18*slots-1],h,True))
            self.assertIsNotNone(wm.horizon_calibrate(rows[:18*slots],h,True))

    def test_actual_entries_after_calibration(self):
        # A predictable rising market lets the calibrated account actually trade.
        bars=[]; price=100.
        for i in range(721+720*25):
            end=price*1.002
            bars.append(Bar(1700000000+i*3600,price,end,price,end,1.))
            price=end
        for h in (24,336):
            r=wm.review([bars],'XBTUSD',bars[-1].ts,now=bars[-1].ts+3600,specs={'mean':()},horizon=h)
            self.assertEqual(r['windows'],13)
            self.assertEqual(sum(w['models']['mean']['normal']['fills'] for w in r['details'][:-1]),0)
            last=r['details'][-1]['models']['mean']
            self.assertEqual(last['normal']['fills'],2*wm.horizon_slots(h))
            self.assertGreater(last['stress']['net'],0)
            self.assertAlmostEqual(last['normal']['exposure'],h*wm.horizon_slots(h)/720)
            self.assertLess(last['calibration_last_label_end'],r['details'][-1]['start'])

    def test_future_peer_price_isolation(self):
        p=tree.Spec((('momentum',24),)); s=tree.Spec(p.features+(('other_volatility',24),))
        r=tree.evaluate([self.btc],'XBTUSD',self.cutoff,p,s,now=self.cutoff+3600,other_parts=[self.eth],horizon=336)
        i=721+720*14
        changed=self.eth[:i]+[Bar(b.ts,b.open*4,b.high*4,b.low*4,b.close*4,b.volume) for b in self.eth[i:]]
        z=tree.evaluate([self.btc],'XBTUSD',self.cutoff,p,s,now=self.cutoff+3600,other_parts=[changed],horizon=336)
        self.assertEqual(r['details'][:2],z['details'][:2])
        self.assertFalse(r['eligible'])

    def test_frozen_node_sweep_and_separate_research(self):
        # Create a completed aligned weekly node, then review and use it at new horizons.
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'tree.sqlite'
            r=tree.run(path,self.parts,self.cutoff,model=None,emit=lambda _:None,cross_asset=True)
            self.assertEqual(r['completed'],3)
            nodes=hr.attribution.load_nodes(path,[1])
            report=hr.review(nodes,self.parts,self.cutoff,horizons=(72,336),now=self.cutoff+3600)
            self.assertEqual(len(report['results']),2)
            self.assertEqual(report['windows'],3)
            self.assertFalse(report['eligible'])
            for result in report['results']:
                self.assertEqual(set(result['markets']),set(old.PAIRS))
                self.assertEqual(result['reports']['XBTUSD']['horizon_hours'],result['horizon_hours'])
            with self.assertRaises(ValueError): hr.review(nodes,self.parts,self.cutoff-3600,horizons=(72,))
            # Explicitly choose a child of the existing weekly node.
            parent=tree.Spec(**nodes[0]['spec']); child=tree.Spec(parent.features,parent.method,parent.transform,'conservative' if parent.calibration=='standard' else 'standard')
            def choose(model,agent,available,seen,context,reject):
                self.assertIn('node:1',available)
                self.assertIn('node:1',context['source_nodes'])
                if child.identity not in seen: return 'node:1',child,'Does calibration improve?', 'deterministic'
                return original(model,agent,available,seen,context,reject)
            original=tree.propose
            with patch.object(tree,'propose',side_effect=choose):
                next_run=tree.run(path,self.parts,self.cutoff,model=None,emit=lambda _:None,cross_asset=True,horizon=336)
            self.assertEqual(next_run['completed'],3)
            self.assertNotEqual(r['dataset'],next_run['dataset'])
            data=tree.status(path); self.assertEqual(len(data['nodes']),6)
            self.assertEqual(next(n for n in data['nodes'] if n['id']==4)['parent'],'node:1')
            self.assertEqual(next_run['horizon_hours'],336)
            self.assertEqual(tree.status(path)['nodes'][0]['horizon_hours'],336)


if __name__=='__main__': unittest.main()

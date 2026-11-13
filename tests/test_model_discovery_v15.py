import unittest
from autarky_trade import model_discovery as d, weekly_models as wm
from autarky_trade.core import Bar, synthetic


class DiscoveryTests(unittest.TestCase):
    def test_constant_features_and_shrinkage(self):
        rows=[{'features':{k:0. for k in {k for spec in d.SPECS.values() for k in spec}},'realized':.02} for _ in range(24)]
        for name,spec in d.SPECS.items():
            self.assertAlmostEqual(d.fit_model(name,rows,spec)(rows[0]['features']),.02)
    def test_common_windows_and_no_future_effect(self):
        b=synthetic(721+720*9); cutoff=b[-1].ts
        a=d.evaluate([b],'XBTUSD',cutoff,now=cutoff+3600)
        self.assertEqual(set(a['summary']),set(d.SPECS))
        self.assertFalse(a['eligible'])
        self.assertEqual(a['paired_vs_mean']['mean']['net_delta'],0)
        self.assertIsNone(a['paired_vs_mean']['mean']['timing_accuracy'])
        self.assertEqual({m['observations'] for m in a['summary'].values()},{12})
        i=721+720*8
        c=b[:i]+[Bar(x.ts,x.open*2,x.high*2,x.low*2,x.close*2,x.volume) for x in b[i:]]
        r=d.evaluate([c],'XBTUSD',cutoff,now=cutoff+3600)
        self.assertEqual(a['details'][:2],r['details'][:2])
        orig=wm.review([b],'XBTUSD',cutoff,now=cutoff+3600)
        for name in wm.SPECS: self.assertEqual(a['summary'][name],orig['summary'][name])
    def test_regime_uses_training_groups(self):
        rows=[{'features':{d.REGIME:-.1},'realized':.1} for _ in range(12)] + [{'features':{d.REGIME:.1},'realized':-.1} for _ in range(12)]
        predict=d.fit_model('regime_mean',rows,(d.REGIME,))
        self.assertAlmostEqual(predict({d.REGIME:-.1}),.05)
        self.assertAlmostEqual(predict({d.REGIME:.1}),-.05)
        self.assertAlmostEqual(predict({d.REGIME:0}),0)

if __name__=='__main__': unittest.main()

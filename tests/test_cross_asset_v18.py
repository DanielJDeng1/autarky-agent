import tempfile
import unittest
from pathlib import Path
from autarky_trade import cross_asset as c, research_tree as t
from autarky_trade.core import Bar, synthetic


def scaled(bars, factor):
    return [Bar(b.ts,b.open*factor,b.high*factor,b.low*factor,b.close*factor,b.volume) for b in bars]


class CrossAssetTests(unittest.TestCase):
    def test_alignment_and_gap(self):
        b=synthetic(800); e=scaled(b,2)
        parts=c.align({'XBTUSD':[b], 'ETHUSD':[e[5:400],e[401:]]})
        self.assertEqual([len(s) for s in parts['XBTUSD']],[395,399])
        for a,z in zip(parts['XBTUSD'],parts['ETHUSD']):
            self.assertEqual([x.ts for x in a],[x.ts for x in z])
        self.assertIsNone(c.series(parts['XBTUSD'][1],parts['ETHUSD'][1],'relative_strength',24)[24])
        with self.assertRaises(ValueError): c.series(b,e[1:],'cross_correlation',24)
        with self.assertRaises(ValueError): c.series(b[:400]+b[401:],e[:400]+e[401:],'other_momentum',24)

    def test_causal_features(self):
        b=synthetic(800); e=scaled(b,2); i=725
        for k in c.FEATURES:
            a=c.series(b,e,k,24)
            future=e[:i]+scaled(e[i:],5)
            self.assertEqual(a[:i+1],c.series(b,future,k,24)[:i+1])
            self.assertIsNone(a[24])
        self.assertAlmostEqual(c.series(b,e,'cross_correlation',24)[i],1.)
        self.assertAlmostEqual(c.series(b,e,'relative_strength',24)[i],0.)

    def test_matched_controls_and_peer_future_isolation(self):
        b=synthetic(721+720*9); e=scaled(b,2); cutoff=b[-1].ts
        p=t.roots()['risk_long']; v=t.Spec(p.features+(('other_momentum',24),))
        x=t.evaluate([b],'XBTUSD',cutoff,p,v,now=cutoff+3600,other_parts=[e])
        y=t.evaluate([b],'XBTUSD',cutoff,p,p,now=cutoff+3600)
        self.assertEqual(x['summary']['parent'],y['summary']['parent'])
        self.assertEqual(x['summary']['mean'],y['summary']['mean'])
        i=721+720*8
        z=t.evaluate([b],'XBTUSD',cutoff,p,v,now=cutoff+3600,other_parts=[e[:i]+scaled(e[i:],5)])
        self.assertEqual(x['details'][:2],z['details'][:2])
        self.assertNotEqual(x['other_dataset_sha256'],z['other_dataset_sha256'])
        with self.assertRaises(ValueError): t.evaluate([b],'XBTUSD',cutoff,p,v,now=cutoff+3600)

    def test_cross_tree_and_separate_identity(self):
        b=synthetic(721+720*7); parts={'XBTUSD':[b],'ETHUSD':[scaled(b,2)]}; cutoff=b[-1].ts
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'tree.sqlite'
            a=t.run(db,parts,cutoff,model=None,emit=lambda _:None)
            x=t.run(db,parts,cutoff,model=None,emit=lambda _:None,cross_asset=True)
            self.assertEqual(x['completed'],3)
            self.assertNotEqual(a['dataset'],x['dataset'])
            data=t.status(db)
            self.assertEqual(len(data['nodes']),6)
            for n in data['nodes'][:3]:
                self.assertEqual(n['research_mode'],c.PROTOCOL)
                self.assertTrue(any(k[0] in c.FEATURES for k in n['spec']['features']))
                self.assertEqual(n['findings']['markets']['XBTUSD']['windows'],n['findings']['markets']['ETHUSD']['windows'])
                self.assertFalse(n['eligible'])
            for n in data['nodes'][3:]: self.assertFalse(any(k[0] in c.FEATURES for k in n['spec']['features']))
            y=t.run(db,parts,cutoff,model=None,emit=lambda _:None,cross_asset=True)
            self.assertEqual(y['completed'],3)
            self.assertEqual(len(t.status(db)['nodes']),9)

if __name__=='__main__': unittest.main()

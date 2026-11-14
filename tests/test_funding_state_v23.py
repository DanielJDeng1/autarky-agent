import unittest
from autarky_trade import funding_state as s, funding_research as f, lab
from autarky_trade.core import synthetic


class FundingStateTests(unittest.TestCase):
    def test_midrank_and_reset(self):
        self.assertEqual(s.trailing_ranks([1,1,1,2,0,None,3,3,3],2),[None,None,.5,1.,0.,None,None,None,.5])
        self.assertEqual(s.pick([.1,.5,.9,.2],'low'),{0,3})
        self.assertEqual(s.pick([.1,.5,.9,.2],'high'),{2})
        with self.assertRaises(ValueError): s.pick([None],'low')

    def test_causal_change_and_rank(self):
        b=synthetic(1000); values={x.ts:float(i//8) for i,x in enumerate(b)}
        a=s.states(b,values); altered={x.ts:values[x.ts] if i<850 else -100. for i,x in enumerate(b)}
        z=s.states(b,altered)
        self.assertEqual(a['level'][:850],z['level'][:850]); self.assertEqual(a['change'][:850],z['change'][:850])
        self.assertIsNotNone(a['change'][721])

    def test_review_matched_and_random_controls(self):
        b=synthetic(721+720*15); t=b[0].ts; cutoff=b[-1].ts
        events=[[(t+(i-8)*3600)*1000,8,.0001*((i//8)%13-6)] for i in range(0,len(b)+16,8)]
        d={'protocol':f.PROTOCOL,'symbols':f.SYMBOLS,'events':{p:events for p in f.SYMBOLS}}
        d['events_sha256']=lab.digest(d['events']); parts={p:[b] for p in f.SYMBOLS}
        r=s.review(parts,d,cutoff,now=cutoff+3600)
        self.assertEqual(len(r['results']),16); self.assertEqual(r['windows'],3); self.assertFalse(r['eligible'])
        self.assertTrue(any(x['selected'] for x in r['results']))
        for x in r['results']:
            self.assertAlmostEqual(x['mean_stress']-x['mean_random_stress'],x['stress_delta_random'])
        for result in r['details']:
            for w in result['windows']:
                if len(w['selected_indices']) in (0,w['opportunities']): self.assertEqual(w['stress']['net'],w['random_stress'])
        with self.assertRaises(ValueError): s.review(parts,d,cutoff,(24,),now=cutoff+3600)

if __name__=='__main__': unittest.main()

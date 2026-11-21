import tempfile
import unittest
from pathlib import Path
from autarky_trade import multiyear as m, lab
from autarky_trade.core import Bar


def bars(start, n):
    return [Bar(start+i*3600, 100+i*.01, 101+i*.01, 99+i*.01, 100+i*.01, 1) for i in range(n)]


class MultiYearTests(unittest.TestCase):
    def test_gap_split_and_cutoff(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'data.csv'
            p.write_text('0,1,1,1,1,1\n3600,1,1,1,1,1\n10800,2,2,2,2,2\n14400,2,2,2,2,2\n')
            self.assertEqual([len(x) for x in m.segments(p,10800)],[2,1])
    def test_fixed_windows_and_exclusions(self):
        parts=[bars(0, 721+168*2+9), bars(3600*2000, 500), bars(3600*3000,721+168)]
        r=m.review(parts, lab.defaults(), 'XBTUSD', parts[-1][-1].ts,168,now=10**9)
        self.assertEqual(r['windows'],3); self.assertEqual(r['evaluated_bars'],504)
        self.assertEqual(r['used_segments'],2); self.assertEqual(r['coverage'][1]['excluded'],500)
        self.assertEqual(r['coverage'][0]['excluded'],730)
        for x in r['results']:
            self.assertEqual(sum(v['windows'] for v in x['by_prior_regime'].values()),3)
            self.assertTrue(all(w['end']-w['start']==167*3600 for w in x['detail']))
            for w in x['detail']:
                if w['model_artifact']: self.assertLess(w['model_artifact']['last_label_end'],w['start'])
        self.assertFalse(r['eligible'])
    def test_future_and_short_rejected(self):
        b=bars(0,900)
        with self.assertRaises(ValueError): m.review([b],lab.defaults(),'XBTUSD',b[-1].ts,168,now=1)
        with self.assertRaises(ValueError): m.review([b[:500]],lab.defaults(),'XBTUSD',b[-1].ts,168,now=10**9)

if __name__=='__main__': unittest.main()

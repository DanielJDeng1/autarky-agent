import unittest
from autarky_trade import weekly_models as w
from autarky_trade.core import Bar


def bars(n):
    return [Bar(i*3600,100+i*.01,101+i*.01,99+i*.01,100+i*.01,1) for i in range(n)]


class WeeklyModelsTests(unittest.TestCase):
    def test_matched_training_and_calibration(self):
        b=bars(721+720*14); cutoff=b[-1].ts
        r=w.review([b],'XBTUSD',cutoff,now=10**9)
        self.assertEqual(r['windows'],8)
        self.assertFalse(r['eligible'])
        for x in r['details']:
            self.assertLess(x['train_last_label_end'],x['start'])
            for m in x['models'].values():
                self.assertEqual(len(m['samples']),4)
                if m['calibration_last_label_end'] is not None:
                    self.assertLess(m['calibration_last_label_end'],x['start'])
                if m['calibration_count']<24:
                    self.assertEqual(m['normal']['fills'],0)
        self.assertEqual({m['observations'] for m in r['summary'].values()},{32})
    def test_later_candles_cannot_change_first_forecasts(self):
        b=bars(721+720*8); cutoff=b[-1].ts
        r=w.review([b],'XBTUSD',cutoff,now=10**9)
        i=721+720*7
        c=b[:i]+[Bar(x.ts,x.open*2,x.high*2,x.low*2,x.close*2,x.volume) for x in b[i:]]
        s=w.review([c],'XBTUSD',cutoff,now=10**9)
        self.assertEqual(r['details'][0],s['details'][0])
    def test_reject_gaps_and_short_history(self):
        b=bars(1000)
        with self.assertRaises(ValueError): w.review([b],'XBTUSD',b[-1].ts,now=10**9)
        with self.assertRaises(ValueError): w.review([b[:500]+b[501:]],'XBTUSD',b[-1].ts,now=10**9)

if __name__=='__main__': unittest.main()

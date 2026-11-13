import unittest
from autarky_trade import weekly_review as w, forecast_audit as fa, lab
from autarky_trade.core import Bar


def bars(n):
    return [Bar(i*3600,100+i*.01,101+i*.01,99+i*.01,100+i*.01,1) for i in range(n)]


class WeeklyReviewTests(unittest.TestCase):
    def test_account_round_trip_and_cash(self):
        b=bars(720); rows=[{'start':0,'label_end':168*3600}]
        self.assertEqual(w.account(b,rows,set(),lab.NORMAL)['net'],0)
        r=w.account(b,rows,{0},lab.NORMAL)
        self.assertEqual(r['fills'],2)
        self.assertAlmostEqual(r['net'],.25*fa.net_return(b[168].open/b[0].open-1,lab.NORMAL))
    def test_chronological_review_and_mismatch(self):
        parts=[bars(721+720*3)]; cutoff=parts[0][-1].ts
        audit=fa.audit(parts,'XBTUSD',cutoff,now=10**9)
        report=w.evaluate(parts,'XBTUSD',cutoff,audit)
        self.assertEqual(report['windows'],3)
        self.assertFalse(report['eligible'])
        self.assertEqual(report['details'][0]['selected'],0)
        self.assertEqual(report['summary']['normal']['ridge']['fills'],0)
        with self.assertRaises(ValueError): w.evaluate(parts,'ETHUSD',cutoff,audit)
    def test_stop_exits_next_open(self):
        b=[Bar(i*3600,100 if i==0 else 50,101 if i==0 else 51,49,50,1) for i in range(720)]
        r=w.account(b,[{'start':0,'label_end':168*3600}],{0},lab.NORMAL)
        self.assertTrue(r['stopped']); self.assertEqual(r['fills'],2); self.assertAlmostEqual(r['exposure'],1/720)

if __name__=='__main__': unittest.main()

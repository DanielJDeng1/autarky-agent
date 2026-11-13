import unittest, tempfile, json, time
from pathlib import Path
from unittest.mock import patch
from autarky_trade.core import *
from autarky_trade.__main__ import paper_tick
class Tests(unittest.TestCase):
    def test_invalid_strategy(self):
        with self.assertRaises(ValueError): Strategy('shell',2,10,'bad')
    def test_no_leverage(self):
        a=Account(); a.fill(True,100,100,Costs()); self.assertAlmostEqual(a.cash,750); self.assertLess(a.units*100,250); a.fill(False,100,100,Costs()); self.assertLess(a.cash,1000)
    def test_drawdown(self):
        a=Account(); a.fill(True,100,100,Costs()); a.mark(1); self.assertTrue(a.stopped); a.fill(False,1,1,Costs()); a.fill(True,1,1,Costs()); self.assertEqual(a.units,0)
    def test_no_lookahead(self):
        bars=synthetic(); s=BASELINES[0]; first=backtest(bars,s,Costs(),100,150); changed=bars[:150]+[Bar(b.ts,1,1,1,1,1) for b in bars[150:]]; self.assertEqual(first,backtest(changed,s,Costs(),100,150))
    def test_evaluate(self):
        r=evaluate(synthetic(),BASELINES,Costs()); self.assertIn('holdout_stress',r); self.assertEqual(len(r['candidates']),4)
    def test_gap(self):
        b=synthetic(); del b[10]
        with self.assertRaises(ValueError): validate(b,3600)
    def test_synthetic_cannot_trade(self):
        with self.assertRaises(ValueError): paper_tick({'paper_eligible':True,'source':'synthetic'},'unused')
    def test_paper_duplicate(self):
        b=synthetic(); now=int(time.time()); b=[Bar(now-(len(b)-i)*3600,x.open,x.high,x.low,x.close,x.volume) for i,x in enumerate(b)]
        r={'paper_eligible':True,'source':'csv','pair':'XBTUSD','interval':60,'selected':asdict(BASELINES[0]),'costs':asdict(Costs()),'holdout_end':b[-2].ts}
        with tempfile.TemporaryDirectory() as d, patch('autarky_trade.__main__.kraken',return_value=b),patch('autarky_trade.__main__.quote',return_value=(100,101)):
            path=str(Path(d)/'paper.sqlite'); self.assertEqual(paper_tick(r,path)['status'],'paper only'); self.assertEqual(paper_tick(r,path)['status'],'already processed')
    def test_holdout_wait(self):
        b=synthetic(); now=int(time.time()); b=[Bar(now-(len(b)-i)*3600,x.open,x.high,x.low,x.close,x.volume) for i,x in enumerate(b)]
        r={'paper_eligible':True,'source':'kraken','pair':'XBTUSD','interval':60,'selected':asdict(BASELINES[0]),'costs':asdict(Costs()),'holdout_end':b[-1].ts}
        with patch('autarky_trade.__main__.kraken',return_value=b): self.assertIn('waiting',paper_tick(r,'unused')['status'])
if __name__=='__main__': unittest.main()

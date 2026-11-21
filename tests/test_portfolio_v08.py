import json
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from autarky_trade import portfolio as p, community, research
from autarky_trade.core import Bar, Strategy


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.path=self.root/'portfolio.sqlite'; p.init(self.path)
        self.ts=1700002800; self.now=self.ts+3600
        self.sources={pair:self.trial(pair) for pair in p.PAIRS}
    def tearDown(self): self.tmp.cleanup()
    def trial(self,pair):
        path=self.root/(pair+'.sqlite'); community.init(path,pair=pair)
        with community.db(path) as c:
            c.execute('INSERT INTO rounds VALUES(1,?,?,?,?,?)',(self.ts-10000,'csv',self.ts-500*3600,self.ts-101*3600,1000))
            for agent,fast in [('alpha',5),('beta',6)]:
                s=Strategy('trend',fast,20,agent)
                report=dict(protocol=research.PROTOCOL,eligible=True,strategy=asdict(s),strategy_id=research.identity(s),dataset_sha256='a'*64,candidates=[{}])
                c.execute('INSERT INTO trials VALUES(1,?,?)',(agent,json.dumps(report)))
            c.commit()
        return path
    def register(self):
        for pair in p.PAIRS: p.register(self.path,self.sources[pair],'alpha',1,2000,self.now)
    def snapshots(self,bid=130.,ask=130.1,stamp=None,candle=None):
        stamp=self.now if stamp is None else stamp; candle=self.ts if candle is None else candle
        bars=[Bar(candle-(219-i)*3600,100+i*.1,100+i*.1,100+i*.1,100+i*.1,10) for i in range(220)]
        return {pair:(bars,bid,ask,stamp) for pair in p.PAIRS}
    def test_register_and_cap(self):
        self.register(); s=p.status(self.path)
        self.assertEqual(s['reserve'],600); self.assertEqual(s['equity'],1000); self.assertTrue(s['reconciled'])
        with self.assertRaisesRegex(ValueError,'Market funding'): p.register(self.path,self.sources['XBTUSD'],'beta',1,2500,self.now)
    def test_trade_once_per_candle_and_reconcile(self):
        self.register(); p.cycle(self.path,self.snapshots(),self.now)
        s=p.status(self.path); self.assertTrue(s['reconciled']); self.assertEqual([x['fills'] for x in s['sleeves']],[1,1]); self.assertLess(s['equity'],1000)
        self.assertEqual(p.cycle(self.path,self.snapshots(),self.now)['status'],'duplicate or older observation')
        p.cycle(self.path,self.snapshots(131,131.1,self.now+10),self.now+10)
        self.assertEqual([x['fills'] for x in p.status(self.path)['sleeves']],[1,1]); self.assertTrue(p.status(self.path)['reconciled'])
        with self.assertRaisesRegex(ValueError,'before the first cycle'): p.register(self.path,self.sources['XBTUSD'],'beta',1,1000,self.now+10)
    def test_incomplete_stale_and_skew_are_atomic(self):
        self.register(); snap=self.snapshots(); snap.pop('ETHUSD')
        with self.assertRaisesRegex(ValueError,'complete snapshot'): p.cycle(self.path,snap,self.now)
        snap=self.snapshots(); snap['ETHUSD']=(*snap['ETHUSD'][:3],self.now-31)
        # At candle boundary the older quote precedes closure, so either safety check is valid.
        with self.assertRaises(ValueError): p.cycle(self.path,snap,self.now)
        with self.assertRaisesRegex(ValueError,'stale'): p.cycle(self.path,self.snapshots(),self.now+91)
        self.assertEqual(p.status(self.path)['last_observed'],0)
    def test_intrabar_portfolio_crash_flattens(self):
        self.register(); p.cycle(self.path,self.snapshots(),self.now)
        p.cycle(self.path,self.snapshots(1.,1.1,self.now+1),self.now+1)
        s=p.status(self.path); self.assertTrue(s['stopped']); self.assertEqual(sum(s['exposures'].values()),0); self.assertTrue(s['reconciled'])
        p.cycle(self.path,self.snapshots(130,130.1,self.now+3600,self.ts+3600),self.now+3600)
        self.assertEqual([x['fills'] for x in p.status(self.path)['sleeves']],[2,2])
    def test_operator_stop_flattens_next_fresh_quote(self):
        self.register(); p.cycle(self.path,self.snapshots(),self.now)
        with p.connect(self.path) as c: c.execute('UPDATE portfolio SET stopped=1'); c.commit()
        p.cycle(self.path,self.snapshots(130,130.1,self.now+1),self.now+1)
        self.assertTrue(p.status(self.path)['reconciled']); self.assertEqual(sum(p.status(self.path)['exposures'].values()),0)
    def test_registration_requires_eligible_trial(self):
        with community.db(self.sources['XBTUSD']) as c: c.execute('UPDATE trials SET report=?',(json.dumps({'eligible':False}),)); c.commit()
        with self.assertRaisesRegex(ValueError,'eligible'): p.register(self.path,self.sources['XBTUSD'],'alpha',1,2000,self.now)
    def test_rebalance_uses_nav_and_conserves_equity(self):
        self.register()
        # Construct a reconciled flat forward-history fixture with opposite gains.
        with p.connect(self.path) as c:
            for pair,delta in [('XBTUSD',20),('ETHUSD',-20)]:
                name=pair+':alpha'; row=c.execute('SELECT account FROM sleeves WHERE id=?',(name,)).fetchone(); a=json.loads(row[0]); a['cash']+=delta; a['trades']=20; a['peak']=max(a['peak'],a['cash']); a['drawdown']=max(0,-delta/200)
                c.execute('UPDATE sleeves SET account=?,since=?,ticks=1000 WHERE id=?',(json.dumps(a),self.ts-35*86400,name))
                # Two synthetic journal entries per pair would not reconcile fill count; use 20.
                for _ in range(20): p._ledger(c,self.now,name,'fill','fixture',delta/20)
            c.execute('UPDATE portfolio SET last_observed=?,last_candle=?',(self.now,self.ts)); c.commit()
        before=p.status(self.path); r=p.rebalance(self.path,self.now); after=p.status(self.path)
        self.assertEqual(r['status'],'allocated'); self.assertEqual(len(r['moves']),2)
        self.assertAlmostEqual(before['equity'],after['equity']); self.assertTrue(after['reconciled'])
        for a,b in zip(before['sleeves'],after['sleeves']): self.assertAlmostEqual(a['nav_return'],b['nav_return']); self.assertAlmostEqual(a['pnl'],b['pnl'])
        self.assertNotEqual(p.rebalance(self.path,self.now)['status'],'allocated')
    def test_stale_rebalance_and_tampering(self):
        self.register(); self.assertNotEqual(p.rebalance(self.path,self.now)['status'],'allocated')
        with p.connect(self.path) as c: c.execute('UPDATE portfolio SET reserve=reserve+1'); c.commit()
        self.assertFalse(p.status(self.path)['reconciled'])

if __name__=='__main__': unittest.main()

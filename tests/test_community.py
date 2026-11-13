import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from autarky_trade import community as m
from autarky_trade.core import Bar, Account, Strategy, synthetic

class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'db.sqlite')
        m.init(self.path); self.bars=synthetic(1000); self.ts=self.bars[-1].ts; self.now=self.ts+3600
    def tearDown(self): self.tmp.cleanup()
    def activate(self, name='alpha', after=None):
        with m.db(self.path) as c:
            c.execute('UPDATE agents SET pending=?,pending_after=? WHERE id=?',(m.encode(asdict(Strategy('trend',2,5,'test'))),self.ts-3600 if after is None else after,name)); c.commit()
    def rising(self, shift=0):
        return [Bar(b.ts+shift,100+i,101+i,99+i,100+i,1) for i,b in enumerate(self.bars)]
    def test_initial_conservation_and_restart(self):
        m.init(self.path); s=m.status(self.path); self.assertEqual(s['equity'],1000); self.assertEqual(s['reserve'],250); self.assertEqual(len(s['agents']),3)
        with self.assertRaises(ValueError): m.init(self.path,'ETHUSD')
    def test_synthetic_does_not_consume_holdout(self):
        m.research(self.path,self.bars,'synthetic'); self.assertEqual(m.status(self.path)['last_holdout'],0)
        with m.db(self.path) as c: self.assertEqual(c.execute('SELECT count(*) FROM trials').fetchone()[0],0)
    def test_research_disjoint_holdouts(self):
        first=m.research(self.path,self.bars,'csv',now=self.now); self.assertEqual(first['status'],'researched')
        self.assertEqual(m.research(self.path,self.bars,'csv',now=self.now)['status'],'waiting for new unseen data')
        new=self.rising(120*3600); second=m.research(self.path,new,'csv',now=self.now+120*3600)
        self.assertGreater(second['test_start'],first['test_end']); self.assertEqual(second['status'],'researched')
    def test_future_history_rejected(self):
        with self.assertRaises(ValueError): m.research(self.path,self.bars,'csv',now=self.ts)
    def test_distinct_trials(self):
        m.research(self.path,self.bars,'csv',now=self.now)
        with m.db(self.path) as c:
            rows=list(c.execute('SELECT * FROM trials')); self.assertEqual(len(rows),3)
            for r in rows: self.assertEqual(json.loads(r['report'])['strategy']['kind'],m.AGENTS[r['agent']])
    def test_pending_cannot_trade_on_holdout(self):
        self.activate(after=self.ts); r=m.tick(self.path,self.rising(),100,101,self.now); self.assertEqual(r['agents']['alpha']['fills'],0)
    def test_tick_idempotent_and_capital_conserved(self):
        self.activate(); r=m.tick(self.path,self.rising(),100,101,self.now); self.assertEqual(r['agents']['alpha']['fills'],1); self.assertLess(r['equity'],1000)
        self.assertEqual(m.tick(self.path,self.rising(),100,101,self.now)['status'],'already processed'); self.assertEqual(m.status(self.path)['agents'][0]['fills'],1)
        self.assertEqual(m.status(self.path)['agents'][0]['active_fills'],1)
    def test_stale_quote_and_candle(self):
        with self.assertRaises(ValueError): m.tick(self.path,self.rising(),100,101,self.now+99999)
        with self.assertRaises(ValueError): m.tick(self.path,self.rising(),101,100,self.now)
    def test_portfolio_stop_flattens(self):
        for name in m.AGENTS: self.activate(name)
        m.tick(self.path,self.rising(),100,100,self.now)
        r=m.tick(self.path,self.rising(3600),1,1,self.now+3600); self.assertTrue(r['stopped'])
        with m.db(self.path) as c:
            for row in c.execute('SELECT account FROM agents'): self.assertEqual(json.loads(row[0])['units'],0)
    def test_manual_stop_flattens_same_candle(self):
        self.activate(); m.tick(self.path,self.rising(),100,100,self.now)
        with m.db(self.path) as c:
            c.execute('UPDATE treasury SET stopped=1'); c.commit()
        self.assertEqual(m.tick(self.path,self.rising(),99,100,self.now)['status'],'halted and flattened')
        with m.db(self.path) as c: self.assertEqual(json.loads(c.execute("SELECT account FROM agents WHERE id='alpha'").fetchone()[0])['units'],0)
    def test_switch_unwinds_before_activate(self):
        self.activate(); m.tick(self.path,self.rising(),100,100,self.now)
        with m.db(self.path) as c:
            c.execute('UPDATE agents SET pending=?,pending_after=? WHERE id=?',(m.encode(asdict(Strategy('trend',3,6,'next'))),self.ts,'alpha')); c.commit()
        r=m.tick(self.path,self.rising(3600),100,100,self.now+3600); self.assertEqual(r['agents']['alpha']['active']['name'],'test'); self.assertEqual(r['agents']['alpha']['fills'],1)
        r=m.tick(self.path,self.rising(7200),100,100,self.now+7200); self.assertEqual(r['agents']['alpha']['active']['name'],'next'); self.assertEqual(m.status(self.path)['agents'][0]['active_fills'],1)
    def test_allocation_preserves_nav(self):
        a=Account(cash=275,peak=275,trades=22)
        with m.db(self.path) as c:
            c.execute('UPDATE agents SET account=?,first_tick=?,active=?,active_since=?,active_fills=22,active_ticks=745,active_start_nav=1 WHERE id=?',(m.encode(asdict(a)),self.ts-31*86400,m.encode(asdict(Strategy('trend',2,5,'test'))),self.ts-31*86400,'alpha')); c.execute('UPDATE treasury SET last_ts=?,last_bid=100',(self.ts,)); c.commit()
        before=m.status(self.path); r=m.rebalance(self.path,self.now); after=m.status(self.path)
        self.assertEqual(r['status'],'allocated'); self.assertAlmostEqual(before['equity'],after['equity']); self.assertAlmostEqual(before['agents'][0]['nav_return'],after['agents'][0]['nav_return']); self.assertAlmostEqual(before['agents'][0]['net_pnl'],after['agents'][0]['net_pnl'])
        self.assertEqual(m.rebalance(self.path,self.now)['status'],'stopped or cooldown')
    def test_no_evidence_no_allocation(self):
        m.tick(self.path,self.rising(),100,101,self.now); self.assertEqual(m.rebalance(self.path,self.now)['status'],'insufficient positive forward evidence')
    def test_readiness_not_live(self):
        self.assertFalse(any(a['eligible_for_manual_review'] for a in m.readiness(self.path)['agents']))
    def test_local_model_rejects_invalid_candidates(self):
        with patch.object(m,'request',return_value={'message':{'content':json.dumps({'strategies':[{'fast':2,'slow':10},{'fast':99,'slow':1},{'fast':True,'slow':10}]})}}):
            c=m.candidates('trend','qwen',self.bars[:500],'alpha',[]); self.assertEqual(len(c),5)

if __name__=='__main__': unittest.main()

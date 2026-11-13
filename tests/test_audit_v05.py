import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from autarky_trade import community as m, jobs, research
from autarky_trade.core import Account, Strategy, Bar, synthetic


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'db.sqlite')
        m.init(self.path); self.bars=synthetic(); self.ts=self.bars[-1].ts; self.now=self.ts+3600
    def tearDown(self): self.tmp.cleanup()
    def rising(self, shift=0):
        return [Bar(b.ts+shift,100+i,101+i,99+i,100+i,1) for i,b in enumerate(self.bars)]
    def activate(self, name='alpha', after=None):
        with m.db(self.path) as c:
            c.execute('UPDATE agents SET pending=?,pending_after=? WHERE id=?',(m.encode(asdict(Strategy('trend',2,5,'test'))),self.ts-3600 if after is None else after,name)); c.commit()
    def test_same_candle_risk_stop_no_new_tick(self):
        for name in m.AGENTS: self.activate(name)
        m.tick(self.path,self.rising(),100,100,self.now)
        result=m.tick(self.path,self.rising(),1,1,self.now+60)
        self.assertEqual(result['status'],'halted and flattened')
        s=m.status(self.path); self.assertTrue(s['stopped']); self.assertLess(s['equity'],920)
        self.assertTrue(all(a['fills']==2 and a['active_ticks']==1 for a in s['agents']))
        with m.db(self.path) as c: self.assertEqual(c.execute('SELECT count(*) FROM ticks').fetchone()[0],1)
    def test_same_candle_risk_does_not_enter_again(self):
        self.activate(); m.tick(self.path,self.rising(),100,100,self.now)
        m.tick(self.path,self.rising(),110,110,self.now+60)
        a=m.status(self.path)['agents'][0]; self.assertEqual(a['fills'],1); self.assertEqual(a['active_ticks'],1); self.assertGreater(a['equity'],250)
    def test_old_strategy_lifetime_profit_does_not_allocate(self):
        a=Account(cash=275,peak=275,trades=50)
        with m.db(self.path) as c:
            c.execute('UPDATE agents SET account=?,first_tick=?,active=?,active_since=?,active_fills=0,active_ticks=1,active_start_nav=1.1 WHERE id=?',(m.encode(asdict(a)),self.ts-60*86400,m.encode(asdict(Strategy('trend',2,5,'new'))),self.ts,'alpha'))
            c.execute('UPDATE treasury SET last_ts=?,last_bid=100',(self.ts,)); c.commit()
        self.assertEqual(m.rebalance(self.path,self.now)['status'],'insufficient positive forward evidence')
    def test_future_pending_does_not_interrupt_active(self):
        self.activate(); m.tick(self.path,self.rising(),100,100,self.now)
        with m.db(self.path) as c:
            c.execute('UPDATE agents SET pending=?,pending_after=? WHERE id=?',(m.encode(asdict(Strategy('trend',3,6,'future'))),self.ts+7200,'alpha')); c.commit()
        r=m.tick(self.path,self.rising(3600),100,100,self.now+3600)
        self.assertEqual(r['agents']['alpha']['fills'],0); self.assertTrue(r['agents']['alpha']['signal'])
    def test_strategy_identity_ignores_name_and_hypothesis(self):
        a=Strategy('trend',2,5,'a',(), 'claim A'); b=Strategy('trend',2,5,'b',(), 'claim B')
        self.assertEqual(research.identity(a),research.identity(b))
        self.assertTrue(m.same_strategy(m.encode(asdict(a)),m.encode(asdict(b))))
    def test_holdout_cannot_change_selection(self):
        s=[Strategy('trend',5,20,'one'),Strategy('trend',12,48,'two')]
        a=research.assess(self.bars,s,800)
        changed=self.bars[:800]+[replace(b,open=1,high=1,low=1,close=1) for b in self.bars[800:]]
        b=research.assess(changed,s,800)
        self.assertEqual(a['strategy_id'],b['strategy_id']); self.assertEqual(a['candidates'],b['candidates']); self.assertNotEqual(a['dataset_sha256'],b['dataset_sha256'])
        self.assertEqual(len(a['candidates'][0]['folds']),3)
    def test_failing_pretest_cannot_win(self):
        good=Strategy('trend',5,20,'good'); bad=Strategy('trend',12,48,'bad')
        def fake(b,s,c,start,end):
            return {'return':.5 if s.name=='bad' else .01,'drawdown':0,'trades':0 if s.name=='bad' else 5,'fees':0,'stopped':False}
        with patch.object(research,'backtest',side_effect=fake):
            r=research.assess(self.bars,[bad,good],800)
        self.assertEqual(r['strategy']['name'],'good'); self.assertFalse(r['candidates'][1]['pretest_pass'])
    def test_duplicate_candidates_only_evaluated_once(self):
        a=Strategy('trend',5,20,'one'); b=Strategy('trend',5,20,'two')
        self.assertEqual(len(research.assess(self.bars,[a,b],800)['candidates']),1)
    def test_plan_survives_retry(self):
        jobs.schedule(self.path,self.bars,'csv',self.now)
        j=jobs.claim(self.path,'alpha'); original=jobs.plan(self.path,j,None,self.bars[:600])
        self.assertTrue(jobs.finish(self.path,j,error='retry'))
        k=jobs.claim(self.path,'alpha')
        with patch.object(m,'candidates',side_effect=AssertionError('must reuse plan')):
            self.assertEqual(jobs.plan(self.path,k,'another-model',self.bars[:600]),original)
    def test_forward_trial_cannot_be_replaced_early(self):
        self.activate(); m.tick(self.path,self.rising(),100,100,self.now)
        with m.db(self.path) as c:
            result=m.stage_candidate(c,'alpha',asdict(Strategy('trend',3,6,'new')),self.ts)
            self.assertEqual(result,'active forward trial frozen'); c.commit()
        self.assertIsNone(m.status(self.path)['agents'][0]['pending'])
    def test_pending_candidate_frozen(self):
        self.activate()
        with m.db(self.path) as c:
            self.assertEqual(m.stage_candidate(c,'alpha',asdict(Strategy('trend',3,6,'new')),self.ts),'pending candidate already frozen')
            c.commit()
        self.assertEqual(m.status(self.path)['agents'][0]['pending']['name'],'test')
    def test_stopped_batch_cannot_promote(self):
        jobs.schedule(self.path,self.bars,'csv',self.now)
        for name in m.AGENTS:
            j=jobs.claim(self.path,name)
            r={'eligible':True,'protocol':research.PROTOCOL,'strategy':asdict(Strategy(m.AGENTS[name],2,5,'test'))}
            self.assertTrue(jobs.finish(self.path,j,report=r))
        with m.db(self.path) as c: c.execute('UPDATE treasury SET stopped=1'); c.commit()
        jobs.finalize(self.path)
        self.assertTrue(all(a['pending'] is None for a in m.status(self.path)['agents']))

if __name__=='__main__': unittest.main()

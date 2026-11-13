import json
import tempfile
from pathlib import Path
import unittest
from autarky_trade import community as m, jobs, allocation
from autarky_trade.core import synthetic


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'c.sqlite'
        m.init(self.path); self.bars=synthetic(2000); self.now=self.bars[-1].ts+3600
    def tearDown(self): self.temp.cleanup()

    def test_one_slot_rotates_and_records_choice(self):
        chosen=[]
        for end in (1000,1150,1300,1450):
            r=jobs.schedule(self.path,self.bars[:end],'csv',self.now,slots=1)
            self.assertEqual(len(r['agents']),1)
            agent=r['agents'][0]; chosen.append(agent)
            for _ in range(jobs.MAX_ATTEMPTS):
                j=jobs.claim(self.path,agent,self.now)
                jobs.finish(self.path,j,error='fixture failure',now=self.now)
            self.assertEqual(jobs.finalize(self.path)['status'],'failed; holdout consumed')
        self.assertEqual(set(chosen[:3]),set(m.AGENTS))
        self.assertEqual(chosen[3],chosen[0])
        with m.db(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],4)
            e=json.loads(c.execute("SELECT detail FROM events WHERE kind='batch_scheduled' ORDER BY id DESC LIMIT 1").fetchone()[0]); self.assertEqual(e['slots'],1)

    def test_partial_batch_preserves_completed_evidence(self):
        jobs.schedule(self.path,self.bars[:1000],'csv',self.now,slots=2)
        self.assertEqual(jobs.work(self.path,'alpha')['status'],'completed')
        for _ in range(jobs.MAX_ATTEMPTS):
            j=jobs.claim(self.path,'beta',self.now); jobs.finish(self.path,j,error='fixture',now=self.now)
        r=jobs.finalize(self.path); self.assertEqual(r['status'],'partial; holdout consumed'); self.assertEqual(r['failed'],['beta'])
        with m.db(self.path) as c: self.assertEqual(c.execute('SELECT agent FROM trials').fetchone()[0],'alpha')
        self.assertEqual(jobs.schedule(self.path,self.bars[:1000],'csv',self.now)['status'],'waiting for fresh data')

    def test_abandon_preserves_completed_and_rejects_stale_worker(self):
        jobs.schedule(self.path,self.bars[:1000],'csv',self.now)
        self.assertEqual(jobs.work(self.path,'alpha')['status'],'completed')
        stale=jobs.claim(self.path,'beta',self.now)
        result=jobs.abandon(self.path,self.now+1)
        self.assertEqual(result['status'],'partial; holdout consumed')
        self.assertEqual(set(result['failed']),{'beta','gamma'})
        self.assertFalse(jobs.finish(self.path,stale,report={},now=self.now+2))
        with m.db(self.path) as c: self.assertEqual(c.execute('SELECT count(*) FROM trials').fetchone()[0],1)

    def test_invalid_slots_do_not_burn_data(self):
        for v in (0,4,True):
            with self.assertRaises(ValueError): jobs.schedule(self.path,self.bars[:1000],'csv',self.now,slots=v)
        self.assertEqual(m.config(self.path)['last_holdout'],0)

    def test_forward_score_uses_current_strategy_not_lifetime_profit(self):
        jobs.setup(self.path)
        with m.db(self.path) as c:
            # Mark all three as recently scheduled so no initial-exploration priority.
            c.execute("INSERT INTO batches VALUES(1,0,'csv',1,2,0,'[]','done')")
            c.executemany("INSERT INTO jobs(batch_id,agent,state) VALUES(1,?,'done')",[(a,) for a in m.AGENTS])
            c.execute('UPDATE treasury SET last_ts=?,last_bid=100',(40*86400,))
            for a,cash,start_nav in [('alpha',500,2.1),('beta',300,1.0),('gamma',250,1.0)]:
                state=json.loads(c.execute('SELECT account FROM agents WHERE id=?',(a,)).fetchone()[0]); state['cash']=cash
                c.execute('UPDATE agents SET account=?,active=?,active_since=0,active_fills=20,active_ticks=1000,active_start_nav=? WHERE id=?',(m.encode(state),'{}',start_nav,a))
            chosen,rank=allocation.choose(c,1,40*86400+3600)
            self.assertEqual(chosen[0]['agent'],'beta')
            self.assertIsNone(next(r for r in rank if r['agent']=='alpha')['forward_score'])
            chosen,rank=allocation.choose(c,1,40*86400+3*3600)
            self.assertTrue(all(r['forward_score'] is None for r in rank))
            c.rollback()

if __name__=='__main__': unittest.main()

import tempfile, unittest
from pathlib import Path
from autarky_trade import community as m, jobs
from autarky_trade.core import synthetic

class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'c.sqlite'); m.init(self.path); self.bars=synthetic(); self.now=self.bars[-1].ts+3600
    def tearDown(self): self.tmp.cleanup()
    def test_complete_batch(self):
        self.assertEqual(jobs.schedule(self.path,self.bars,'csv',self.now)['status'],'scheduled')
        self.assertEqual(jobs.finalize(self.path)['status'],'waiting for workers')
        for a in m.AGENTS: self.assertEqual(jobs.work(self.path,a)['status'],'completed')
        self.assertEqual(jobs.finalize(self.path)['status'],'finalized')
        self.assertEqual(jobs.finalize(self.path)['status'],'no open batch')
        with m.db(self.path) as c: self.assertEqual(c.execute('SELECT count(*) FROM trials').fetchone()[0],3)
    def test_lease_recovery_and_stale_finish(self):
        jobs.schedule(self.path,self.bars,'csv',self.now); j=jobs.claim(self.path,'alpha',self.now); self.assertIsNone(jobs.claim(self.path,'alpha',self.now+1)); k=jobs.claim(self.path,'alpha',self.now+jobs.LEASE); self.assertNotEqual(j['token'],k['token']); self.assertFalse(jobs.finish(self.path,j,error='old',now=self.now+jobs.LEASE)); self.assertTrue(jobs.finish(self.path,k,error='retry',now=self.now+jobs.LEASE+1))
    def test_failed_batch_consumes_holdout(self):
        jobs.schedule(self.path,self.bars,'csv',self.now)
        for i in range(jobs.MAX_ATTEMPTS):
            j=jobs.claim(self.path,'alpha',self.now); self.assertTrue(jobs.finish(self.path,j,error='failure',now=self.now))
        self.assertEqual(jobs.finalize(self.path)['status'],'failed; holdout consumed'); self.assertEqual(jobs.schedule(self.path,self.bars,'csv',self.now)['status'],'waiting for fresh data')
    def test_no_synthetic_queue(self):
        with self.assertRaises(ValueError): jobs.schedule(self.path,self.bars,'synthetic',self.now)
    def test_holdout_reserved_once(self):
        jobs.schedule(self.path,self.bars,'csv',self.now); self.assertEqual(jobs.schedule(self.path,self.bars,'csv',self.now)['status'],'batch already open'); self.assertEqual(m.status(self.path)['last_holdout'],self.bars[-1].ts)
if __name__=='__main__': unittest.main()

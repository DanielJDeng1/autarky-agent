import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from autarky_trade import community as m, paper_audit as audit
from autarky_trade.core import Bar, Strategy, synthetic

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'db.sqlite'); m.init(self.path)
        self.bars=[Bar(b.ts,100+i,101+i,99+i,100+i,1) for i,b in enumerate(synthetic())]
        self.ts=self.bars[-1].ts; self.now=self.ts+3600
        with m.db(self.path) as c:
            c.execute('UPDATE agents SET pending=?,pending_after=? WHERE id=?',(m.encode(asdict(Strategy('trend',2,5,'test'))),self.ts-3600,'alpha')); c.commit()
    def tearDown(self): self.tmp.cleanup()
    def test_fills_and_cash_reconcile(self):
        m.tick(self.path,self.bars,100,101,self.now)
        with m.db(self.path) as c: c.execute('UPDATE treasury SET stopped=1'); c.commit()
        m.tick(self.path,self.bars,99,100,self.now+10)
        r=audit.report(self.path); self.assertTrue(all(a['reconciled'] for a in r['agents']))
        self.assertEqual(len(r['recent_fills']),2); sell,buy=r['recent_fills']
        self.assertEqual((buy['side'],sell['side']),('buy','sell')); self.assertEqual(sell['reason'],'portfolio_stop')
        self.assertAlmostEqual(buy['price'],101*1.001); self.assertAlmostEqual(sell['price'],99*.999)
        self.assertAlmostEqual(sell['quantity'],buy['quantity']); self.assertGreater(sell['fee'],0)
    def test_older_quote_does_not_change_account(self):
        m.tick(self.path,self.bars,100,100,self.now+20)
        before=m.status(self.path)
        self.assertEqual(m.tick(self.path,self.bars,1,1,self.now+10)['status'],'older quote ignored')
        self.assertEqual(m.status(self.path),before)
    def test_repeated_quote_does_not_duplicate_fills(self):
        m.tick(self.path,self.bars,100,100,self.now)
        m.tick(self.path,self.bars,100,100,self.now)
        r=audit.report(self.path); self.assertEqual(len(r['recent_fills']),1)
        with m.db(self.path) as c: self.assertEqual(c.execute('SELECT count(*) FROM paper_quotes').fetchone()[0],1)
    def test_legacy_baseline_explicit(self):
        with m.db(self.path) as c:
            account=json.loads(c.execute("SELECT account FROM agents WHERE id='alpha'").fetchone()[0]); account['trades']=7
            c.execute("UPDATE agents SET account=? WHERE id='alpha'",(m.encode(account),)); c.commit()
        r=audit.report(self.path)['agents'][0]; self.assertEqual(r['legacy_fills_before_journal'],7); self.assertTrue(r['reconciled'])
        m.tick(self.path,self.bars,100,100,self.now)
        r=audit.report(self.path)['agents'][0]; self.assertEqual(r['journaled_fills'],1); self.assertTrue(r['reconciled'])
    def test_corruption_visible(self):
        m.tick(self.path,self.bars,100,100,self.now)
        with m.db(self.path) as c:
            account=json.loads(c.execute("SELECT account FROM agents WHERE id='alpha'").fetchone()[0]); account['cash']+=1
            c.execute("UPDATE agents SET account=? WHERE id='alpha'",(m.encode(account),)); c.commit()
        self.assertFalse(audit.report(self.path)['agents'][0]['reconciled'])

if __name__=='__main__': unittest.main()

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from autarky_trade import economic_kernel as e
from test_research_allocator_v21 import report


class EconomyTests(unittest.TestCase):
    def test_seed_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'economy.sqlite'
            s=e.init(path)
            self.assertEqual(s['reserve_cents'],25000)
            self.assertEqual(s['remaining_cents'],100000)
            self.assertEqual(sum(a['productive_cents'] for a in s['agents']),60000)
            self.assertEqual(sum(a['operating_cents'] for a in s['agents']),15000)
            with self.assertRaises(ValueError): e.init(path)
            with closing(e.connect(path)) as c,c:
                c.execute("UPDATE economy_accounts SET cents=cents+1 WHERE name='reserve'")
            with self.assertRaisesRegex(ValueError,'reconcile'): e.status(path)

    def test_prepaid_failure_and_conservation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'economy.sqlite'; e.init(path)
            with closing(e.connect(path)) as c:
                def fail(*args):
                    self.assertEqual(e.status(path)['spent_cents'],175)
                    raise RuntimeError('offline')
                with self.assertRaisesRegex(RuntimeError,'offline'):
                    e.execute_tick(c,e.DEFAULT,report(),lambda:[],fail,lambda _:None,'qwen',None,None)
            s=e.status(path); self.assertEqual(s['spent_cents'],175)
            self.assertEqual(s['recent_tasks'][0]['state'],'failed')
            self.assertEqual(s['recent_tasks'][0]['agent'],'alpha:1')
            self.assertFalse(s['self_sustaining'])
            with closing(e.connect(path)) as c:
                e.execute_tick(c,e.DEFAULT,report(),lambda:[],lambda *_:{'completed':0},lambda _:None,'qwen',100,0.15)
            s=e.status(path); a=next(a for a in s['agents'] if a['id']=='alpha:1')
            self.assertEqual(a['state'],'conserving')
            self.assertGreaterEqual(s['recent_tasks'][0]['report']['estimated_energy_usd'],0)
            with closing(e.connect(path)) as c:
                r=e.execute_tick(c,e.DEFAULT,report(),lambda:[],lambda *_:{'completed':0},lambda _:None,'qwen',None,None)
                self.assertNotEqual(r.get('agent_id'),'alpha:1')

    def test_retirement_replacement_finite_reserve(self):
        cfg=dict(e.DEFAULT,initial_cents=330,productive_cents=80,operating_cents=20,research_cents=10,upkeep_cents=10)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'economy.sqlite'; e.init(path,cfg)
            with closing(e.connect(path)) as c:
                for tick in range(1,5):
                    with c: e.upkeep(c,cfg,tick)
            s=e.status(path); old=[a for a in s['agents'] if a['generation']==1]
            self.assertTrue(all(a['state']=='retired' for a in old))
            children=[a for a in s['agents'] if a['generation']==2]
            self.assertEqual(len(children),2)  # $2.70 reserve; $1 per new agent.
            self.assertTrue(all(a['policy']=='deterministic' and a['predecessor'] for a in children))
            self.assertEqual(s['reserve_cents'],70)
            self.assertEqual(s['remaining_cents'],270)
            self.assertTrue(s['reconciled'])

    def test_recovery_retains_fee(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'economy.sqlite'; e.init(path)
            with closing(e.connect(path)) as c,c:
                c.execute("INSERT INTO economy_ticks VALUES(1,'running','research')")
                e.transfer(c,1,'alpha:1:operating','spent',100,'research','task')
                c.execute("INSERT INTO economy_tasks VALUES('task',1,'alpha:1','running',100,'{}')")
            with closing(e.connect(path)) as c,c: e.recover(c)
            s=e.status(path); self.assertEqual(s['spent_cents'],100)
            self.assertEqual(s['recent_tasks'][0]['state'],'interrupted')

    def test_existing_expenses_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'expenses.sqlite'
            with closing(sqlite3.connect(p)) as c,c:
                c.execute('CREATE TABLE operating_expenses(category TEXT,cents INTEGER)')
                c.execute("INSERT INTO operating_expenses VALUES('electricity',123)")
            before=p.read_bytes(); self.assertEqual(e.actual_expenses(p)['actual_expenses_usd'],1.23)
            self.assertEqual(p.read_bytes(),before)

    def test_real_research_integration_and_resume(self):
        from autarky_trade import research_tree as tree, horizon_review as hr
        from autarky_trade.core import Bar, synthetic
        btc=synthetic(721+720*15); eth=[Bar(b.ts,b.open*2,b.high*2,b.low*2,b.close*2,b.volume) for b in btc]
        parts={'XBTUSD':[btc],'ETHUSD':[eth]}; cutoff=btc[-1].ts
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'tree.sqlite'; economy=Path(d)/'economy.sqlite'
            tree.run(db,parts,cutoff,model=None,agents=('alpha',),cross_asset=True,emit=lambda _:None)
            review=json.loads(json.dumps(hr.review(hr.attribution.load_nodes(db,[1]),parts,cutoff,horizons=(72,336))))
            e.init(economy)
            for _ in range(2):
                r=e.run(economy,db,review,parts,cutoff,model=None,ticks=1,emit=lambda _:None)
                self.assertEqual(r['ticks'][0]['completed'],1)
            s=e.status(economy); self.assertEqual(s['tick'],2); self.assertEqual(s['spent_cents'],350)
            nodes=tree.status(db)['nodes']; self.assertEqual(len(nodes),3)
            for n in nodes[:2]: self.assertEqual(n['allocation']['economic_agent'],'alpha:1')
            self.assertEqual(len({n['allocation']['economic_task'] for n in nodes[:2]}),2)

if __name__=='__main__': unittest.main()

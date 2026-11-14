import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from autarky_trade import research_allocator as a


def market(stress, delta, fills=20, active=8):
    return {'net':stress+.001,'stress':stress,'fills':fills,'active_windows':active,'windows':82,
            'drawdown':.06,'rmse':.1,'vs_mean':{'mean_net':delta,'mean_stress':delta,'rmse':-.001},
            'vs_parent':{'mean_net':delta,'mean_stress':delta,'rmse':-.001}}


def report():
    rows=[]
    for ident,h,markets in [(6,336,{'XBTUSD':market(-.00532,-.00086),'ETHUSD':market(-.00028,.00361)}),
                            (8,168,{'XBTUSD':market(-.00018,-.00018),'ETHUSD':market(-.00233,-.00017)}),
                            (7,24,{p:market(0,0,0,0) for p in a.PAIRS})]:
        rows.append({'id':ident,'agent':'alpha' if ident==6 else 'gamma','horizon_hours':h,'spec':{'id':ident},'markets':markets})
    return {'protocol':a.HORIZON_PROTOCOL,'development_only':True,'eligible':False,'dataset':'d','cutoff':100,'results':rows}


class AllocatorTests(unittest.TestCase):
    def test_activity_and_market_specific_priority(self):
        r=report(); choice=a.choose(r,[])
        self.assertEqual((choice['seed_id'],choice['horizon_hours'],choice['target_market']),(6,336,'ETHUSD'))
        self.assertEqual(choice['selection'],'exploitation')
        self.assertEqual(a.quality(market(0,0,0,0))[1],'inactive')
        self.assertEqual(a.quality(market(.01,.01,4,2))[1],'sparse')
        self.assertEqual(a.rank(r,[])[0][-1]['activity'],'inactive')

    def test_failures_reduce_priority_and_exploration_persists(self):
        r=report(); first=a.choose(r,[]); hist=[]
        for _ in range(3):
            choice=a.choose(r,hist); hist.append(({'allocation':choice},None))
        self.assertEqual(a.choose(r,hist)['selection'],'exploration')
        arms,_=a.rank(r,hist); original=next(x for x in arms if x['arm']==first['arm'])
        self.assertGreater(original['failures'],0)
        self.assertLess(original['priority'],first['priority'])
        self.assertEqual(a.choose(r,hist)['attempts'],0)

    def test_child_replaces_parent_only_when_active_and_better(self):
        r=report(); first=a.choose(r,[])
        node={'id':42,'status':'complete','spec':{'id':42},'findings':{'markets':{'XBTUSD':market(-.009,-.001),'ETHUSD':market(.001,.002)}}}
        choice=a.choose(r,[({'allocation':first},node)])
        self.assertEqual(choice['parent'],'node:42')
        self.assertEqual(choice['parent_evidence']['markets']['ETHUSD']['stress'],.001)
        node['findings']['markets']['ETHUSD']=market(0,0,0,0)
        arms,_=a.rank(r,[({'allocation':first},node)])
        original=next(x for x in arms if x['arm']==first['arm'])
        self.assertEqual(original['parent'],'node:6')

    def test_key_and_duplicate_validation(self):
        r=report(); other=copy.deepcopy(r); other['results'].reverse()
        self.assertEqual(a.allocation_key(r),a.allocation_key(other))
        other['results'].append(other['results'][0])
        with self.assertRaises(ValueError): a.seed_arms(other)
        r['eligible']=True
        with self.assertRaises(ValueError): a.seed_arms(r)

    def test_persisted_history(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'tree.sqlite'; c=sqlite3.connect(p)
            c.execute('CREATE TABLE tree_runs(id INTEGER,status TEXT,report TEXT)')
            c.execute('CREATE TABLE tree_nodes(id INTEGER,run INTEGER,status TEXT,report TEXT)')
            choice=a.choose(report(),[])
            c.execute('INSERT INTO tree_runs VALUES(1,\'complete\',?)',(json.dumps({'allocation':choice}),))
            c.execute('INSERT INTO tree_nodes VALUES(5,1,\'complete\',?)',(json.dumps({'spec':{'id':5},'findings':{'markets':{p:market(-.001,0) for p in a.PAIRS}}}),))
            c.execute('INSERT INTO tree_runs VALUES(2,\'complete\',\'{}\')'); c.commit(); c.close()
            before=p.read_bytes(); h=a.history(p)
            self.assertEqual(len(h),1); self.assertEqual(h[0][1]['id'],5)
            self.assertEqual(p.read_bytes(),before)
            self.assertEqual(a.rank(report(),h)[1],1)

    def test_real_ledger_allocation_and_resume(self):
        from autarky_trade import research_tree as tree, horizon_review as hr
        from autarky_trade.core import Bar, synthetic
        btc=synthetic(721+720*15)
        eth=[Bar(b.ts,b.open*2,b.high*2,b.low*2,b.close*2,b.volume) for b in btc]
        parts={'XBTUSD':[btc],'ETHUSD':[eth]}; cutoff=btc[-1].ts
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'tree.sqlite'; events=[]
            seed=tree.run(path,parts,cutoff,model=None,agents=('alpha',),cross_asset=True,emit=events.append)
            self.assertEqual(seed['completed'],1)
            review=hr.review(hr.attribution.load_nodes(path,[1]),parts,cutoff,horizons=(72,336))
            review=json.loads(json.dumps(review))
            a.validate(review,path,parts,cutoff)
            bad=copy.deepcopy(review); bad['results'][0]['markets']['ETHUSD']['stress']+=.01
            with self.assertRaisesRegex(ValueError,'metrics'):
                a.validate(bad,path,parts,cutoff)
            first=a.run(path,review,parts,cutoff,model=None,slots=1,emit=events.append)
            self.assertEqual(first['completed'],1)
            self.assertEqual(first['allocations'][0]['slot'],1)
            second=a.run(path,review,parts,cutoff,model=None,slots=1,emit=events.append)
            self.assertEqual(second['completed'],1)
            self.assertEqual(second['allocations'][0]['slot'],2)
            status=tree.status(path); self.assertEqual(len(status['nodes']),3)
            self.assertEqual(len(a.history(path)),2)
            for n in status['nodes'][:2]:
                self.assertEqual(n['parent'],n['allocation']['parent'])
                self.assertEqual(n['horizon_hours'],n['allocation']['horizon_hours'])
                self.assertEqual(n['agent'],n['allocation']['agent'])
                self.assertFalse(n['eligible'])
            self.assertEqual(len([e for e in events if e['event']=='allocate']),2)

if __name__=='__main__': unittest.main()

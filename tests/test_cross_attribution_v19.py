import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from autarky_trade import cross_attribution as a


def metrics(stress):
    return dict(mean_net=stress+.001,mean_stress=stress,rmse=.1,mean_exposure=.2,
                fills=10,active_windows=3,worst_drawdown=.04)


def report(parent=-.01):
    return dict(dataset_sha256="btc",other_dataset_sha256="eth",pair="XBTUSD",cutoff=100,
                windows=1,protocol=a.CROSS_MODE,details=[dict(start=10,train_last_label_end=9)],
                summary=dict(candidate=metrics(-.003),parent=metrics(parent),mean=metrics(-.006)))


class AttributionTests(unittest.TestCase):
    def test_matched_reference_and_baselines(self):
        m=a.market_metrics(report(),report(-.004))
        self.assertAlmostEqual(m["vs_reference"]["mean_stress"],.001)
        self.assertAlmostEqual(m["vs_parent"]["mean_stress"],.007)
        self.assertAlmostEqual(m["vs_mean"]["mean_stress"],.003)
        self.assertEqual(m["reference_type"],"matched_local_ablation")
        self.assertEqual(a.market_metrics(report())["reference_type"],"historical_mean_only")

    def test_replay_mismatch(self):
        for key in ("dataset_sha256","other_dataset_sha256","pair","cutoff","windows","protocol"):
            r=report(); r[key]="changed"
            with self.assertRaises(ValueError): a.market_metrics(report(),r)
        r=report(); r["summary"]["candidate"]["mean_stress"]=0
        with self.assertRaises(ValueError): a.market_metrics(report(),r)
        r=report(); r["details"][0]["start"]=11
        with self.assertRaises(ValueError): a.market_metrics(report(),r)

    def test_read_only_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"tree.sqlite"
            c=sqlite3.connect(p)
            c.execute("CREATE TABLE tree_nodes(id INTEGER,dataset TEXT,parent TEXT,agent TEXT,status TEXT,report TEXT)")
            r={"research_mode":a.CROSS_MODE,"results":{p:report() for p in a.PAIRS}}
            c.execute("INSERT INTO tree_nodes VALUES(1,'d','p','alpha','complete',?)",(json.dumps(r),))
            c.execute("INSERT INTO tree_nodes VALUES(2,'d','p','beta','failed',?)",(json.dumps(r),))
            c.commit(); c.close()
            before=p.read_bytes()
            self.assertEqual(a.load_nodes(p,[1])[0]["agent"],"alpha")
            self.assertEqual(p.read_bytes(),before)
            for ids in ([2],[3],[1,1],[]):
                with self.assertRaises(ValueError): a.load_nodes(p,ids)

if __name__=="__main__": unittest.main()

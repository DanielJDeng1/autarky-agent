import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from autarky_trade import autonomous_research as a, research_dashboard as dashboard
from autarky_trade.core import Bar, synthetic


class AutonomousTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        b = synthetic(721+720*8)
        e = [Bar(x.ts,x.open*2,x.high*2,x.low*2,x.close*2,x.volume*(1+i%5)) for i,x in enumerate(b)]
        cls.parts = {'XBTUSD':[b], 'ETHUSD':[e]}
        cls.cutoff = b[-1].ts

    def test_matched_tools_and_models(self):
        for exp in a.EXPERIMENTS:
            report = a.experiment(self.parts['XBTUSD'],'XBTUSD',self.cutoff,exp)
            self.assertEqual(set(report['summary']),{'mean',exp.control,exp.name})
            self.assertFalse(report['eligible'])
            self.assertEqual(report['summary'][exp.name]['windows'],2)
            self.assertEqual(report['summary'][exp.name]['observations'],8)
            for w in report['details']:
                self.assertLess(w['train_last_label_end'],w['start'])
                for m in w['models'].values():
                    if m['calibration_last_label_end'] is not None:
                        self.assertLess(m['calibration_last_label_end'],w['start'])

    def test_resume_shared_evidence_and_exhaustion(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'research.sqlite'; events=[]; decisions=[]
            def select(model,agent,options,context):
                decisions.append((agent,len(context['recent_experiments'])))
                return options[0], 'test hypothesis'
            with patch.object(a,'choose',side_effect=select):
                r=a.run(path,self.parts,self.cutoff,None,1,1,emit=events.append)
                self.assertEqual(r['completed'],3)
                self.assertEqual(decisions,[('alpha',0),('beta',1),('gamma',2)])
                r=a.run(path,self.parts,self.cutoff,None,1,0,emit=events.append)
                self.assertEqual(r['completed'],6)
                self.assertEqual(r['status'],'exhausted')
                r=a.run(path,self.parts,self.cutoff,None,1,1,emit=events.append)
                self.assertEqual(r['completed'],0)
            report=a.status(path)
            self.assertEqual(len(report['experiments']),9)
            self.assertTrue(all(e['status']=='complete' for e in report['experiments']))
            self.assertTrue(all(set(e['summary'])==set(a.PAIRS) for e in report['experiments']))
            self.assertTrue(any(e.get('cached') for e in events if e['event']=='baseline'))
            self.assertIn('momentum_only',dashboard.render(report))
            self.assertIn('Development only',dashboard.render(report))
            self.assertFalse(report['eligible'])

    def test_invalid_choice_retries_and_fails(self):
        options=[a.EXPERIMENTS[0],a.EXPERIMENTS[1]]
        responses=[{'message':{'content':'{"choice":99,"reason":"bad"}'}},
                   {'message':{'content':'{"choice":"1","reason":"test"}'}}]
        with patch.object(a,'request',side_effect=responses) as p:
            choice,reason=a.choose('qwen3:4b','alpha',options,{})
            self.assertEqual(choice,options[1]); self.assertEqual(reason,'test'); self.assertEqual(p.call_count,2)
        with patch.object(a,'request',return_value={'message':{'content':'{"choice":-1}'}}):
            with self.assertRaises(ValueError): a.choose('qwen3:4b','alpha',options,{})

    def test_interruption_and_database_guard(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'research.sqlite'
            with closing(a.connect(path)) as c,c:
                rid=c.execute("INSERT INTO model_runs(dataset,created,status,report) VALUES('d',0,'running','{}')").lastrowid
                c.execute("INSERT INTO model_experiments(run,dataset,name,agent,status,report) VALUES(?, 'd','one','alpha','running','{}')",(rid,))
                c.execute("INSERT INTO model_experiments(run,dataset,name,agent,status,report) VALUES(?, 'd','two','alpha','complete','{}')",(rid,))
                a.recover(c)
                self.assertEqual(c.execute('SELECT status FROM model_runs').fetchone()[0],'interrupted')
                self.assertEqual(json.loads(c.execute('SELECT report FROM model_runs').fetchone()[0])['completed'],1)
                self.assertEqual([x[0] for x in c.execute('SELECT status FROM model_experiments ORDER BY id')],['interrupted','complete'])
            other=Path(d)/'paper.sqlite'
            with closing(sqlite3.connect(other)) as c,c: c.execute('CREATE TABLE agents(id INTEGER)')
            with self.assertRaises(ValueError): a.connect(other)

    def test_read_only_dashboard_escape_and_missing_database(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'missing.sqlite'
            with self.assertRaises(sqlite3.Error): a.status(path)
            self.assertFalse(path.exists())
            data={'experiments':[{'id':1,'agent':'alpha','name':'test','status':'failed','reason':'<script>bad</script>'}], 'runs':[], 'baseline':None}
            page=dashboard.render(data)
            self.assertIn('&lt;script&gt;bad&lt;/script&gt;',page)
            self.assertNotIn('<script>bad</script>',page)


if __name__=='__main__': unittest.main()

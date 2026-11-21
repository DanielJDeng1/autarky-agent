import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from autarky_trade import lab, research_agent as r
from autarky_trade.core import synthetic


class ResearchAgentTests(unittest.TestCase):
    def setUp(self):
        self.bars=synthetic(1800)
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=str(Path(self.tmp.name)/'memory.sqlite')
        self.end=self.bars[-1].ts

    def run_session(self,agent='alpha',budget=2,model=None):
        return r.session(self.db,self.bars,'XBTUSD',self.end,agent,model,budget,3)

    def test_matched_allocation_in_lab_proposal(self):
        p=asdict(lab.defaults()[0]); p['fraction']=.01
        with patch.object(lab,'request',return_value={'message':{'content':json.dumps({'policies':[p]})}}):
            self.assertEqual(lab.propose('qwen3:4b',self.bars)[0].fraction,.25)

    def test_variants_change_exactly_one_field(self):
        for p in lab.defaults():
            for _,v in r.variants(p):
                self.assertEqual(v.fraction,.25)
                self.assertEqual(sum(a!=b for a,b in zip(asdict(p).values(),asdict(v).values())),1)

    def test_signal_warmup_and_nonoverlap(self):
        a=r.signal_analysis(self.bars,'momentum',24,24)
        b=r.signal_analysis(self.bars[:1000],'momentum',24,24)
        self.assertEqual(a['boundaries'],b['boundaries'])
        self.assertEqual(sum(x['count'] for x in a['buckets']),len(range(lab.WARMUP,len(self.bars)-24,24)))
        self.assertEqual(a['warmup_end'],self.bars[720].ts)

    def test_forecast_coverage_monotone(self):
        a=r.forecast_analysis(self.bars,3)
        c=list(a['threshold_coverage'].values())
        self.assertEqual(c,sorted(c,reverse=True))
        self.assertEqual(len(a['windows']),3)

    def test_session_budget_and_memory(self):
        a=self.run_session(); b=self.run_session()
        self.assertEqual(a['completed_trials'],2)
        self.assertEqual(b['completed_trials'],2)
        self.assertFalse(a['eligible'])
        self.assertEqual(a['dataset_sha256'],b['dataset_sha256'])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM trials').fetchone()[0],4)
        with closing(r.connect(self.db)) as c:
            self.assertEqual(len(r.memory(c,a['dataset_sha256'],3)),4)
        for t in a['trials']+b['trials']:
            self.assertEqual(t['control']['policy']['fraction'],t['variant']['policy']['fraction'])
            self.assertEqual(len(t['comparison']['window_net_deltas']),3)

    def test_model_receives_shared_evidence_and_retries(self):
        self.run_session('alpha',1)
        requests=[]
        def respond(url,payload):
            requests.append(payload)
            return {'message':{'content':'' if len(requests)==1 else json.dumps({'choice':0,'reason':'test shared evidence'})}}
        with patch.object(r,'request',side_effect=respond):
            b=self.run_session('gamma',1,'qwen3:4b')
        self.assertEqual(b['completed_trials'],1)
        self.assertEqual(len(requests),2)
        self.assertIn('recent_shared_trials',requests[0]['messages'][0]['content'])
        self.assertIn('alpha',requests[0]['messages'][0]['content'])
        self.assertFalse(requests[0]['think'])

    def test_second_model_decision_sees_first_trial(self):
        requests=[]
        def respond(url,payload):
            requests.append(payload['messages'][0]['content'])
            return {'message':{'content':json.dumps({'choice':0,'reason':'Compare measured costs'})}}
        with patch.object(r,'request',side_effect=respond):
            result=self.run_session('alpha',2,'qwen3:4b')
        self.assertEqual(len(requests),2)
        self.assertEqual(result['completed_trials'],2)
        first=result['trials'][0]
        self.assertIn(first['variant']['identity'][:12],requests[1])
        self.assertIn('window_net_deltas',requests[1])
        self.assertNotIn(first['variant']['identity'][:12],requests[0])

    def test_choice_json_mode_retry_and_numeric_string(self):
        options=[{'control':lab.defaults()[0],'change':'hold'}]
        replies=[{'message':{'content':''},'done_reason':'length'},
                 {'message':{'content':'{"choice":"0","reason":"Compare turnover"}'}}]
        with patch.object(r,'request',side_effect=replies) as call:
            selected,reason=r.choose('qwen3:4b','alpha',options,{})
        self.assertIs(selected,options[0]); self.assertEqual(reason,'Compare turnover')
        self.assertEqual(call.call_args_list[1].args[1]['format'],'json')

    def test_choice_only_retry_is_explicit(self):
        options=[{'control':lab.defaults()[0],'change':'hold'}]
        with patch.object(r,'request',return_value={'message':{'content':'{"choice":0}'}}) as call:
            selected,reason=r.choose('qwen3:4b','alpha',options,{})
        self.assertIs(selected,options[0]); self.assertIn('no rationale',reason)
        self.assertEqual(call.call_count,3)

    def test_choice_failure_reports_diagnostics(self):
        options=[{'control':lab.defaults()[0],'change':'hold'}]
        with patch.object(r,'request',return_value={'message':{'content':''},'done_reason':'length','eval_count':2048}):
            with self.assertRaisesRegex(ValueError,'done_reason=length; eval_count=2048'):
                r.choose('qwen3:4b','alpha',options,{})

    def test_interrupt_is_recorded(self):
        original=lab.evaluate
        count=0
        def interrupt(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2: raise KeyboardInterrupt()
            return original(*args,**kwargs)
        with patch.object(lab,'evaluate',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_session()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT status FROM trials').fetchone()[0],'pending')
            self.assertEqual(c.execute('SELECT status FROM sessions').fetchone()[0],'interrupted')
        self.assertEqual(self.run_session(budget=1)['completed_trials'],1)

    def test_invalid_model_choice_fails_without_trial(self):
        with patch.object(r,'request',return_value={'message':{'content':'{"choice":999,"reason":"invalid"}'}}):
            with self.assertRaises(ValueError): self.run_session(model='qwen3:4b')
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM trials').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT status FROM sessions').fetchone()[0],'failed')

    def test_existing_db_not_mutated(self):
        lab.run(self.db,self.bars,'XBTUSD',self.end,[lab.defaults()[0]],folds=3)
        with self.assertRaises(ValueError): self.run_session()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM experiments').fetchone()[0],1)

    def test_failed_attempt_is_preserved(self):
        original=lab.evaluate
        count=0
        def fail(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2: raise RuntimeError('simulated failure')
            return original(*args,**kwargs)
        with patch.object(lab,'evaluate',side_effect=fail):
            with self.assertRaises(RuntimeError): self.run_session()
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute('SELECT status FROM trials').fetchone()[0],'failed')
            self.assertEqual(c.execute('SELECT status FROM sessions').fetchone()[0],'failed')
        self.assertEqual(self.run_session(budget=1)['completed_trials'],1)

    def test_evaluation_is_separate_from_recording(self):
        p=lab.defaults()[0]
        a=lab.evaluate(self.bars,'XBTUSD',self.end,[p],folds=3)
        b=lab.run(self.db,self.bars,'XBTUSD',self.end,[p],folds=3)
        self.assertEqual(a['results'],b['results'])
        self.assertFalse(a['eligible'])

if __name__=='__main__': unittest.main()

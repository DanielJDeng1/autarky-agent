import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from autarky_trade import archive, community as m, dashboard
from autarky_trade.core import Strategy, synthetic, Costs
from autarky_trade.signals import feature_value
from autarky_trade.benchmarks import reference

class V04(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'test.sqlite'); m.init(self.path)
        self.bars=synthetic(); self.now=self.bars[-1].ts+3600
    def tearDown(self): self.tmp.cleanup()
    def test_archive_dedup_and_restart(self):
        self.assertEqual(archive.ingest(self.path,self.bars,'csv',self.now)['inserted'],1000)
        self.assertEqual(archive.ingest(self.path,self.bars,'kraken',self.now)['inserted'],0)
        self.assertEqual(archive.latest(self.path),self.bars)
    def test_archive_conflict_atomic(self):
        archive.ingest(self.path,self.bars,'csv',self.now)
        bad=list(self.bars); bad[-1]=replace(bad[-1],volume=99)
        with self.assertRaises(ValueError): archive.ingest(self.path,bad,'kraken',self.now)
        self.assertEqual(archive.latest(self.path),self.bars)
    def test_archive_gap_and_future(self):
        archive.ingest(self.path,self.bars,'csv',self.now)
        shifted=[replace(b,ts=b.ts+2000*3600) for b in self.bars]
        with self.assertRaises(ValueError): archive.ingest(self.path,shifted,'csv',self.now)
        archive.ingest(self.path,shifted,'csv',self.now+2000*3600)
        self.assertEqual(archive.info(self.path)['count'],2000); self.assertEqual(archive.latest(self.path),shifted)
    def test_archive_synthetic_rejected(self):
        with self.assertRaises(ValueError): archive.ingest(self.path,self.bars,'synthetic',self.now)
    def test_signal_feature_and_filter(self):
        x=[100+i for i in range(120)]; self.assertEqual(feature_value('rsi',10,x),100)
        self.assertGreater(feature_value('momentum',10,x),0)
        s=Strategy('trend',2,10,'test',[dict(feature='rsi',window=10,op='lt',threshold=70)])
        self.assertFalse(s.signal(x)); self.assertTrue(Strategy('trend',2,10,'plain').signal(x))
        self.assertEqual(Strategy(**json.loads(json.dumps(asdict(s)))),s)
        with self.assertRaises(ValueError): Strategy('trend',2,10,'bad',[dict(feature='shell',window=10,op='gt',threshold=1)])
        with self.assertRaises(ValueError): Strategy('trend',2,10,'bad',[dict(feature='rsi',window=10,op='gt',threshold=float('nan'))])
    def test_legacy_identity_and_filter_order(self):
        plain=json.dumps(dict(kind='trend',fast=2,slow=10,name='old'))
        self.assertTrue(m.same_strategy(plain,json.dumps(asdict(Strategy('trend',2,10,'new')))))
        a=dict(feature='rsi',window=10,op='lt',threshold=70); b=dict(feature='momentum',window=10,op='gt',threshold=0)
        self.assertTrue(m.same_strategy(json.dumps(asdict(Strategy('trend',2,10,'a',[a,b]))),json.dumps(asdict(Strategy('trend',2,10,'b',[b,a])))))
    def test_model_filters_and_no_holdout_in_prompt(self):
        response={'message':{'content':json.dumps({'strategies':[{'fast':3,'slow':11,'filters':[dict(feature='rsi',window=10,op='lt',threshold=70)]}]})}}
        with patch.object(m,'request',return_value=response) as req:
            c=m.candidates('trend','qwen',self.bars[:400],'alpha',[])
            self.assertEqual(c[-1].filters[0]['feature'],'rsi'); self.assertIn('Training-only',req.call_args.args[1]['messages'][0]['content'])
    def test_benchmark_deterministic(self):
        s=Strategy('trend',5,20,'a'); a=reference(self.bars,s,Costs(),800,1000,600)
        self.assertEqual(a,reference(self.bars,s,Costs(),800,1000,600)); self.assertEqual(a['random_seeds'],31)
        self.assertGreaterEqual(a['random_p75'],a['random_median'])
    def test_model_retries_empty_thinking_answer(self):
        empty={'message':{'content':'','thinking':'reasoning'},'done_reason':'length'}
        valid={'message':{'content':json.dumps({'strategies':[{'fast':3,'slow':11}]})}}
        with patch.object(m,'request',side_effect=[empty,valid]) as req:
            c=m.candidates('trend','qwen3:4b',self.bars[:400],'alpha',[])
        self.assertEqual(c[-1].fast,3)
        self.assertEqual([call.args[1]['options']['num_predict'] for call in req.call_args_list],[2048,4096])
        self.assertTrue(all(call.args[1]['think'] is False for call in req.call_args_list))
    def test_model_invalid_response_fails_explicitly(self):
        with patch.object(m,'request',return_value={'message':{'content':'{"strategies":null}'}}) as req:
            with self.assertRaisesRegex(RuntimeError,'no valid proposal JSON for alpha'):
                m.candidates('trend','qwen3:4b',self.bars[:400],'alpha',[])
        self.assertEqual(req.call_count,2)
    def test_sync_proposal_failure_preserves_holdout(self):
        with patch.object(m,'candidates',side_effect=RuntimeError('model down')), patch.object(m,'assess') as assess:
            with self.assertRaises(RuntimeError): m.research(self.path,self.bars,'csv',now=self.now)
            assess.assert_not_called()
        self.assertEqual(m.config(self.path)['last_holdout'],0)
    def test_sync_assessment_failure_consumes_holdout(self):
        with patch.object(m,'assess',side_effect=RuntimeError('evaluation failed')):
            with self.assertRaises(RuntimeError): m.research(self.path,self.bars,'csv',now=self.now)
        self.assertEqual(m.config(self.path)['last_holdout'],self.bars[-1].ts)
        self.assertEqual(m.research(self.path,self.bars,'csv',now=self.now)['status'],'waiting for new unseen data')
    def test_dashboard(self):
        data=dashboard.snapshot(self.path); self.assertEqual(data['portfolio']['equity'],1000)
        data['portfolio']['agents'][0]['id']='<script>'
        page=dashboard.render(data); self.assertIn('&lt;script&gt;',page); self.assertNotIn('<script>',page)

if __name__=='__main__': unittest.main()

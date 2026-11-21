"""v0.30 cost-replay, provenance, and autonomous economic-readiness regression tests."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import copy
import hashlib
import json
import math
import sys
import types
import unittest

from autarky_trade import economic_policy as policy, economic_review as economics


@dataclass
class Bar:
    ts: int
    open: float


@dataclass
class Costs:
    fee: int
    slip: int
    fraction: float


def paper_account(bars,samples,selected,costs):
    n=len(selected)
    net=n*.03 - (2*(costs.fee+costs.slip)/10000 if n else 0)
    return {'net':net,'fills':2*n,'drawdown':.05 if n else 0., 'exposure':n/30}


class ReviewTests(unittest.TestCase):
    def sample(self):
        bars=[Bar(i*3600,100.+i) for i in range(722)]
        start=bars[2].ts; horizon=24
        samples=[{'start':bars[i].ts,'label_end':bars[i+horizon].ts,
                  'realized':bars[i+horizon].open/bars[i].open-1,
                  'prediction':.5 if j==0 else -.5}
                 for j,i in enumerate(range(2,722-horizon,horizon))]
        models={}
        for name in ('candidate','parent','mean'):
            t=.1 if name=='candidate' else 2.0
            selected={i for i,r in enumerate(samples) if r['prediction']>t}
            models[name]={'samples':samples,'threshold':t,'selected':len(selected),
                          'calibration_last_label_end':None,
                          'normal':paper_account(bars[2:722],samples,selected,Costs(40,20,.25)),
                          'stress':paper_account(bars[2:722],samples,selected,Costs(80,40,.25))}
        report={'dataset_sha256':hashlib.sha256(json.dumps(['XBTUSD',[[asdict(b) for b in bars]]],sort_keys=True).encode()).hexdigest(),'development_only':True,'eligible':False,'pair':'XBTUSD','horizon_hours':horizon,
                'cutoff':bars[-1].ts,'details':[{'start':start,'year':2026,
                          'train_last_label_end':start-3600,'models':models}]}
        return report,[bars]

    def fake_modules(self):
        fake={}
        for name in ('lab','weekly_review','weekly_models','core'):
            mod=types.ModuleType('autarky_trade.'+name);fake['autarky_trade.'+name]=mod
        fake['autarky_trade.lab'].WARMUP=2
        fake['autarky_trade.lab'].digest=lambda x:hashlib.sha256(json.dumps(x,sort_keys=True).encode()).hexdigest()
        fake['autarky_trade.weekly_review'].account=paper_account
        fake['autarky_trade.weekly_models'].horizon_slots=lambda h:29 if h==24 else (_ for _ in ()).throw(ValueError('invalid horizon'))
        fake['autarky_trade.core'].Costs=Costs
        return patch.dict(sys.modules,fake)

    def test_cost_replay_identical_trades_and_genuine_shortfall(self):
        report,bars=self.sample()
        with self.fake_modules(): r=policy.replay_directional(report,bars,24)
        self.assertEqual(r['scenarios']['legacy_normal']['candidate']['fills'],2)
        self.assertAlmostEqual(r['scenarios']['kraken_taker_proxy']['candidate']['mean_30d_net'],.01)
        self.assertTrue(r['profitable_after_assumed_costs'])
        self.assertFalse(r['historical_operating_surplus_positive'])
        self.assertFalse(r['eligible'])
        self.assertAlmostEqual(r['scenarios']['kraken_taker_proxy']['candidate']['required_annual_return'],.45625)

    def test_rejects_replaced_candles_even_when_labels_match(self):
        report,bars=self.sample(); bars[0][100].open+=.001
        with self.fake_modules(),self.assertRaisesRegex(ValueError,'fingerprint'):
            policy.replay_directional(report,bars,24)

    def test_integrity_rejects_tampered_labels(self):
        report,bars=self.sample(); report['details'][0]['models']['candidate']['samples'][0]['realized']+=.01
        with self.fake_modules(),self.assertRaisesRegex(ValueError,'labels'):
            policy.replay_directional(report,bars,24)

    def test_integrity_rejects_rewritten_saved_account(self):
        report,bars=self.sample(); report['details'][0]['models']['candidate']['normal']['net']+=.1
        with self.fake_modules(),self.assertRaisesRegex(ValueError,'replay mismatch'):
            policy.replay_directional(report,bars,24)

    def test_integrity_rejects_noncausal_training(self):
        report,bars=self.sample(); report['details'][0]['train_last_label_end']=report['details'][0]['start']
        with self.fake_modules(),self.assertRaisesRegex(ValueError,'noncausal'):
            policy.replay_directional(report,bars,24)

    def fixture(self,home):
        source=Path(home)/'history/horizon-review-v20.json';source.parent.mkdir(parents=True)
        source.write_text(json.dumps({'protocol':'v0.20','cutoff':100,'dataset':'fixture'}))
        def metrics(net):
            return {'candidate':{'mean_30d_net':net,'annual_simple_surplus_usd':200*net*365/30-91.25,'fills':10}}
        r={'protocol':economics.PROTOCOL,'development_only':True,'eligible':False,
           'source_report_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
           'cutoff':100,'dataset':'fixture','capital_usd':200.,'daily_operating_usd':.25,
           'results':[{'node':1,'agent':'alpha','pair':'ETHUSD','horizon_hours':336,'windows':12,
                       'scenarios':{'kraken_taker':metrics(.01),
                                    'legacy_stress':metrics(.001),
                                    'okx_us_proxy':metrics(.02)}}]}
        return source,r

    def test_authenticated_report_rank_and_prompt_bounds(self):
        with TemporaryDirectory() as d:
            source,r=self.fixture(d)
            (source.parent/'economic-review-v29.json').write_text(json.dumps(r))
            result=policy.load_review(d)
            self.assertTrue(result['valid'])
            self.assertEqual(result['positive_cost_adjusted'],1)
            self.assertEqual(result['positive_operating_surplus'],0)
            self.assertEqual(len(policy.compact(result)['ranked']),1)

    def test_reject_fabricated_surplus_and_duplicate_rows(self):
        with TemporaryDirectory() as d:
            source,r=self.fixture(d)
            destination=source.parent/'economic-review-v29.json'
            r['results'][0]['scenarios']['kraken_taker']['candidate']['annual_simple_surplus_usd']+=1000
            destination.write_text(json.dumps(r))
            out=policy.load_review(d)
            self.assertFalse(out['valid'])
            self.assertIn('reconcile',out['reason'])
            r['results'][0]['scenarios']['kraken_taker']['candidate']['annual_simple_surplus_usd']-=1000
            r['results'].append(copy.deepcopy(r['results'][0]))
            destination.write_text(json.dumps(r))
            out=policy.load_review(d)
            self.assertFalse(out['valid'])
            self.assertIn('Duplicate',out['reason'])

    def test_stale_source_is_rejected(self):
        with TemporaryDirectory() as d:
            source,r=self.fixture(d); (source.parent/'economic-review-v29.json').write_text(json.dumps(r))
            source.write_text('changed')
            self.assertFalse(policy.load_review(d)['valid'])

    def test_missing_review_does_not_invent_results(self):
        with TemporaryDirectory() as d:
            r=policy.load_review(d)
            self.assertFalse(r['available']);self.assertEqual(r['ranked'],[])

    def test_atomic_creation_and_idempotent_restart(self):
        with TemporaryDirectory() as d:
            source,r=self.fixture(d)
            with patch.object(economics,'replay',return_value={k:v for k,v in r.items() if k!='source_report_sha256'}) as replay:
                result=policy.create_review_if_missing(d,{'fake':'bars'},100)
                self.assertTrue(result['generated']);self.assertTrue(result['review']['valid'])
                result=policy.create_review_if_missing(d,{'fake':'bars'},100)
                self.assertFalse(result['generated']);replay.assert_called_once()
            self.assertFalse(list(source.parent.glob('*.partial')))

    def test_stale_report_preserved_not_overwritten(self):
        with TemporaryDirectory() as d:
            source,r=self.fixture(d)
            dest=source.parent/'economic-review-v29.json';dest.write_text('corrupt')
            with self.assertRaisesRegex(ValueError,'Existing review is invalid'):
                policy.create_review_if_missing(d,{},100)
            self.assertEqual(dest.read_text(),'corrupt')

if __name__=='__main__':unittest.main()

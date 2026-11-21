import csv
from dataclasses import asdict
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autarky_trade import community, final_eval as f, research
from autarky_trade.core import Strategy, Bar


class FinalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = self.dir/'final.sqlite'
        self.start = 1700002800  # UTC-hour aligned
        self.step = 3600
        self.end = self.start + (90*24-1)*self.step
        self.now = self.start-1
        self.btc = self.trial('XBTUSD')
        self.eth = self.trial('ETHUSD')

    def tearDown(self):
        self.tmp.cleanup()

    def trial(self, pair, eligible=True, source='csv'):
        path = self.dir/(pair+'.sqlite')
        community.init(path,pair=pair)
        s=Strategy('trend',5,20,'test strategy')
        report=dict(protocol=research.PROTOCOL,eligible=eligible,strategy=asdict(s),
                    strategy_id=research.identity(s),dataset_sha256='a'*64,candidates=[{}])
        with community.db(path) as c:
            c.execute('INSERT INTO rounds VALUES(1,?,?,?,?,?)', (self.now-1,source,self.start-200*self.step,self.start-102*self.step,1000))
            c.execute('INSERT INTO trials VALUES(1,?,?)',('alpha',json.dumps(report)))
            c.commit()
        return path

    def spec(self, two=True):
        legs=[dict(db=str(self.btc),agent='alpha',round_id=1,weight_bps=4000)]
        if two: legs.append(dict(db=str(self.eth),agent='alpha',round_id=1,weight_bps=4000))
        return dict(name='frozen portfolio',mode='prospective',start=self.start,end=self.end,legs=legs)

    def write_csv(self,pair,offset=0):
        path=self.dir/(pair+'.csv')
        p=100.
        with path.open('w',newline='') as fp:
            writer=csv.writer(fp)
            writer.writerow(['timestamp','open','high','low','close','volume'])
            for i in range(-f.WARMUP,90*24):
                ts=self.start+i*self.step+offset
                close=p*math.exp(.001*math.sin(i/9)+.0002)
                writer.writerow([ts,p,max(p,close),min(p,close),close,10])
                p=close
        return path

    def test_register_exact_weights_and_windows(self):
        r=f.freeze(self.ledger,self.spec(),self.now)
        self.assertEqual(r['state'],'registered')
        self.assertEqual(len(r['plan']['legs']),2)
        self.assertEqual(r['plan']['legs'][0]['source_completed_candidate_evaluations'],1)
        self.assertEqual(f.status(self.ledger)[0]['id'],r['id'])
        self.assertEqual(f.digest(f.canonical(r['plan']).encode()),r['id'])
        s=self.spec(False); s['start']+=3600; s['end']+=3600
        with self.assertRaisesRegex(ValueError,'overlapping'): f.freeze(self.ledger,s,self.now)

    def test_no_past_prospective_or_overlap_with_development(self):
        with self.assertRaisesRegex(ValueError,'after registration'): f.freeze(self.ledger,self.spec(),self.start)
        with community.db(self.btc) as c:
            c.execute('UPDATE treasury SET last_holdout=?',(self.start,)); c.commit()
        with self.assertRaisesRegex(ValueError,'overlaps known'): f.freeze(self.ledger,self.spec(),self.now)

    def test_requires_eligible_and_current_source_trial(self):
        with community.db(self.btc) as c:
            c.execute('UPDATE trials SET report=?',(json.dumps({'eligible':False}),)); c.commit()
        with self.assertRaisesRegex(ValueError,'eligible'): f.freeze(self.ledger,self.spec(),self.now)

    def test_weights_and_duplicate_market_fail(self):
        s=self.spec(); s['legs'][0]['weight_bps']=5000
        with self.assertRaisesRegex(ValueError,'20%'): f.freeze(self.ledger,s,self.now)
        s=self.spec(); s['legs'][1]['db']=str(self.btc)
        with self.assertRaisesRegex(ValueError,'one sleeve|One sleeve'): f.freeze(self.ledger,s,self.now)

    def test_future_data_not_opened_and_failure_consumes(self):
        key=f.freeze(self.ledger,self.spec(False),self.now)['id']
        with patch.object(f,'analyze') as analyze:
            with self.assertRaisesRegex(ValueError,'not complete'):
                f.evaluate(self.ledger,key,{'XBTUSD':'not-opened'},self.start)
            analyze.assert_not_called()
        self.assertEqual(f.status(self.ledger)[0]['state'],'registered')
        with self.assertRaises(FileNotFoundError): f.evaluate(self.ledger,key,{'XBTUSD':'missing'},self.end+self.step)
        self.assertEqual(f.status(self.ledger)[0]['state'],'failed')
        with self.assertRaisesRegex(ValueError,'consumed'): f.evaluate(self.ledger,key,{'XBTUSD':'missing'},self.end+self.step)

    def test_two_market_alignment_and_one_shot(self):
        key=f.freeze(self.ledger,self.spec(),self.now)['id']
        files={p:self.write_csv(p) for p in f.PAIRS}
        r=f.evaluate(self.ledger,key,files,self.end+self.step)['result']
        self.assertEqual(r['observations'],2160)
        self.assertAlmostEqual(r['portfolio']['initial_cash_weight'],.2)
        self.assertAlmostEqual(r['portfolio']['return'],.4*sum(x['active']['return'] for x in r['legs']))
        self.assertAlmostEqual(r['sleeve_log_return_correlation'],1.)
        self.assertEqual(f.status(self.ledger)[0]['state'],'completed')
        with self.assertRaisesRegex(ValueError,'consumed'): f.evaluate(self.ledger,key,files,self.end+self.step)

    def test_wrong_market_clock_and_sealed_digest(self):
        s=self.spec(False); s['mode']='sealed'; s['legs'][0]['csv_sha256']='0'*64
        key=f.freeze(self.ledger,s,self.end+self.step)['id']
        with self.assertRaisesRegex(ValueError,'digest mismatch'): f.evaluate(self.ledger,key,{'XBTUSD':self.write_csv('XBTUSD')},self.end+self.step)
        self.assertEqual(f.status(self.ledger)[0]['state'],'failed')
        # A different ledger is a different research experiment, not a reset in production.
        other=self.dir/'other.sqlite'; key=f.freeze(other,self.spec(False),self.now)['id']
        with self.assertRaisesRegex(ValueError,'exact registered interval'): f.evaluate(other,key,{'XBTUSD':self.write_csv('XBTUSD',1)},self.end+self.step)

    def test_sealed_result_never_review_eligible(self):
        path=self.write_csv('XBTUSD'); s=self.spec(False); s['mode']='sealed'; s['legs'][0]['csv_sha256']=f.digest(path.read_bytes())
        key=f.freeze(self.ledger,s,self.end+self.step)['id']
        r=f.evaluate(self.ledger,key,{'XBTUSD':path},self.end+self.step)['result']
        self.assertFalse(r['eligible_for_manual_review'])

    def test_tampered_plan_rejected_without_opening_data(self):
        key=f.freeze(self.ledger,self.spec(False),self.now)['id']
        c=f.connect(self.ledger)
        try:
            row=c.execute('SELECT plan FROM final_plans WHERE id=?',(key,)).fetchone(); plan=json.loads(row[0]); plan['legs'][0]['weight_bps']=1000
            c.execute('UPDATE final_plans SET plan=? WHERE id=?',(json.dumps(plan),key)); c.commit()
        finally: c.close()
        with patch.object(f,'analyze') as analyze:
            with self.assertRaisesRegex(ValueError,'integrity'): f.evaluate(self.ledger,key,{'XBTUSD':'unused'},self.end+self.step)
            analyze.assert_not_called()

    def test_portfolio_drawdown_and_drifting_weights(self):
        values,metrics=f.combine([[1,2,1],[1,1,1]],[.4,.4])
        for actual, expected in zip(values,[1,1.4,1]): self.assertAlmostEqual(actual,expected)
        self.assertAlmostEqual(metrics['drawdown'],1-1/1.4)
        self.assertAlmostEqual(metrics['maximum_sleeve_equity_weight'],.8/1.4)
        self.assertIsNone(f.correlation([0,0],[1,2]))


if __name__=='__main__': unittest.main()

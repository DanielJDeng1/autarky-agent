import unittest
from autarky_trade import forecast_audit as a, lab
from autarky_trade.core import Bar


class ForecastAuditTests(unittest.TestCase):
    def test_cost_and_abstention(self):
        self.assertLess(a.net_return(0,lab.NORMAL),0)
        self.assertLess(a.net_return(.02,lab.STRESS),a.net_return(.02,lab.NORMAL))
        self.assertIsNone(a.calibrate([{'prediction':.03,'realized':.02}]*23))
        self.assertIsNone(a.calibrate([{'prediction':.03,'realized':-.02}]*30))
        self.assertIsNotNone(a.calibrate([{'prediction':.03,'realized':.06}]*30))
    def test_chronological_and_gap(self):
        def bars(start,n):
            return [Bar((start+i)*3600,100+i*.02,101+i*.02,99+i*.02,100+i*.02,1) for i in range(n)]
        parts=[bars(0,2161),bars(3000,2161)]
        report=a.audit(parts,'XBTUSD',parts[-1][-1].ts,now=10**9)
        self.assertFalse(report['eligible'])
        for r in report['results']:
            self.assertEqual(len(r['folds']),4)
            self.assertIsNone(r['folds'][0]['threshold'])
            self.assertEqual(r['folds'][0]['calibration_observations'],0)
            for f in r['folds']:
                self.assertLess(f['training_last_label_end'],f['start'])
                if f['calibration_last_label_end'] is not None: self.assertLess(f['calibration_last_label_end'],f['start'])
            for x,y in zip(r['samples'],r['samples'][1:]): self.assertLessEqual(x['label_end'],y['start'])
            self.assertEqual(sum(x['observations'] for x in r['by_prior_regime'].values()),r['observations'])
        with self.assertRaises(ValueError): a.audit([parts[0]+parts[1]],'XBTUSD',parts[-1][-1].ts,now=10**9)

if __name__=='__main__': unittest.main()

import hashlib
import io
import unittest
from zipfile import ZipFile
from autarky_trade import funding_research as f, lab
from autarky_trade.core import synthetic


class FundingTests(unittest.TestCase):
    def test_archive_and_checksum(self):
        name='BTCUSDT-fundingRate-2024-01'; start,_=f.month_bounds('2024-01')
        buf=io.BytesIO()
        with ZipFile(buf,'w') as z:
            z.writestr(name+'.csv',f'calc_time,funding_interval_hours,last_funding_rate\n{start*1000+1},8,0.0001\n')
        data=buf.getvalue(); checksum=(hashlib.sha256(data).hexdigest()+'  '+name+'.zip').encode()
        rows,_=f.parse_archive(data,checksum,'BTCUSDT','2024-01')
        self.assertEqual(rows,[[start*1000+1,8,.0001]])
        with self.assertRaises(ValueError): f.parse_archive(data,b'bad','BTCUSDT','2024-01')

    def test_lag_staleness_and_interval(self):
        rows=[[1000,4,.001],[14401000,8,.003]]
        self.assertIsNone(f.asof(rows,3600))
        self.assertEqual(f.asof(rows,3601),.002)
        self.assertEqual(f.asof(rows,18000),.002)
        self.assertEqual(f.asof(rows,18001),.003)
        self.assertIsNone(f.asof(rows,60000))

    def test_missing_funding_splits_both_markets(self):
        b=synthetic(30); t=b[0].ts
        events=[[t*1000-3600000,8,.001],[(t+15*3600)*1000,8,.002]]
        out,values=f.matched({'XBTUSD':[b],'ETHUSD':[b]},{'XBTUSD':events,'ETHUSD':events})
        self.assertEqual([[x.ts for x in s] for s in out['XBTUSD']],[[x.ts for x in s] for s in out['ETHUSD']])
        self.assertEqual(len(out['XBTUSD']),2)
        self.assertNotIn(b[10].ts,values['XBTUSD'])

    def test_matched_review_and_future_funding_isolation(self):
        b=synthetic(721+720*15); t=b[0].ts; cutoff=b[-1].ts
        events=[[(t+(i-8)*3600)*1000,8,.0001 if i%16==0 else -.0002] for i in range(0,len(b)+16,8)]
        data={'protocol':f.PROTOCOL,'symbols':f.SYMBOLS,'events':{p:events for p in f.SYMBOLS}}
        data['events_sha256']=lab.digest(data['events'])
        a=f.review({p:[b] for p in f.SYMBOLS},data,cutoff,(168,336),now=cutoff+3600)
        self.assertEqual(len(a['results']),16)
        self.assertEqual({r['windows'] for r in a['results']},{3})
        self.assertFalse(a['eligible'])
        # Alter future observations: past as-of values and rolling features are identical.
        out,v=f.matched({p:[b] for p in f.SYMBOLS},data['events'])
        changed=[r if r[0]<b[2000].ts*1000 else [r[0],r[1],.05] for r in events]
        _,w=f.matched({p:[b] for p in f.SYMBOLS},{p:changed for p in f.SYMBOLS})
        self.assertEqual(f.feature_series(out['XBTUSD'][0][:2000],v['XBTUSD'],('funding_mean',168)),f.feature_series(out['XBTUSD'][0][:2000],w['XBTUSD'],('funding_mean',168)))

if __name__=='__main__': unittest.main()

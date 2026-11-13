import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
from autarky_trade import history_data as h, coordinator as c, lab, research_agent as a, research_review as review
from autarky_trade.core import synthetic

class V13(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.db=self.root/'memory.sqlite'; self.bars=synthetic(1800)
    def test_exact_pair_and_gaps(self):
        z=self.root/'market.zip'
        with zipfile.ZipFile(z,'w') as f:
            f.writestr('AIXBTUSD_60.csv','bad')
            f.writestr('nested/XBTUSD_60.csv','0,100,101,99,100,2,1\n7200,100,101,99,100,2,1\n')
        info=h.extract(z,self.root,'XBTUSD'); self.assertEqual(info['gaps'],1); self.assertEqual(info['contiguous_suffix'],1)
        self.assertEqual(info['missing_hours'],1); self.assertFalse(info['official_checksum_verified'])
    def test_duplicate_csv_rejected(self):
        p=self.root/'x.csv'; p.write_text('0,100,101,99,100,2\n0,100,101,99,100,2\n')
        with self.assertRaisesRegex(ValueError,'Duplicate'): h.inspect_csv(p)
    def test_checksums_require_all_parts(self):
        text='\n'.join('a'*64+'  '+p for p in h.PARTS)
        self.assertEqual(len(h.checksums(text)),5)
        with self.assertRaises(ValueError): h.checksums(text.splitlines()[0])
    def test_checksum_failure_does_not_replace(self):
        import io
        p=self.root/'data'; p.write_bytes(b'old')
        with patch.object(h.urllib.request,'urlopen',return_value=io.BytesIO(b'wrong')):
            with self.assertRaisesRegex(ValueError,'Checksum mismatch'): h.fetch('https://example.com',p,'a'*64)
        self.assertEqual(p.read_bytes(),b'old')
    def test_curl_fallback_checks_download(self):
        import hashlib
        import urllib.error
        from types import SimpleNamespace
        p=self.root/'data'
        def curl(args, **kwargs):
            Path(args[args.index('--output')+1]).write_bytes(b'good')
            return SimpleNamespace(returncode=0,stderr='')
        with patch.object(h.urllib.request,'urlopen',side_effect=urllib.error.HTTPError('https://example.com',403,'Forbidden',None,None)), patch.object(h.shutil,'which',return_value='curl'), patch.object(h.subprocess,'run',side_effect=curl):
            h.fetch('https://example.com',p,hashlib.sha256(b'good').hexdigest())
        self.assertEqual(p.read_bytes(),b'good')
    def test_manifest_unavailable_full_checksum_required(self):
        import hashlib
        import urllib.error
        parts=[b'one',b'two',b'three',b'four',b'five']
        def download(url,path,expected=None):
            if url.endswith(h.CHECKSUMS):
                raise urllib.error.HTTPError(url,403,'Forbidden',None,None)
            self.assertIsNone(expected)
            path.write_bytes(parts[h.PARTS.index(path.name)])
        with patch.object(h,'fetch',side_effect=download), patch.object(h,'FULL_SHA',hashlib.sha256(b''.join(parts)).hexdigest()):
            self.assertEqual(h.acquire(self.root).read_bytes(),b''.join(parts))
        (self.root/'Kraken_OHLCVT_Full.zip').unlink()
        (self.root/h.PARTS[0]).write_bytes(b'corrupt')
        with patch.object(h,'fetch',side_effect=download), patch.object(h,'FULL_SHA',hashlib.sha256(b''.join(parts)).hexdigest()):
            with self.assertRaisesRegex(ValueError,'published checksum'): h.acquire(self.root)
        self.assertFalse((self.root/'Kraken_OHLCVT_Full.zip').exists())
    def test_no_trade_menu_changes_only_entry(self):
        p=next(x for x in lab.defaults() if x.name=='reversion-recovery')
        metrics={'fills':0}; opts=a.ranked_variants(p,metrics,self.bars,[])
        for change,v in opts:
            self.assertIn('entry',change); self.assertEqual(v.exit,p.exit); self.assertEqual(v.min_hold,p.min_hold)
            self.assertGreater(a.entry_coverage(self.bars,v)['joint'],a.entry_coverage(self.bars,p)['joint'])
    def test_failed_direction_deprioritized(self):
        p=lab.defaults()[0]; metrics={'fills':2,'mean_gross':.01,'mean_net':-.01}
        t={'control':{'policy':{'name':p.name}},'change':'increase minimum hold','comparison':{'net_delta':-.01,'stress_delta':0}}
        self.assertNotEqual(a.ranked_variants(p,metrics,self.bars,[t])[0][0],'increase minimum hold')
    def test_coordinator_sequential(self):
        calls=[]; events=[]
        def fake(path,bars,pair,cutoff,name,model,budget,folds):
            calls.append(name); return {'session_id':len(calls),'status':'complete','completed_trials':budget}
        with patch.object(a,'session',side_effect=fake): c.run(self.db,self.bars,'XBTUSD',self.bars[-1].ts,'qwen3:4b',1,3,1,30,events.append)
        self.assertEqual(calls,['alpha','beta','gamma']); self.assertEqual(len(events),6)
    def test_lock_releases(self):
        p=self.root/'lock'
        with c.exclusive(p):
            with self.assertRaises(RuntimeError):
                with c.exclusive(p): pass
        with c.exclusive(p): pass
    def test_recovery_preserves_pending_as_interrupted(self):
        with closing(a.connect(self.db)) as db,db:
            db.execute("INSERT INTO sessions VALUES(1,0,'x',3,'alpha','running','{}')")
            db.execute("INSERT INTO trials VALUES(1,1,'x',3,'a','b','pending','{}')")
        c.recover(self.db)
        with closing(a.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT status FROM trials').fetchone()[0],'interrupted')
            self.assertEqual(db.execute('SELECT status FROM sessions').fetchone()[0],'interrupted')
    def test_frozen_review(self):
        result=a.session(self.db,self.bars,'XBTUSD',self.bars[-1].ts,'alpha',None,1,3)
        t=result['trials'][0]; policies=review.frozen(self.db,[t['trial_id']],'XBTUSD')
        self.assertEqual({p.identity for p in policies},{t['control']['identity'],t['variant']['identity']})
        self.assertTrue(all(p.fraction==.25 for p in policies))
        with self.assertRaises(ValueError): review.frozen(self.db,[t['trial_id']],'ETHUSD')

if __name__=='__main__': unittest.main()

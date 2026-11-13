import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

from autarky_trade import gateway as g
from autarky_trade.exchange import Kraken, ExchangeUnavailable, ExchangeError, signature


def reply(result=None, error=None):
    return json.dumps({'error':error or [],'result':result or {}}).encode()


class FakeKraken:
    def __init__(self):
        self.calls=[]; self.open={}; self.balance={'ZUSD':{'balance':'100','hold_trade':'0'},'XXBT':{'balance':'0.001','hold_trade':'0'}}
        self.response={'descr':{'order':'buy 0.0002 XBTUSD @ limit 100000'}}; self.error=None
        self.order={'status':'open','vol':'0.001','vol_exec':'0','cost':'0','fee':'0'}
    def transport(self,path,body,headers):
        self.calls.append((path,parse_qs(body.decode()) if body else {},headers))
        method=path.split('/')[-1].split('?')[0]
        if method=='SystemStatus': return reply({'status':'online'})
        if method=='AssetPairs': return reply({'XXBTZUSD':{'altname':'XBTUSD','base':'XXBT','quote':'ZUSD','status':'online','lot_multiplier':1,'lot_decimals':8,'tick_size':'0.1','ordermin':'0.0001','costmin':'0.5'}})
        if method=='Ticker': return reply({'XXBTZUSD':{'b':['99999'],'a':['100000']}})
        if method=='OpenOrders': return reply({'open':self.open})
        if method=='BalanceEx': return reply(self.balance)
        if method=='QueryOrders': return reply({'OABC-DEFG':self.order})
        if method=='QueryTrades': return reply({'T1':{'ordertxid':'OABC-DEFG','vol':'0.0004','cost':'40','fee':'0.16','price':'100000'}})
        if method=='AddOrder':
            if self.error: raise self.error
            return reply(self.response)
        raise AssertionError(method)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=str(Path(self.tmp.name)/'gateway.sqlite'); g.init(self.db)
        self.fake=FakeKraken(); self.key='fake-test-key'; self.secret=base64.b64encode(b'test-only-secret').decode()
        self.client=Kraken(self.key,self.secret,self.db+'.nonce',self.fake.transport)
        g.account_snapshot(self.db,self.client,999); self.fake.calls.clear()
    def intent(self, ref='demo', side='buy', qty='0.0002', price='100000'):
        return g.propose(self.db,ref,'XBTUSD',side,qty,price,1000)
    def approved(self, **kw):
        r=self.intent(**kw); g.approve(self.db,r['id'],1001); return r['id']
    def run_order(self, ident):
        return g.validate_intent(self.db,ident,self.client,clock=lambda:1002)
    def add_calls(self):
        return [x for x in self.fake.calls if x[0].endswith('/AddOrder')]
    def test_signature_official_vector(self):
        # Public example from Kraken REST authentication docs (not a usable credential).
        secret='kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=='
        body=b'nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25'
        self.assertEqual(signature('/0/private/AddOrder',body,1616492376594,secret),'4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==')
    def test_adapter_forbids_mutations(self):
        for method,fields in [('Withdraw',{}),('CancelOrder',{}),('AddOrder',{'validate':'false'}),('AddOrder',{'validate':'true','leverage':'2'})]:
            with self.assertRaises(ValueError): self.client._private(method,fields)
        with self.assertRaises(ValueError): self.client.read('AddOrder',validate='true')
        self.assertFalse(self.fake.calls)
    def test_nonce_persists_after_restart_and_clock_rollback(self):
        fresh=Kraken(self.key,self.secret,self.db+'.fresh',self.fake.transport)
        with patch('autarky_trade.exchange.time.time_ns',return_value=123000000): fresh.read('BalanceEx')
        again=Kraken(self.key,self.secret,self.db+'.fresh',self.fake.transport)
        with patch('autarky_trade.exchange.time.time_ns',return_value=100000000): again.read('BalanceEx')
        self.assertEqual([int(x[1]['nonce'][0]) for x in self.fake.calls],[123,124])
    def test_idempotent_reference_and_conflict(self):
        a=self.intent(); b=self.intent(qty='0.000200')
        self.assertEqual(a['id'],b['id'])
        with self.assertRaises(ValueError): self.intent(price='100001')
    def test_no_unapproved_or_expired_order(self):
        r=self.intent()
        with self.assertRaises(ValueError): self.run_order(r['id'])
        with self.assertRaises(ValueError): g.approve(self.db,r['id'],1300)
        self.assertFalse(self.fake.calls)
    def test_validation_only_body_and_no_duplicate(self):
        i=self.approved(); result=self.run_order(i)
        self.assertEqual(result['state'],'validated'); self.assertTrue(json.loads(result['detail'])['account_unchanged_at_postcheck'])
        path,fields,headers=self.add_calls()[0]
        self.assertEqual(fields['validate'],['true']); self.assertEqual(fields['ordertype'],['limit']); self.assertEqual(fields['cl_ord_id'],[i]); self.assertNotIn('leverage',fields)
        self.assertEqual(set(fields),{'nonce','pair','type','ordertype','volume','price','cl_ord_id','validate'})
        self.assertIn('API-Sign',headers)
        with self.assertRaises(ValueError): self.run_order(i)
        self.assertEqual(len(self.add_calls()),1)
        self.assertNotIn(self.secret,json.dumps(g.status(self.db)))
    def test_notional_and_invalid_decimal(self):
        for q in ('0','-0.1','NaN','1e-4','0.0003'):
            with self.assertRaises(ValueError): self.intent(qty=q)
    def test_minimum_tick_and_lot(self):
        for ref,q,p in [('minimum','0.00001','100000'),('tick','0.0002','100000.01'),('lot','0.000200001','100000')]:
            i=self.approved(ref=ref,qty=q,price=p); self.assertEqual(self.run_order(i)['state'],'rejected')
        self.assertFalse(self.add_calls())
    def test_preflight_balance_and_no_short(self):
        self.fake.balance['ZUSD']['hold_trade']='90'
        self.assertEqual(self.run_order(self.approved())['state'],'rejected')
        self.assertTrue(g.status(self.db)['stopped']); self.assertFalse(self.add_calls())
    def test_open_orders_and_price_band(self):
        self.assertEqual(self.run_order(self.approved(ref='distant',price='96000'))['state'],'rejected')
        self.fake.open={'external':{}}
        self.assertEqual(self.run_order(self.approved())['state'],'rejected')
        self.assertTrue(g.status(self.db)['stopped'])
        self.assertFalse(self.add_calls())
    def test_timeout_latches_stop(self):
        self.fake.error=ExchangeUnavailable('test secret not logged')
        i=self.approved(); self.assertEqual(self.run_order(i)['state'],'unknown')
        self.assertTrue(g.status(self.db)['stopped'])
        with self.assertRaises(ValueError): self.run_order(i)
        self.assertEqual(len(self.add_calls()),1)
    def test_postvalidation_account_change_latches_stop(self):
        def transport(path,body,headers):
            result=self.fake.transport(path,body,headers)
            if path.endswith('/AddOrder'): self.fake.balance['ZUSD']['balance']='99'
            return result
        self.client._transport=transport
        self.assertEqual(self.run_order(self.approved())['state'],'unknown')
        self.assertTrue(g.status(self.db)['stopped'])
    def test_unexpected_txid_latches_stop(self):
        self.fake.response={'descr':{'order':'unexpected'},'txid':['OORDER']}
        self.assertEqual(self.run_order(self.approved())['state'],'unknown'); self.assertTrue(g.status(self.db)['stopped'])
    def test_explicit_rejection_is_not_retry(self):
        self.fake.error=ExchangeError('rejected')
        i=self.approved(); self.assertEqual(self.run_order(i)['state'],'rejected')
        self.assertFalse(g.status(self.db)['stopped'])
        with self.assertRaises(ValueError): self.run_order(i)
    def test_crash_recovery_invalidates_claim(self):
        i=self.approved()
        with g.connect(self.db) as c:
            c.execute("UPDATE intents SET state='validating' WHERE id=?",(i,)); c.commit()
        self.assertEqual(g.recover(self.db,1003),[i]); self.assertTrue(g.status(self.db)['stopped'])
        self.assertEqual(g.status(self.db)['intents'][0]['state'],'unknown')
    def test_operator_stop_cancels_pending(self):
        i=self.approved(); g.stop(self.db,1002)
        self.assertEqual(g.status(self.db)['intents'][0]['state'],'cancelled')
        with self.assertRaises(ValueError): self.run_order(i)
        self.assertFalse(self.fake.calls)
    def test_concurrent_claim_is_unique(self):
        i=self.approved(); entered=threading.Event(); release=threading.Event(); results=[]
        def transport(path,body,headers):
            if path.endswith('/AddOrder'):
                entered.set(); release.wait(5)
            return self.fake.transport(path,body,headers)
        self.client._transport=transport
        def run(): results.append(self.run_order(i))
        t=threading.Thread(target=run); t.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(ValueError): self.run_order(i)
        finally: release.set(); t.join(5)
        self.assertEqual(results[0]['state'],'validated'); self.assertEqual(len(self.add_calls()),1)
    def test_requires_account_baseline(self):
        path=str(Path(self.tmp.name)/'new.sqlite'); g.init(path)
        r=g.propose(path,'new','XBTUSD','buy','0.0002','100000',1000); g.approve(path,r['id'],1001)
        with self.assertRaises(ValueError): g.validate_intent(path,r['id'],self.client,clock=lambda:1002)
        self.assertFalse(self.fake.calls)
    def test_unchanged_balance_insufficient(self):
        # A separate account baseline with no held or borrowed funds but insufficient cash/asset.
        path=str(Path(self.tmp.name)/'poor.sqlite'); g.init(path)
        self.fake.balance={'ZUSD':{'balance':'10','hold_trade':'0'},'XXBT':{'balance':'0.0001','hold_trade':'0'}}
        g.account_snapshot(path,self.client,999)
        for side in ('buy','sell'):
            r=g.propose(path,side,'XBTUSD',side,'0.0002','100000',1000); g.approve(path,r['id'],1001)
            self.assertEqual(g.validate_intent(path,r['id'],self.client,clock=lambda:1002)['state'],'rejected')
        self.assertFalse(self.add_calls())
    def test_account_snapshot_detects_changes(self):
        self.assertFalse(g.account_snapshot(self.db,self.client,1000)['baseline'])
        self.assertFalse(g.account_snapshot(self.db,self.client,1001)['needs_review'])
        self.fake.balance['ZUSD']['balance']='99'
        self.assertTrue(g.account_snapshot(self.db,self.client,1002)['needs_review'])
        self.assertTrue(g.status(self.db)['stopped'])
    def test_different_key_rejected(self):
        g.account_snapshot(self.db,self.client,1000)
        other=Kraken('another',self.secret,self.db+'.nonce',self.fake.transport)
        with self.assertRaises(ValueError): g.account_snapshot(self.db,other,1001)
    def test_trade_summary_mismatch_stops(self):
        self.fake.order.update(vol_exec='0.0004',cost='41',fee='0.16',trades=['T1'])
        self.assertFalse(g.inspect_order(self.db,self.client,'OABC-DEFG',1000)['reconciled'])
        self.assertTrue(g.status(self.db)['stopped'])
    def test_paper_import_read_only_and_quantity_bound(self):
        from autarky_trade import portfolio
        path=str(Path(self.tmp.name)/'paper.sqlite'); portfolio.init(path)
        with portfolio.connect(path) as c:
            c.execute("INSERT INTO sleeves(id,pair,artifact,strategy_id,development_end,account,funding,shares) VALUES('BTC:alpha','XBTUSD','{}','test',1,'{}',20,20)")
            c.execute("INSERT INTO portfolio_ledger(observed,sleeve,kind,reason,cash_delta,units_delta,fee,price,reserve_delta) VALUES(1,'BTC:alpha','fill','signal',-20,0.0002,0,100000,0)"); c.commit()
        r=g.propose_paper(self.db,path,1,'0.0001','100000',1000)
        self.assertEqual(r['side'],'buy'); self.assertTrue(r['reference'].startswith('paper:'))
        with self.assertRaises(ValueError): g.propose_paper(self.db,path,1,'0.00021','100000',1000)
    def test_partial_and_terminal_read_only_reconciliation(self):
        self.fake.order.update(vol_exec='0.0004',cost='40',fee='0.16',trades=['T1'])
        a=g.inspect_order(self.db,self.client,'OABC-DEFG',1000); self.assertTrue(a['partial']); self.assertTrue(a['reconciled'])
        self.fake.order.update(status='canceled')
        b=g.inspect_order(self.db,self.client,'OABC-DEFG',1001); self.assertTrue(b['partial']); self.assertEqual(b['status'],'canceled')
        self.fake.order.update(status='open',vol_exec='0.0003',cost='30')
        with self.assertRaises(ValueError): g.inspect_order(self.db,self.client,'OABC-DEFG',1002)
        self.assertTrue(g.status(self.db)['stopped']); self.assertFalse(self.add_calls())

if __name__=='__main__': unittest.main()

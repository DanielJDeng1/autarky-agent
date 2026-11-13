import base64
from decimal import Decimal as D
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from dataclasses import asdict
from autarky_trade import live as l, gateway as g, final_eval as f
from autarky_trade.core import Bar, Strategy
from autarky_trade.research import identity
from autarky_trade.exchange import Kraken, ExchangeUnavailable

NOW=1800000000//3600*3600+10
CHECKS={'portfolio_positive','beats_passive','portfolio_drawdown','concentration_bounded','stress_positive_and_bounded','all_sleeves_positive_and_bounded','positive_bootstrap_lower_bounds'}

def reply(result): return json.dumps({'error':[],'result':result}).encode()

class Venue:
    def __init__(self):
        self.cash=D('100'); self.units=D(0); self.price=D('100000'); self.calls=[]; self.orders={}; self.fills={}; self.lose_ack=False; self.partial=False; self.open=False; self.drift=False; self.permissions=['query-funds','query-open-trades','query-closed-trades','modify-trades','close-trades']
    def transport(self,path,body,headers):
        method=path.split('/')[-1].split('?')[0]; fields={k:v[0] for k,v in parse_qs(body.decode()).items()} if body else {}; self.calls.append((method,fields))
        if method=='GetApiKeyInfo': return reply({'permissions':self.permissions})
        if method=='BalanceEx': return reply({'ZUSD':{'balance':format(self.cash,'f'),'hold_trade':'0'},'XXBT':{'balance':format(self.units,'f'),'hold_trade':'0'}})
        if method=='OpenOrders': return reply({'open':{k:v for k,v in self.orders.items() if v['status']=='open'}})
        if method=='ClosedOrders': return reply({'closed':{k:v for k,v in self.orders.items() if v['status']!='open'},'count':len(self.orders)})
        if method=='SystemStatus': return reply({'status':'online'})
        if method=='AssetPairs': return reply({'XXBTZUSD':{'altname':'XBTUSD','base':'XXBT','quote':'ZUSD','status':'online','lot_multiplier':1,'lot_decimals':8,'tick_size':'0.1','ordermin':'0.00001','costmin':'0.5'}})
        if method=='Ticker': return reply({'XXBTZUSD':{'b':[str(self.price-D(1))],'a':[str(self.price)]}})
        if method=='AddOrder':
            q=D(fields['volume']); executed=q/2 if self.partial else q; price=self.price if fields['type']=='buy' else self.price-D(1)
            if self.open: executed=D(0)
            cost=executed*price; fee=cost*D('.004'); txid='ORDER-'+str(len(self.orders)+1); tid='TRADE-'+str(len(self.orders)+1)
            self.orders[txid]={'cl_ord_id':fields['cl_ord_id'],'descr':{'pair':'XBTUSD','type':fields['type'],'ordertype':'limit','price':fields['price']},'vol':str(q),'vol_exec':format(executed,'f'),'cost':format(cost,'f'),'fee':format(fee,'f'),'status':'open' if self.open else 'canceled' if self.partial else 'closed','trades':[tid] if executed else []}
            if executed:
                self.fills[tid]={'ordertxid':txid,'type':fields['type'],'pair':'XBTUSD','vol':format(executed,'f'),'cost':format(cost,'f'),'fee':format(fee,'f'),'price':str(price)}
                self.cash+=(-cost if fields['type']=='buy' else cost)-fee; self.units+=executed if fields['type']=='buy' else -executed
                if self.drift: self.cash+=D('.01')
            if self.lose_ack: raise ExchangeUnavailable('response lost after accept')
            return reply({'txid':[txid]})
        if method=='QueryOrders': return reply({fields['txid']:self.orders[fields['txid']]})
        if method=='QueryTrades': return reply({k:self.fills[k] for k in fields['txid'].split(',')})
        if method=='CancelOrder': self.orders[fields['txid']]['status']='canceled'; return reply({'count':1})
        raise AssertionError(method)

class Pilot(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.path=str(Path(self.tmp.name)/'live.sqlite'); self.final=str(Path(self.tmp.name)/'final.sqlite')
        self.venue=Venue(); self.client=l.LiveKraken('test',base64.b64encode(b'not-real').decode(),self.path+'.nonce',self.venue.transport)
        self.env=patch.dict(os.environ,{'AUTARKY_LIVE_ENABLE':'YES'}); self.env.start(); self.addCleanup(self.env.stop)
        s=Strategy('trend',5,20,'pilot'); end=NOW-7200; start=end-100*86400
        leg={'pair':'XBTUSD','interval':60,'strategy':asdict(s),'strategy_id':identity(s)}
        plan={'protocol':f.PROTOCOL,'research_protocol':f.RESEARCH_PROTOCOL,'mode':'prospective','start':start,'end':end,'step':3600,'legs':[leg]}
        self.key=f.digest(f.canonical(plan).encode()); c=f.connect(self.final)
        result={'mode':'prospective','eligible_for_manual_review':True,'research_checks_pass':True,'checks':{k:True for k in CHECKS},'legs':[{'pair':'XBTUSD','strategy_id':identity(s)}]}
        c.execute('INSERT INTO final_plans VALUES(?,?,?,?,?,NULL)',(self.key,start-1,'completed',f.canonical(plan),f.canonical(result))); c.commit(); c.close()
    def arm(self): l.arm(self.path,self.final,self.key,self.client,l.ARM_CONFIRM,NOW)
    def bars(self,now=NOW,up=True):
        end=now//3600*3600-3600
        return [Bar(end-(219-i)*3600,100000+i*(1 if up else -1),100001+i*(1 if up else -1),99999+i*(1 if up else -1),100000+i*(1 if up else -1),100) for i in range(220)]
    def plan(self,now=NOW,up=True,exit_only=False): return l.plan(self.path,self.client,self.bars(now,up),now,exit_only)
    def submit(self,r,now=NOW): return l.submit(self.path,r['id'],self.client,l.SUBMIT_CONFIRM,lambda:now)
    def reconcile(self,r,now=NOW): return l.reconcile(self.path,r['id'],self.client,now)
    def count_add(self): return sum(m=='AddOrder' for m,_ in self.venue.calls)
    def test_arm_requires_explicit_confirmation_and_usd_only(self):
        with self.assertRaises(ValueError): l.arm(self.path,self.final,self.key,self.client,'yes',NOW)
        self.venue.units=D('.0001')
        with self.assertRaises(ValueError): self.arm()
        self.venue.units=D(0); self.venue.cash=D('101')
        with self.assertRaises(ValueError): self.arm()
        self.assertEqual(self.count_add(),0)
    def test_withdraw_permission_refused(self):
        self.venue.permissions.append('withdraw-funds')
        with self.assertRaises(ValueError): self.arm()
        self.assertEqual(self.count_add(),0)
    def test_sealed_or_failed_evidence_refused(self):
        c=f.connect(self.final); c.execute("UPDATE final_plans SET state='consumed'"); c.commit(); c.close()
        with self.assertRaises(ValueError): self.arm()
    def test_no_submit_without_confirmation(self):
        self.arm(); r=self.plan()
        with self.assertRaises(ValueError): l.submit(self.path,r['id'],self.client,'no',lambda:NOW)
        with patch.dict(os.environ,{'AUTARKY_LIVE_ENABLE':'NO'}):
            with self.assertRaises(ValueError): self.submit(r)
        self.assertEqual(self.count_add(),0)
    def test_buy_sell_and_reconcile_idempotent(self):
        self.arm(); r=self.plan(); self.assertEqual(r['side'],'buy'); self.assertEqual(self.submit(r)['state'],'acknowledged')
        self.assertEqual(self.reconcile(r)['state'],'closed'); self.reconcile(r)
        self.assertEqual(self.count_add(),1); self.assertTrue(l.status(self.path)['accounting']['reconciled'])
        self.assertEqual(l.status(self.path)['accounting']['realized_pnl'],'0')
        self.venue.price=D('101000'); nexttime=NOW+3600; sell=self.plan(nexttime,False); self.assertEqual(sell['side'],'sell'); self.submit(sell,nexttime); self.reconcile(sell,nexttime)
        self.assertEqual(self.venue.units,0); self.assertTrue(l.status(self.path)['accounting']['reconciled']); self.assertEqual(self.count_add(),2)
    def test_ioc_wire_and_no_blind_retry(self):
        self.arm(); r=self.plan(); self.submit(r); add=next(v for m,v in self.venue.calls if m=='AddOrder')
        self.assertEqual((add['validate'],add['timeinforce'],add['oflags']),('false','IOC','fciq')); self.assertIn('deadline',add); self.assertNotIn('leverage',add)
        with self.assertRaises(ValueError): self.submit(r)
        self.assertEqual(self.count_add(),1)
    def test_lost_ack_discovered_by_client_id(self):
        self.arm(); r=self.plan(); self.venue.lose_ack=True; self.assertEqual(self.submit(r)['state'],'unknown'); self.assertEqual(self.reconcile(r)['state'],'closed'); self.assertTrue(l.status(self.path)['session']['stopped']); self.assertEqual(self.count_add(),1)
    def test_unknown_missing_is_not_retried(self):
        self.arm(); r=self.plan();
        with g.connect(self.path) as c: c.execute("UPDATE live_orders SET state='submitting' WHERE id=?",(r['id'],)); c.commit()
        with self.assertRaises(ValueError): self.reconcile(r,NOW+61)
        self.assertEqual(self.count_add(),0); self.assertTrue(l.status(self.path)['session']['stopped'])
    def test_partial_fill_terminal(self):
        self.arm(); self.venue.partial=True; r=self.plan(); self.submit(r); a=self.reconcile(r); self.assertEqual(a['state'],'canceled'); self.assertGreater(self.venue.units,0); self.assertLess(self.venue.units,D(r['quantity'])); self.assertTrue(l.status(self.path)['accounting']['reconciled'])
    def test_balance_discrepancy_halts(self):
        self.arm(); r=self.plan(); self.venue.drift=True; self.submit(r)
        with self.assertRaises(ValueError): self.reconcile(r)
        self.assertTrue(l.status(self.path)['session']['stopped']); self.assertEqual(len(l.status(self.path)['fills']),0)
    def test_intrabar_stop_and_exit(self):
        self.arm(); r=self.plan(); self.submit(r); self.reconcile(r); self.venue.price=D('70000'); l.observe(self.path,self.client,NOW+100)
        self.assertTrue(l.status(self.path)['session']['stopped']); sell=self.plan(NOW+100,True,True); self.assertEqual(sell['side'],'sell'); self.submit(sell,NOW+100); self.reconcile(sell,NOW+100); self.assertEqual(self.venue.units,0)
    def test_expiry_and_discard(self):
        self.arm(); r=self.plan()
        with self.assertRaises(ValueError): self.submit(r,NOW+31)
        l.discard(self.path,r['id']); self.assertEqual(self.count_add(),0)
        with self.assertRaises(ValueError): self.plan()
    def test_open_ioc_can_be_cancelled(self):
        self.arm(); r=self.plan(); self.venue.open=True; self.submit(r); self.assertEqual(self.reconcile(r)['state'],'open'); l.cancel(self.path,r['id'],self.client,l.SUBMIT_CONFIRM); self.assertEqual(self.reconcile(r)['state'],'canceled')
    def test_account_drift_before_send(self):
        self.arm(); r=self.plan(); self.venue.cash+=D('1'); self.assertEqual(self.submit(r)['state'],'rejected'); self.assertEqual(self.count_add(),0); self.assertTrue(l.status(self.path)['session']['stopped'])
    def test_validation_transport_stays_read_only(self):
        old=Kraken('test',base64.b64encode(b'not-real').decode(),self.path+'.nonce',self.venue.transport)
        with self.assertRaises(ValueError): old._private('AddOrder',{'validate':'false'})
        self.assertEqual(self.count_add(),0)

if __name__=='__main__': unittest.main()

"""Bounded operator-controlled live spot pilot. Separate from paper and validation DBs."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal as D, ROUND_DOWN, ROUND_UP
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from . import final_eval, gateway as g
from .core import Strategy, kraken, validate
from .exchange import Kraken, ExchangeError, ExchangeUnavailable
from .research import identity

MAX_CAPITAL = D('100')
MAX_ORDER = D('25')
MAX_EXPOSURE = D('50')
MAX_DAILY_BUYS = D('50')
FEE_BUFFER = D('.01')
PRICE_BUFFER = D('.002')
DAILY_LOSS = D('.03')
TOTAL_LOSS = D('.08')
TERMINAL = {'closed', 'canceled', 'expired'}
UNRESOLVED = ('submitting', 'unknown', 'acknowledged', 'open')
ARM_CONFIRM = 'I UNDERSTAND LIVE TRADING CAN LOSE MONEY'
SUBMIT_CONFIRM = 'SUBMIT REAL ORDER'


class LiveKraken(Kraken):
    """The old Kraken class remains validate-only. No transfer or withdrawal methods."""
    READ = Kraken.READ | {'ClosedOrders', 'GetApiKeyInfo'}

    def _check_private(self, method, fields):
        if method == 'AddOrder' and fields.get('validate') == 'false':
            if os.environ.get('AUTARKY_LIVE_ENABLE') != 'YES':
                raise ValueError('Live transport disabled')
            if (set(fields) != {'pair','type','ordertype','volume','price','cl_ord_id','validate','timeinforce','oflags','deadline'}
                or fields['pair'] not in g.PAIRS or fields['type'] not in ('buy','sell')
                or fields['ordertype'] != 'limit' or fields['timeinforce'] != 'IOC' or fields['oflags'] != 'fciq'
                or g.decimal(fields['volume'])*g.decimal(fields['price']) > MAX_ORDER):
                raise ValueError('Only bounded IOC spot limit orders allowed')
            uuid.UUID(fields['cl_ord_id'])
            deadline = datetime.fromisoformat(fields['deadline'].replace('Z','+00:00')).timestamp()
            if not 2 <= deadline-time.time() <= 60:
                raise ValueError('Invalid matching engine deadline')
        elif method == 'CancelOrder':
            if os.environ.get('AUTARKY_LIVE_ENABLE') != 'YES' or set(fields) != {'txid'} or not valid_txid(fields['txid']):
                raise ValueError('Only explicit order cancellation allowed')
        else:
            super()._check_private(method, fields)

    def submit_order(self, r):
        deadline = datetime.fromtimestamp(time.time()+20, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return self._private('AddOrder', {'pair':r['pair'],'type':r['side'],'ordertype':'limit',
            'volume':r['quantity'],'price':r['price'],'cl_ord_id':r['id'],'validate':'false',
            'timeinforce':'IOC','oflags':'fciq','deadline':deadline})

    def cancel_order(self, txid):
        return self._private('CancelOrder', {'txid':txid})


def valid_txid(txid):
    import re
    return isinstance(txid,str) and bool(re.fullmatch(r'[A-Z0-9-]{5,40}', txid))


def init(path):
    with g.connect(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS live_session(id INTEGER PRIMARY KEY CHECK(id=1), plan_id TEXT NOT NULL,
            leg TEXT NOT NULL, fingerprint TEXT NOT NULL, started INTEGER NOT NULL, capital TEXT NOT NULL,
            cash TEXT NOT NULL, units TEXT NOT NULL, stopped INTEGER NOT NULL DEFAULT 0,
            last_candle INTEGER NOT NULL DEFAULT 0, day INTEGER NOT NULL, day_equity TEXT NOT NULL,
            peak TEXT NOT NULL, day_buys TEXT NOT NULL DEFAULT '0', last_equity TEXT NOT NULL, last_observed INTEGER NOT NULL DEFAULT 0, last_bid TEXT);
        CREATE TABLE IF NOT EXISTS live_orders(id TEXT PRIMARY KEY, candle INTEGER NOT NULL,
            pair TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
            created INTEGER NOT NULL, state TEXT NOT NULL, txid TEXT UNIQUE, detail TEXT NOT NULL DEFAULT '{}', UNIQUE(candle,side));
        CREATE TABLE IF NOT EXISTS live_events(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS live_fills(id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES live_orders(id),
            quantity TEXT NOT NULL, cost TEXT NOT NULL, fee TEXT NOT NULL);
        ''')


def row(c, table, ident=1):
    if table not in ('live_session','live_orders'): raise ValueError('Invalid table')
    r=c.execute(f'SELECT * FROM {table} WHERE id=?',(ident,)).fetchone()
    if not r: raise ValueError('Live session or order missing')
    return dict(r)


def log(c, now, kind, detail=None):
    c.execute('INSERT INTO live_events(ts,kind,detail) VALUES(?,?,?)',(now,kind,g.dump(detail or {})))


def halt(c, now, reason):
    c.execute('UPDATE live_session SET stopped=1')
    log(c,now,'halt',{'reason':reason})


def check_key(s, client):
    if s['fingerprint'] != client._fingerprint: raise ValueError('Wrong exchange API key')


def read_plan(path, key, now):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        r=c.execute('SELECT * FROM final_plans WHERE id=?',(key,)).fetchone()
        if not r or r['state']!='completed': raise ValueError('Completed final evaluation required')
        p=json.loads(r['plan']); result=json.loads(r['result'])
        if (final_eval.digest(final_eval.canonical(p).encode())!=key or p['protocol']!=final_eval.PROTOCOL
            or p['research_protocol']!=final_eval.RESEARCH_PROTOCOL or p['mode']!='prospective'
            or r['created']>=p['start'] or p['end']+p['step']>now or len(p['legs'])!=1
            or result.get('eligible_for_manual_review') is not True or result.get('research_checks_pass') is not True
            or result.get('mode')!='prospective' or set(result.get('checks',{}))!={'portfolio_positive','beats_passive','portfolio_drawdown','concentration_bounded','stress_positive_and_bounded','all_sleeves_positive_and_bounded','positive_bootstrap_lower_bounds'}
            or not all(v is True for v in result['checks'].values()) or p['end']-p['start']+p['step']<final_eval.MIN_DAYS*86400):
            raise ValueError('Require a passed single-market prospective final evaluation')
        leg=p['legs'][0]; strategy=Strategy(**leg['strategy'])
        if leg['pair'] not in g.PAIRS or identity(strategy)!=leg['strategy_id'] or len(result['legs'])!=1 or result['legs'][0]['strategy_id']!=leg['strategy_id'] or result['legs'][0]['pair']!=leg['pair']:
            raise ValueError('Strategy evidence mismatch')
        return {**leg,'final_end':p['end'],'plan_id':key}


def check_permissions(client):
    # Refuse credentials with withdrawal, transfer, or unrelated permissions.
    required={'query-funds','query-open-trades','query-closed-trades','modify-trades','close-trades'}
    permissions=client.read('GetApiKeyInfo').get('permissions')
    if not isinstance(permissions,list) or set(permissions)!=required:
        raise ValueError('Use a dedicated key with exactly the required trading/query permissions')


def account(client, pair):
    raw=client.read('BalanceEx'); orders=client.read('OpenOrders').get('open')
    snap=g.normalized_account(raw,orders); base={'XBTUSD':'XXBT','ETHUSD':'XETH'}[pair]
    for asset,b in snap['balances'].items():
        if any(D(b[k]) for k in ('hold_trade','credit','credit_used')) or (asset not in ('ZUSD',base) and D(b['balance'])):
            raise ValueError('Dedicated USD/one-asset account required, no holds or credit')
    if orders: raise ValueError('Unexpected open orders; cancel and reconcile')
    return g.available(raw,'ZUSD'),g.available(raw,base)


def arm(path, final_db, key, client, confirm, now=None):
    now=int(time.time()) if now is None else now
    if confirm!=ARM_CONFIRM: raise ValueError('Explicit operator confirmation required')
    if Path(path).resolve()==Path(final_db).resolve(): raise ValueError('Separate live DB required')
    leg=read_plan(final_db,key,now); init(path)
    check_permissions(client)
    cash,units=account(client,leg['pair'])
    if units or not D(0)<cash<=MAX_CAPITAL: raise ValueError('Dedicated account must start USD-only, at most $100')
    with g.connect(path) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO live_session(id,plan_id,leg,fingerprint,started,capital,cash,units,day,day_equity,peak,last_equity) VALUES(1,?,?,?,?,?,?,?,?,?,?,?)',
            (key,g.dump(leg),client._fingerprint,now,g.fmt(cash),g.fmt(cash),'0',now//86400,g.fmt(cash),g.fmt(cash),g.fmt(cash)))
        log(c,now,'armed',{'plan':key,'capital':g.fmt(cash)}); c.commit()


def market(client, pair):
    if client.public('SystemStatus').get('status')!='online': raise ValueError('Exchange not online')
    m=g.pair_row(client.public('AssetPairs',{'pair':pair}),pair)
    if m.get('status')!='online' or m.get('quote')!='ZUSD' or m.get('base')!={'XBTUSD':'XXBT','ETHUSD':'XETH'}[pair] or m.get('lot_multiplier')!=1:
        raise ValueError('Unsupported market')
    n=m['lot_decimals']
    if type(n) is not int or not 0<=n<=18: raise ValueError('Invalid lot precision')
    t=client.public('Ticker',{'pair':pair})
    if len(t)!=1: raise ValueError('Ambiguous ticker')
    tick=next(iter(t.values())); bid,ask=g.decimal(tick['b'][0]),g.decimal(tick['a'][0])
    if bid>ask or ask/bid-1>D('.02'): raise ValueError('Spread too wide')
    return bid,ask,D(1).scaleb(-n),g.decimal(m['tick_size']),g.decimal(m['ordermin']),g.decimal(m['costmin'])


def rounded(value, tick, up=False):
    return (value/tick).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*tick


def risk(c,s,now,bid):
    cash,units=D(s['cash']),D(s['units']); equity=cash+units*bid
    peak=max(D(s['peak']),equity); day=now//86400
    if day<s['day'] or now<s['last_observed']: raise ValueError('Clock moved backward')
    # Carry previous observed equity into a new UTC day, so an overnight gap is not erased.
    start=D(s['day_equity']) if day==s['day'] else D(s['last_equity'])
    buys=D(s['day_buys']) if day==s['day'] else D(0)
    c.execute('UPDATE live_session SET day=?,day_equity=?,day_buys=?,peak=?,last_equity=?,last_observed=?,last_bid=?',(day,g.fmt(start),g.fmt(buys),g.fmt(peak),g.fmt(equity),now,g.fmt(bid)))
    if equity<=start*(1-DAILY_LOSS) or equity<=peak*(1-TOTAL_LOSS) or units*bid>MAX_EXPOSURE:
        halt(c,now,'marked equity/exposure limit')
    return equity,buys


def observe(path,client,now=None):
    now=int(time.time()) if now is None else now
    with g.connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); s=row(c,'live_session'); check_key(s,client)
        if c.execute("SELECT 1 FROM live_orders WHERE state IN ('submitting','unknown','acknowledged','open')").fetchone():
            raise ValueError('Reconcile unresolved order before marking risk')
        pair=json.loads(s['leg'])['pair']
        try:
            if account(client,pair)!=(D(s['cash']),D(s['units'])): raise ValueError('Account drift')
            bid,*_=market(client,pair); risk(c,s,now,bid); c.commit()
        except Exception:
            c.rollback(); c.execute('BEGIN IMMEDIATE'); halt(c,now,'risk observation failed'); c.commit(); raise
    return status(path)


def plan(path,client,bars,now=None,exit_only=False):
    """Generate a single exact intent from the frozen strategy and fresh exchange state."""
    now=int(time.time()) if now is None else now
    with g.connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); s=row(c,'live_session'); check_key(s,client)
        if c.execute("SELECT 1 FROM live_orders WHERE state IN ('proposed','submitting','unknown','acknowledged','open')").fetchone(): raise ValueError('Resolve existing intent first')
        leg=json.loads(s['leg']); pair=leg['pair']; step=leg['interval']*60
        bars=validate(bars,step); candle=bars[-1].ts
        if (candle%step or not 0<=now-candle-step<=(step+90 if exit_only else 90) or candle<=leg['final_end']
            or (not exit_only and candle<=s['last_candle'])): raise ValueError('Require a fresh closed candle after final evaluation')
        try:
            cash,units=account(client,pair)
            if (cash,units)!=(D(s['cash']),D(s['units'])): raise ValueError('Account drift')
            bid,ask,lot,tick,minqty,mincost=market(client,pair)
        except (ValueError,KeyError,RuntimeError):
            halt(c,now,'account or market preflight failed'); c.commit(); raise
        equity,buys=risk(c,s,now,bid); s=row(c,'live_session')
        strategy=Strategy(**leg['strategy'])
        if identity(strategy)!=leg['strategy_id']: raise ValueError('Frozen strategy changed')
        signal=strategy.signal([b.close for b in bars])
        side='sell' if units and (not signal or s['stopped'] or exit_only) else 'buy' if signal and not units and not s['stopped'] and not exit_only else None
        if side is None or c.execute('SELECT 1 FROM live_orders WHERE candle=? AND side=?',(candle,side)).fetchone(): c.commit(); return None
        price=rounded(ask*(1+PRICE_BUFFER) if side=='buy' else bid*(1-PRICE_BUFFER),tick,side=='buy')
        if side=='buy':
            spend=min(MAX_ORDER,MAX_EXPOSURE,MAX_DAILY_BUYS-buys,cash*D('.25')/(1+FEE_BUFFER))
            qty=rounded(spend/price,lot)
        else: qty=rounded(min(units,MAX_ORDER/price),lot)
        if qty<minqty or qty*price<mincost: c.commit(); return None
        ident=str(uuid.uuid4())
        c.execute('INSERT INTO live_orders(id,candle,pair,side,quantity,price,created,state) VALUES(?,?,?,?,?,?,?,?)',
                  (ident,candle,pair,side,g.fmt(qty),g.fmt(price),now,'proposed'))
        log(c,now,'proposed',{'id':ident,'side':side,'quantity':g.fmt(qty),'price':g.fmt(price)}); c.commit()
        return row(c,'live_orders',ident)


def submit(path,ident,client,confirm,clock=time.time):
    if confirm!=SUBMIT_CONFIRM or os.environ.get('AUTARKY_LIVE_ENABLE')!='YES': raise ValueError('Real order requires explicit enable and confirmation')
    now=int(clock())
    with g.connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); s=row(c,'live_session'); r=row(c,'live_orders',ident); check_key(s,client)
        if r['state']!='proposed' or not 0<=now-r['created']<=30 or (s['stopped'] and r['side']=='buy'): raise ValueError('Order expired, already used or halted')
        # Commit claim and consume candle before any exchange mutation.
        c.execute("UPDATE live_orders SET state='submitting' WHERE id=?",(ident,)); c.execute('UPDATE live_session SET last_candle=max(last_candle,?)',(r['candle'],)); c.commit()
    sent=False
    try:
        check_permissions(client); cash,units=account(client,r['pair']); bid,ask,lot,tick,minqty,mincost=market(client,r['pair']); q,p=D(r['quantity']),D(r['price'])
        with g.connect(path) as c:
            c.execute('BEGIN IMMEDIATE'); s=row(c,'live_session'); stamp=int(clock())
            if (cash,units)!=(D(s['cash']),D(s['units'])): raise ValueError('Account drift')
            equity,buys=risk(c,s,stamp,bid); s=row(c,'live_session')
            if not 0<=stamp-r['created']<=30 or row(c,'live_orders',ident)['state']!='submitting': raise ValueError('Expired/superseded')
            if q%lot or p%tick or q<minqty or not mincost<=q*p<=MAX_ORDER or abs(p/(ask if r['side']=='buy' else bid)-1)>D('.01'): raise ValueError('Market changed')
            if r['side']=='buy' and (s['stopped'] or units or q*p*(1+FEE_BUFFER)>cash or q*p+buys>MAX_DAILY_BUYS or q*p>MAX_EXPOSURE): raise ValueError('Buy risk cap')
            if r['side']=='sell' and q>units: raise ValueError('Cannot short')
            # Hold the writer lock across this one bounded request. A completed stop
            # cannot race past the final authorization; it waits for in-flight I/O.
            sent=True; response=client.submit_order(r)
            ids=response.get('txid')
            if not isinstance(ids,list) or len(ids)!=1 or not valid_txid(ids[0]): raise ExchangeUnavailable('Ambiguous order acknowledgement')
            c.execute("UPDATE live_orders SET state='acknowledged',txid=? WHERE id=?",(ids[0],ident)); log(c,int(clock()),'acknowledged',{'id':ident,'txid':ids[0]}); c.commit()
    except Exception:
        # Even explicit rejection after a mutation is conservative: require reconciliation.
        with g.connect(path) as c:
            c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE live_orders SET state=? WHERE id=?',('unknown' if sent else 'rejected',ident)); halt(c,int(clock()),'submission uncertain' if sent else 'preflight rejected'); c.commit()
    with g.connect(path) as c: return row(c,'live_orders',ident)


def reconcile(path,ident,client,now=None):
    now=int(time.time()) if now is None else now
    with g.connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); s=row(c,'live_session'); r=row(c,'live_orders',ident); check_key(s,client)
        if r['state'] in TERMINAL: return r
        if r['state'] not in UNRESOLVED: raise ValueError('Order not submitted')
        if now<max(r['created'],s['last_observed']): raise ValueError('Stale reconciliation clock')
        if r['state']=='submitting' and now<r['created']+60: raise ValueError('Submission may still be in flight; wait at least 60 seconds and stop submitting process')
        try:
            txid=r['txid']
            if not txid:
                # A bounded lookup is not proof of absence. Never retry an unknown order.
                opened=client.read('OpenOrders').get('open',{}); closed=client.read('ClosedOrders',start=r['created']-60,cl_ord_id=ident).get('closed',{})
                matches=[k for k,v in {**opened,**closed}.items() if v.get('cl_ord_id')==ident]
                if len(matches)!=1 or not valid_txid(matches[0]): raise ValueError('Unresolved client order ID')
                txid=matches[0]; c.execute('UPDATE live_orders SET txid=? WHERE id=?',(txid,ident))
            response=client.read('QueryOrders',txid=txid,trades='true',consolidate_taker='false')
            if set(response)!={txid}: raise ValueError('Order not found')
            o=response[txid]; descr=o['descr']; pair=descr['pair']
            aliases={'XBTUSD':{'XBTUSD','XXBTZUSD','XBT/USD','BTC/USD'},'ETHUSD':{'ETHUSD','XETHZUSD','ETH/USD'}}
            if o.get('cl_ord_id')!=ident or pair not in aliases[r['pair']] or descr['type']!=r['side'] or descr['ordertype']!='limit' or D(o['vol'])!=D(r['quantity']) or D(descr['price'])!=D(r['price']): raise ValueError('Order identity mismatch')
            if o['status'] in ('pending','open'):
                c.execute("UPDATE live_orders SET state='open' WHERE id=?",(ident,)); halt(c,now,'IOC unexpectedly still open'); c.commit(); return row(c,'live_orders',ident)
            if o['status'] not in TERMINAL: raise ValueError('Unknown order state')
            ids=o.get('trades',[])
            if not isinstance(ids,list) or len(ids)>200 or len(set(ids))!=len(ids) or any(not valid_txid(x) for x in ids): raise ValueError('Invalid fill IDs')
            fills=[]
            for i in range(0,len(ids),20):
                subset=ids[i:i+20]; trades=client.read('QueryTrades',txid=','.join(subset))
                if set(trades)!=set(subset): raise ValueError('Missing trade records')
                for tid,t in trades.items():
                    if t['ordertxid']!=txid or t['type']!=r['side'] or t['pair'] not in aliases[r['pair']]: raise ValueError('Trade identity mismatch')
                    q,cost,fee,price=(g.decimal(t[k],zero=k=='fee') for k in ('vol','cost','fee','price'))
                    if (r['side']=='buy' and price>D(r['price'])) or (r['side']=='sell' and price<D(r['price'])): raise ValueError('Fill outside limit')
                    fills.append((tid,q,cost,fee))
            q,cost,fee=[sum((f[i] for f in fills),D(0)) for i in (1,2,3)]
            if (q,cost,fee)!=tuple(g.decimal(o[k],zero=True) for k in ('vol_exec','cost','fee')) or q>D(r['quantity']) or (o['status']=='closed' and q!=D(r['quantity'])): raise ValueError('Cumulative fill mismatch')
            # fciq is a preference, not a guarantee. Require exact exchange balances or stop.
            cash=D(s['cash'])+(-cost if r['side']=='buy' else cost)-fee; units=D(s['units'])+(q if r['side']=='buy' else -q)
            if cash<0 or units<0 or account(client,r['pair'])!=(cash,units): raise ValueError('Balance/fill or fee-currency discrepancy')
            for tid,qty,amount,charge in fills: c.execute('INSERT INTO live_fills VALUES(?,?,?,?,?)',(tid,ident,g.fmt(qty),g.fmt(amount),g.fmt(charge)))
            # Conservatively attribute buy turnover to the observation UTC day.
            day=now//86400; buys=(D(s['day_buys']) if s['day']==day else D(0))+(cost if r['side']=='buy' else D(0))
            c.execute('UPDATE live_session SET cash=?,units=?,day_buys=?,day_equity=?,day=?',(g.fmt(cash),g.fmt(units),g.fmt(buys),s['day_equity'] if day==s['day'] else s['last_equity'],day))
            c.execute('UPDATE live_orders SET state=?,detail=? WHERE id=?',(o['status'],g.dump({'quantity':g.fmt(q),'cost':g.fmt(cost),'fee':g.fmt(fee)}),ident)); log(c,now,'reconciled',{'id':ident}); c.commit()
            # Do not lose already reconciled fills if a subsequent quote is unavailable.
            try:
                c.execute('BEGIN IMMEDIATE'); bid,*_=market(client,r['pair']); risk(c,row(c,'live_session'),now,bid); c.commit()
            except Exception:
                c.rollback(); c.execute('BEGIN IMMEDIATE'); halt(c,now,'post-fill valuation unavailable'); c.commit()
        except Exception:
            c.rollback(); c.execute('BEGIN IMMEDIATE'); halt(c,now,'order reconciliation failed; no resubmission'); c.commit(); raise
        return row(c,'live_orders',ident)


def stop(path,now=None):
    now=int(time.time()) if now is None else now
    with g.connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); row(c,'live_session'); halt(c,now,'operator stop'); c.execute("UPDATE live_orders SET state='cancelled' WHERE state='proposed'"); c.commit()


def discard(path,ident):
    with g.connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); r=row(c,'live_orders',ident)
        if r['state']!='proposed': raise ValueError('Only unsent proposals may be discarded')
        c.execute("UPDATE live_orders SET state='cancelled' WHERE id=?",(ident,)); c.execute('UPDATE live_session SET last_candle=max(last_candle,?)',(r['candle'],)); c.commit()


def cancel(path,ident,client,confirm):
    if confirm!=SUBMIT_CONFIRM: raise ValueError('Explicit real cancellation confirmation required')
    with g.connect(path) as c:
        s=row(c,'live_session'); r=row(c,'live_orders',ident); check_key(s,client)
        if r['state'] not in UNRESOLVED or not r['txid']: raise ValueError('Resolve exchange order ID first')
    stop(path); return client.cancel_order(r['txid'])


def accounting(c,s):
    cash=D(s['capital']); units=basis=realized=fees=D(0)
    for r in c.execute('SELECT f.*,o.side FROM live_fills f JOIN live_orders o ON o.id=f.order_id ORDER BY f.rowid'):
        q,cost,fee=(D(r[k]) for k in ('quantity','cost','fee')); fees+=fee
        if r['side']=='buy': cash-=cost+fee; units+=q; basis+=cost+fee
        else:
            if q>units: raise ValueError('Journal inventory inconsistency')
            allocated=basis*q/units; units-=q; basis-=allocated; cash+=cost-fee; realized+=cost-fee-allocated
    bid=D(s['last_bid']) if s['last_bid'] else None
    return {'cash':g.fmt(cash),'units':g.fmt(units),'fees':g.fmt(fees),'realized_pnl':g.fmt(realized),
            'open_cost_basis':g.fmt(basis),'marked_equity':g.fmt(cash+units*bid) if bid is not None else None,
            'reconciled':(cash,units)==(D(s['cash']),D(s['units']))}


def status(path):
    with g.connect(path) as c:
        c.execute('BEGIN'); s=row(c,'live_session'); s.pop('fingerprint'); s['leg']=json.loads(s['leg']); return {'mode':'LIVE PILOT (no autonomous order loop)','session':s,'accounting':accounting(c,s),'orders':[dict(r) for r in c.execute('SELECT * FROM live_orders ORDER BY created DESC LIMIT 100')],'fills':[dict(r) for r in c.execute('SELECT * FROM live_fills ORDER BY rowid DESC LIMIT 100')]}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/live.sqlite'); sub=p.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('arm'); a.add_argument('--final-db',required=True); a.add_argument('--plan-id',required=True); a.add_argument('--confirm',required=True)
    a=sub.add_parser('plan'); a.add_argument('--exit-only',action='store_true')
    for name in ('submit','cancel','reconcile','discard'):
        a=sub.add_parser(name); a.add_argument('id');
        if name in ('submit','cancel'): a.add_argument('--confirm',required=True)
    sub.add_parser('status'); sub.add_parser('stop'); sub.add_parser('observe'); x=p.parse_args(); init(x.db)
    if x.cmd=='status': result=status(x.db)
    elif x.cmd=='stop': stop(x.db); result={'stopped':True}
    elif x.cmd=='discard': discard(x.db,x.id); result={'discarded':x.id}
    else:
        client=LiveKraken.from_env(x.db+'.nonce')
        if x.cmd=='arm': arm(x.db,x.final_db,x.plan_id,client,x.confirm); result=status(x.db)
        elif x.cmd=='observe': result=observe(x.db,client)
        elif x.cmd=='plan':
            with g.connect(x.db) as c: leg=json.loads(row(c,'live_session')['leg'])
            result=plan(x.db,client,kraken(leg['pair'],leg['interval']),exit_only=x.exit_only)
        elif x.cmd=='submit': result=submit(x.db,x.id,client,x.confirm)
        elif x.cmd=='cancel': result=cancel(x.db,x.id,client,x.confirm)
        else: result=reconcile(x.db,x.id,client)
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__': main()

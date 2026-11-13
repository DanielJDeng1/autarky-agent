"""Operator-approved validation-only order gateway; no actual orders can be sent."""
from __future__ import annotations
import argparse
import hashlib
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from .exchange import Kraken, ExchangeError, ExchangeUnavailable

PAIRS = ('XBTUSD', 'ETHUSD')
MAX_NOTIONAL = Decimal('25')
FEE_BUFFER = Decimal('0.01')
MAX_DISTANCE = Decimal('0.02')
TTL = 300
MAX_PREFLIGHT = 60
D = Decimal


class AccountDrift(ValueError):
    pass


class PreflightError(ValueError):
    pass


def decimal(value, zero=False):
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError('Use decimal strings, not floating point')
    s = str(value)
    if not re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,18})?', s) or len(s.replace('.', '').lstrip('0')) > 14:
        raise ValueError('Invalid nonnegative decimal')
    try:
        v = D(s)
    except InvalidOperation as e:
        raise ValueError('Invalid decimal') from e
    if v < 0 or (not zero and v == 0):
        raise ValueError('Invalid amount')
    return v


def fmt(v):
    return format(v, 'f')


def dump(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


@contextmanager
def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA journal_mode=WAL')
        yield c


def init(path):
    with connect(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS gateway(id INTEGER PRIMARY KEY CHECK(id=1), stopped INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO gateway VALUES(1,0);
        CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, reference TEXT UNIQUE NOT NULL,
          pair TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
          created INTEGER NOT NULL, expires INTEGER NOT NULL, state TEXT NOT NULL,
          updated INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS gateway_events(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
          intent TEXT REFERENCES intents(id), kind TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_identity(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_snapshots(id INTEGER PRIMARY KEY, observed INTEGER NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observed_orders(txid TEXT PRIMARY KEY, status TEXT NOT NULL,
          volume TEXT NOT NULL, executed TEXT NOT NULL, cost TEXT NOT NULL, fee TEXT NOT NULL,
          detail TEXT NOT NULL, observed INTEGER NOT NULL);
        ''')


def event(c, now, intent, kind, detail=None):
    c.execute('INSERT INTO gateway_events(ts,intent,kind,detail) VALUES(?,?,?,?)',
              (now, intent, kind, dump(detail or {})))


def halted(c):
    return bool(c.execute('SELECT stopped FROM gateway WHERE id=1').fetchone()[0])


def now_sec(now):
    return int(time.time()) if now is None else now


def bind_account(c, client):
    fingerprint = client._fingerprint
    row = c.execute('SELECT fingerprint FROM account_identity WHERE id=1').fetchone()
    if row and row[0]!=fingerprint:
        raise ValueError('Gateway belongs to another API key; use a new gateway database')
    if not row:
        c.execute('INSERT INTO account_identity VALUES(1,?)',(fingerprint,))


def get_intent(c, order_id):
    r = c.execute('SELECT * FROM intents WHERE id=?', (order_id,)).fetchone()
    if r is None:
        raise ValueError('Unknown intent')
    return dict(r)


def propose(path, reference, pair, side, quantity, price, now=None):
    now = now_sec(now)
    if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9:._-]{1,128}', reference):
        raise ValueError('Invalid unique reference')
    if pair not in PAIRS or side not in ('buy', 'sell'):
        raise ValueError('Only BTC/ETH USD spot supported')
    q, p = decimal(quantity), decimal(price)
    if q*p > MAX_NOTIONAL:
        raise ValueError('Validation notional exceeds $25 cap')
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM intents WHERE reference=?', (reference,)).fetchone()
        if old:
            if (old['pair'], old['side'], D(old['quantity']), D(old['price'])) != (pair, side, q, p):
                raise ValueError('Reference already used for a different intent')
            return dict(old)
        if halted(c):
            raise ValueError('Gateway stopped')
        ident = str(uuid.uuid4())
        c.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (ident, reference, pair, side, fmt(q), fmt(p), now, now+TTL, 'proposed', now, '{}'))
        event(c, now, ident, 'proposed'); c.commit()
        return get_intent(c, ident)


def propose_paper(path, source, ledger_id, quantity, price, now=None):
    """Import direction/provenance only; operator supplies exchange-valid quantity and price."""
    if Path(source).resolve()==Path(path).resolve(): raise ValueError('Separate portfolio DB required')
    uri=Path(source).resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True)) as c:
        c.row_factory=sqlite3.Row
        row=c.execute("SELECT l.*,s.pair,s.strategy_id FROM portfolio_ledger l JOIN sleeves s ON s.id=l.sleeve WHERE l.id=? AND l.kind='fill'",(ledger_id,)).fetchone()
        if not row: raise ValueError('Paper fill not found')
        units=D(str(row['units_delta']))
        if not units.is_finite() or not units or decimal(quantity)>abs(units): raise ValueError('Quantity exceeds paper fill; choose a smaller validation amount')
        fingerprint=hashlib.sha256(dump(dict(row)).encode()).hexdigest()
        return propose(path,'paper:'+fingerprint,row['pair'],'buy' if units>0 else 'sell',quantity,price,now)


def approve(path, ident, now=None):
    now = now_sec(now)
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); r = get_intent(c, ident)
        if halted(c) or r['state'] != 'proposed' or not r['created'] <= now < r['expires']:
            raise ValueError('Intent cannot be approved')
        c.execute("UPDATE intents SET state='approved', updated=? WHERE id=?", (now, ident))
        event(c, now, ident, 'operator_approved'); c.commit()
        return get_intent(c, ident)


def stop(path, now=None):
    now = now_sec(now)
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE gateway SET stopped=1')
        c.execute("UPDATE intents SET state='cancelled', updated=? WHERE state IN ('proposed','approved')", (now,))
        event(c, now, None, 'operator_stop'); c.commit()


def recover(path, now=None):
    """Only use after all gateway workers have stopped. Never resubmit uncertain intents."""
    now = now_sec(now)
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE')
        ids = [r[0] for r in c.execute("SELECT id FROM intents WHERE state='validating'")]
        for ident in ids:
            c.execute("UPDATE intents SET state='unknown',updated=? WHERE id=?", (now, ident))
            event(c, now, ident, 'interrupted')
        if ids:
            c.execute('UPDATE gateway SET stopped=1')
        c.commit(); return ids


def pair_row(result, pair):
    matches = [(k,v) for k,v in result.items() if isinstance(v,dict) and (k==pair or v.get('altname')==pair)]
    if len(matches)!=1:
        raise ValueError('Missing or ambiguous exchange pair metadata')
    return matches[0][1]


def available(balance, asset):
    row = balance.get(asset, {'balance':'0','hold_trade':'0'})
    if not isinstance(row,dict):
        raise ValueError('Malformed extended balance')
    # Explicitly reject credit/margin; do not count it as spendable cash.
    if any(decimal(row.get(k,'0'), zero=True) for k in ('credit','credit_used')):
        raise ValueError('Credit balance not supported')
    value = decimal(row['balance'], zero=True)-decimal(row['hold_trade'], zero=True)
    if value<0:
        raise ValueError('Invalid available balance')
    return value


def normalized_account(raw, orders):
    if not isinstance(raw,dict) or not isinstance(orders,dict): raise ValueError('Malformed account data')
    balances={}
    for asset,row in raw.items():
        if not isinstance(row,dict) or 'balance' not in row or 'hold_trade' not in row: raise ValueError('Malformed balance')
        balances[asset]={k:fmt(decimal(row.get(k,'0'),zero=True).normalize()) for k in ('balance','hold_trade','credit','credit_used')}
    return {'balances':balances,'open_order_ids':sorted(orders)}


def changed_assets(baseline,snapshot):
    old,new=baseline['balances'],snapshot['balances']
    empty={k:'0' for k in ('balance','hold_trade','credit','credit_used')}
    return [a for a in sorted(set(old)|set(new)) if old.get(a,empty)!=new.get(a,empty)]


def preflight(client, r, baseline):
    pair, side = r['pair'], r['side']; q,p = decimal(r['quantity']),decimal(r['price'])
    if pair not in PAIRS or side not in ('buy','sell') or q*p>MAX_NOTIONAL:
        raise PreflightError('Intent violates gateway limits')
    if client.public('SystemStatus').get('status') != 'online':
        raise PreflightError('Exchange not online')
    meta = pair_row(client.public('AssetPairs', {'pair':pair}), pair)
    if meta.get('status')!='online' or meta.get('quote')!='ZUSD' or meta.get('base')!={'XBTUSD':'XXBT','ETHUSD':'XETH'}[pair] or meta.get('lot_multiplier')!=1:
        raise PreflightError('Unsupported market configuration')
    n = meta['lot_decimals']
    if type(n) is not int or not 0<=n<=18 or q % D(1).scaleb(-n) or p % decimal(meta['tick_size']):
        raise PreflightError('Invalid lot or price tick')
    if q<decimal(meta['ordermin']) or q*p<decimal(meta['costmin']):
        raise PreflightError('Below exchange minimum')
    orders = client.read('OpenOrders')
    bal = client.read('BalanceEx')
    snapshot=normalized_account(bal,orders.get('open'))
    if snapshot['open_order_ids'] or changed_assets(baseline,snapshot):
        raise AccountDrift('Account changed; inspect balance and open orders')
    if side=='buy' and available(bal,meta['quote'])<q*p*(1+FEE_BUFFER):
        raise PreflightError('Insufficient unencumbered USD')
    if side=='sell' and available(bal,meta['base'])<q:
        raise PreflightError('Insufficient owned asset; no shorting')
    # Fetch ticker last, directly before sending; REST ticker has no exchange timestamp.
    result = client.public('Ticker', {'pair':pair})
    if len(result)!=1:
        raise PreflightError('Ambiguous ticker')
    tick = next(iter(result.values())); bid,ask=decimal(tick['b'][0]),decimal(tick['a'][0])
    if bid>ask or ask/bid-1>MAX_DISTANCE or abs(p/(ask if side=='buy' else bid)-1)>MAX_DISTANCE:
        raise PreflightError('Spread or limit price too far from market')
    return {'bid':fmt(bid),'ask':fmt(ask),'notional':fmt(q*p),'available_quote':fmt(available(bal,meta['quote'])),'available_base':fmt(available(bal,meta['base']))}


def validate_intent(path, ident, client, clock=time.time):
    now = int(clock())
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); r=get_intent(c,ident)
        if halted(c) or r['state']!='approved' or not r['created']<=now<r['expires'] or c.execute("SELECT 1 FROM intents WHERE state='validating'").fetchone():
            raise ValueError('Intent not approved, expired, or stopped')
        bind_account(c,client)
        baseline_row=c.execute('SELECT detail FROM account_snapshots ORDER BY id LIMIT 1').fetchone()
        if not baseline_row: raise ValueError('Record an account baseline before validating')
        baseline=json.loads(baseline_row[0])['snapshot']
        # Exactly one owner may attempt this intent. Persist BEFORE contacting exchange.
        c.execute("UPDATE intents SET state='validating',updated=? WHERE id=?", (now,ident))
        event(c,now,ident,'claimed'); c.commit()
    detail={}; state='rejected'; sent=False; drift=False
    try:
        detail=preflight(client,r,baseline)
        with connect(path) as c:
            stamp=int(clock()); current=get_intent(c,ident)
            if halted(c) or current['state']!='validating' or stamp>=r['expires'] or not 0<=stamp-now<=MAX_PREFLIGHT:
                raise ValueError('Preflight expired or gateway stopped')
        sent=True
        response=client.validate_order(pair=r['pair'],side=r['side'],quantity=r['quantity'],price=r['price'],client_id=ident)
        if response.get('txid') or not isinstance(response.get('descr'),dict) or not isinstance(response['descr'].get('order'),str) or not response['descr']['order']:
            state='unknown'; detail['reason']='unexpected validation response; inspect account'
        else:
            state='validated'; detail['exchange_description']=str(response['descr']['order'])[:500]
            post=account_snapshot(path,client,int(clock()))
            if post['needs_review']:
                state='unknown'; detail['reason']='Unexpected account change after validation'
            else: detail['account_unchanged_at_postcheck']=True
    except AccountDrift:
        drift=True; detail['reason']='Account changed; no order validation sent'
    except PreflightError as e:
        detail['reason']=str(e)
    except ExchangeError:
        state='unknown' if sent and state=='validated' else 'rejected'
        detail['reason']='post-validation account check failed' if state=='unknown' else 'exchange rejected request'
    except (ValueError, KeyError, TypeError, ArithmeticError, ExchangeUnavailable, OSError, RuntimeError):
        state='unknown' if sent else 'rejected'
        detail['reason']='submission response uncertain' if sent else 'preflight failed; no validation sent'
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); current=get_intent(c,ident)
        if current['state']!='validating':
            raise ValueError('Intent superseded; inspect exchange account')
        stamp=int(clock()); c.execute('UPDATE intents SET state=?,updated=?,detail=? WHERE id=?',(state,stamp,dump(detail),ident))
        if state=='unknown' or drift: c.execute('UPDATE gateway SET stopped=1')
        event(c,stamp,ident,state,detail); c.commit(); return get_intent(c,ident)


def inspect_order(path, client, txid, now=None):
    """Read-only cumulative reconciliation of explicitly identified external orders."""
    now=now_sec(now)
    if not re.fullmatch(r'[A-Z0-9-]{5,40}',txid): raise ValueError('Invalid exchange order ID')
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); bind_account(c,client); c.commit()
    result=client.read('QueryOrders',txid=txid,trades='true')
    if set(result)!={txid}: raise ValueError('Order not uniquely found')
    r=result[txid]; status=r['status']
    if status not in ('pending','open','closed','canceled','expired'): raise ValueError('Unknown order status')
    volume,executed,cost,fee=(decimal(r[k],zero=True) for k in ('vol','vol_exec','cost','fee'))
    if not volume or executed>volume or (executed==0 and (cost or fee)) or (executed and not cost) or (status=='closed' and executed!=volume):
        raise ValueError('Inconsistent cumulative exchange order')
    trade_ids=r.get('trades',[])
    if not isinstance(trade_ids,list) or len(trade_ids)>200 or len(set(trade_ids))!=len(trade_ids) or any(not isinstance(t,str) or not re.fullmatch(r'[A-Z0-9-]{2,40}',t) for t in trade_ids):
        raise ValueError('Invalid trade identifiers')
    fills=[]
    for i in range(0,len(trade_ids),20):
        batch=trade_ids[i:i+20]; response=client.read('QueryTrades',txid=','.join(batch))
        if set(response)!=set(batch): raise ValueError('Missing exchange trades')
        for tid in batch:
            tr=response[tid]
            if tr['ordertxid']!=txid: raise ValueError('Trade belongs to another order')
            vals={k:fmt(decimal(tr[k],zero=(k=='fee'))) for k in ('vol','cost','fee','price')}
            fills.append({'id':tid,**vals})
    totals={k:sum((D(f[k]) for f in fills),D(0)) for k in ('vol','cost','fee')}
    # Exact reconciliation is intentionally conservative: venue rounding differences need review.
    reconciled=(totals['vol'],totals['cost'],totals['fee'])==(executed,cost,fee)
    item={'txid':txid,'status':status,'volume':fmt(volume),'executed':fmt(executed),'cost':fmt(cost),'fee':fmt(fee),
          'partial':0<executed<volume,'trade_ids':trade_ids,'fills':fills,'reconciled':reconciled}
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); old=c.execute('SELECT * FROM observed_orders WHERE txid=?',(txid,)).fetchone()
        if old and (now<old['observed'] or D(old['volume'])!=volume or executed<D(old['executed']) or cost<D(old['cost']) or fee<D(old['fee']) or (old['status'] in ('closed','canceled','expired') and (old['status']!=status or executed!=D(old['executed']) or cost!=D(old['cost']) or fee!=D(old['fee'])))):
            c.execute('UPDATE gateway SET stopped=1'); event(c,now,None,'order_discrepancy',{'txid':txid}); c.commit()
            raise ValueError('Exchange order regressed; gateway stopped')
        c.execute('INSERT INTO observed_orders VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(txid) DO UPDATE SET status=excluded.status,executed=excluded.executed,cost=excluded.cost,fee=excluded.fee,detail=excluded.detail,observed=excluded.observed',
                  (txid,status,fmt(volume),fmt(executed),fmt(cost),fmt(fee),dump(item),now))
        if not reconciled:
            c.execute('UPDATE gateway SET stopped=1'); event(c,now,None,'fill_discrepancy',{'txid':txid})
        event(c,now,None,'order_observed',item); c.commit()
    return item


def account_snapshot(path, client, now=None):
    """Compare real account observations to the first snapshot; never credit paper P&L."""
    now=now_sec(now)
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE'); bind_account(c,client); c.commit()
    raw=client.read('BalanceEx'); orders=client.read('OpenOrders').get('open')
    snapshot=normalized_account(raw,orders); balances=snapshot['balances']
    with connect(path) as c:
        c.execute('BEGIN IMMEDIATE')
        first=c.execute('SELECT detail FROM account_snapshots ORDER BY id LIMIT 1').fetchone()
        if first:
            baseline=json.loads(first[0])['snapshot']
            changes=changed_assets(baseline,snapshot)
        else: changes=[]
        discrepancy=bool(changes or orders or any(D(r['credit']) or D(r['credit_used']) or D(r['hold_trade']) for r in balances.values()))
        result={'snapshot':snapshot,'baseline':not bool(first),'changed_assets':changes,'needs_review':discrepancy}
        c.execute('INSERT INTO account_snapshots(observed,detail) VALUES(?,?)',(now,dump(result)))
        if discrepancy:
            c.execute('UPDATE gateway SET stopped=1'); event(c,now,None,'account_discrepancy',{'changed_assets':changes,'open_orders':len(orders)})
        c.commit(); return result


def status(path):
    with connect(path) as c:
        c.execute('BEGIN')
        return {'mode':'VALIDATION ONLY — no real orders','stopped':halted(c),
                'intents':[dict(r) for r in c.execute('SELECT * FROM intents ORDER BY created DESC,id LIMIT 100')],
                'account_snapshot':(lambda r:json.loads(r[0]) if r else None)(c.execute('SELECT detail FROM account_snapshots ORDER BY id DESC LIMIT 1').fetchone()),
                'external_orders':[json.loads(r['detail']) for r in c.execute('SELECT * FROM observed_orders ORDER BY observed DESC LIMIT 100')]}


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/gateway.sqlite'); s=p.add_subparsers(dest='cmd',required=True)
    s.add_parser('init'); s.add_parser('status'); s.add_parser('stop'); s.add_parser('recover'); s.add_parser('account')
    a=s.add_parser('propose-paper'); a.add_argument('--source',required=True); a.add_argument('--fill-id',type=int,required=True); a.add_argument('--quantity',required=True); a.add_argument('--price',required=True)
    a=s.add_parser('propose'); a.add_argument('--reference',required=True); a.add_argument('--pair',choices=PAIRS,required=True); a.add_argument('--side',choices=['buy','sell'],required=True); a.add_argument('--quantity',required=True); a.add_argument('--price',required=True)
    for cmd in ('approve','validate','inspect-order'):
        a=s.add_parser(cmd); a.add_argument('id')
    x=p.parse_args(); init(x.db)
    if x.cmd=='init' or x.cmd=='status': result=status(x.db)
    elif x.cmd=='propose': result=propose(x.db,x.reference,x.pair,x.side,x.quantity,x.price)
    elif x.cmd=='propose-paper': result=propose_paper(x.db,x.source,x.fill_id,x.quantity,x.price)
    elif x.cmd=='approve': result=approve(x.db,x.id)
    elif x.cmd=='stop': stop(x.db); result={'stopped':True}
    elif x.cmd=='recover': result={'unknown':recover(x.db)}
    else:
        client=Kraken.from_env(x.db+'.nonce')
        result=account_snapshot(x.db,client) if x.cmd=='account' else validate_intent(x.db,x.id,client) if x.cmd=='validate' else inspect_order(x.db,client,x.id)
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__': main()

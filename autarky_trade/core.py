from __future__ import annotations
import csv, json, math, urllib.request, urllib.parse, time, random
from dataclasses import dataclass, asdict
from pathlib import Path

@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass(frozen=True)
class Strategy:
    kind: str
    fast: int
    slow: int
    name: str
    def __post_init__(self):
        if self.kind not in ('trend','breakout','reversion') or type(self.fast) is not int or type(self.slow) is not int or not 2 <= self.fast < self.slow <= 100 or not isinstance(self.name,str) or not 1 <= len(self.name) <= 80:
            raise ValueError('Invalid strategy')
    def signal(self, closes):
        if len(closes) < self.slow: return False
        f=sum(closes[-self.fast:])/self.fast; s=sum(closes[-self.slow:])/self.slow
        if self.kind=='trend': return f>s
        if self.kind=='breakout': return closes[-1]>max(closes[-self.slow:-1])
        return f<s and closes[-1]>closes[-2]

BASELINES=[Strategy('trend',5,20,'trend-5-20'),Strategy('trend',12,48,'trend-12-48'),Strategy('breakout',5,20,'breakout-20'),Strategy('reversion',5,30,'reversion-5-30')]

def request(url, payload=None):
    data=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request(url,data=data,headers={'User-Agent':'Autarky-research/0.4','Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=120) as r: return json.load(r)


def validate(bars, interval=None):
    """Reject malformed or discontinuous market data before any evaluation.

    Historical gaps cannot be silently treated as evenly spaced observations.
    Run `python -m autarky_trade.data_audit inspect` to locate them.
    """
    if interval is not None and (type(interval) is not int or interval <= 0):
        raise ValueError('Interval must be a positive number of seconds')
    if len(bars) < 220:
        raise ValueError('Need at least 220 complete candles')

    previous = None
    for index, bar in enumerate(bars):
        if type(bar.ts) is not int or bar.ts <= 0:
            raise ValueError(f'Invalid timestamp at bar {index}')
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f'Non-finite price or volume at bar {index}')
        if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
            raise ValueError(f'Invalid price or volume at bar {index}')
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError(f'Inconsistent OHLC range at bar {index}')
        if previous is not None:
            elapsed = bar.ts - previous
            if elapsed <= 0:
                raise ValueError(f'Duplicate or unsorted timestamp at bar {index}')
            if interval is not None and elapsed != interval:
                raise ValueError(f'Missing or irregular candle before bar {index}')
        previous = bar.ts
    return bars


def load_csv(path, interval=3600):
    """Read timestamp, OHLC and volume, ignoring optional trailing columns."""
    rows = []
    with open(path, newline='', encoding='utf-8-sig') as handle:
        reader = csv.reader(handle)
        for line_number, raw in enumerate(reader, start=1):
            if not raw or not any(field.strip() for field in raw):
                continue
            if line_number == 1 and raw[0].strip().lower() in ('timestamp', 'time', 'ts'):
                continue
            if len(raw) < 6:
                raise ValueError(f'CSV line {line_number}: expected timestamp, OHLC and volume')
            try:
                row = Bar(int(raw[0].strip()), *(float(value) for value in raw[1:6]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f'CSV line {line_number}: invalid timestamp or numeric value') from exc
            rows.append(row)
    return validate(rows, interval)

def kraken(pair='XBTUSD',interval=60):
    if pair not in ('XBTUSD','ETHUSD') or interval not in (15,60,240,1440): raise ValueError('Unsupported pair/interval')
    response=request('https://api.kraken.com/0/public/OHLC?'+urllib.parse.urlencode({'pair':pair,'interval':interval}))
    if response['error']: raise RuntimeError(response['error'])
    result=response['result']; key=next(k for k in result if k!='last')
    # Kraken includes the current, uncommitted bar; never use it as a signal.
    rows=result[key][:-1]
    return validate([Bar(int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[6])) for x in rows],interval*60)

def quote(pair):
    if pair not in ('XBTUSD','ETHUSD'): raise ValueError('Unsupported pair')
    r=request('https://api.kraken.com/0/public/Ticker?'+urllib.parse.urlencode({'pair':pair}))
    if r['error']: raise RuntimeError(r['error'])
    d=next(iter(r['result'].values())); bid,ask=float(d['b'][0]),float(d['a'][0])
    if not all(math.isfinite(value) for value in (bid, ask)) or not 0 < bid <= ask: raise ValueError('Invalid quote')
    return bid,ask

def synthetic(n=1000):
    rng=random.Random(17); p=100.; bars=[]
    for i in range(n):
        o=p; p*=math.exp((.001 if (i//120)%2==0 else -.0008)+rng.gauss(0,.008)); bars.append(Bar(1700000000+i*3600,o,max(o,p)*1.001,min(o,p)*.999,p,100))
    return bars

@dataclass(frozen=True)
class Costs:
    fee_bps: float=40
    slip_bps: float=10
    fraction: float=.25
    def __post_init__(self):
        if not all(math.isfinite(v) for v in (self.fee_bps,self.slip_bps,self.fraction)) or not 0<=self.fee_bps<=500 or not 0<=self.slip_bps<=500 or not 0<self.fraction<=.5: raise ValueError('Invalid cost/risk parameters')

@dataclass
class Account:
    cash: float=1000.
    units: float=0.
    trades: int=0
    fees: float=0.
    peak: float=1000.
    drawdown: float=0.
    stopped: bool=False
    def equity(self,price): return self.cash+self.units*price
    def fill(self,buy,bid,ask,costs):
        fee=costs.fee_bps/10000; slip=costs.slip_bps/10000
        if buy and not self.units and not self.stopped:
            spend=self.cash*costs.fraction; price=ask*(1+slip); self.units=spend/(price*(1+fee)); self.cash-=spend; self.fees+=self.units*price*fee; self.trades+=1
        elif not buy and self.units:
            price=bid*(1-slip); gross=self.units*price; self.cash+=gross*(1-fee); self.fees+=gross*fee; self.units=0; self.trades+=1
    def mark(self,price):
        e=self.equity(price); self.peak=max(self.peak,e); self.drawdown=max(self.drawdown,1-e/self.peak)
        if self.drawdown>=.08: self.stopped=True


def backtest(bars, strategy, costs, start, end, capital=1000.):
    """Signal on completed candles; fill at the following candle's open."""
    if not 0 < start < end <= len(bars):
        raise ValueError('Invalid backtest window')
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError('Starting capital must be positive and finite')

    account = Account(cash=capital, peak=capital)
    closes = [bar.close for bar in bars[:start]]
    for bar in bars[start:end]:
        should_buy = strategy.signal(closes) and not account.stopped
        account.fill(should_buy, bar.open, bar.open, costs)
        account.mark(bar.close)
        closes.append(bar.close)

    if account.units:
        account.fill(False, bars[end - 1].close, bars[end - 1].close, costs)
    account.mark(bars[end - 1].close)
    return {
        'return': account.cash / capital - 1,
        'trades': account.trades,
        'fees': account.fees,
        'drawdown': account.drawdown,
        'stopped': account.stopped,
    }

def evaluate(bars,strategies,costs):
    n=len(bars); split=int(n*.6); hold=int(n*.8)
    rows=[]
    for s in strategies:
        train=backtest(bars,s,costs,100,split); val=backtest(bars,s,costs,split,hold)
        rows.append({'strategy':asdict(s),'train':train,'validation':val})
    # Only training and validation determine the winner. The holdout is opened once for it.
    rows.sort(key=lambda x:x['validation']['return'],reverse=True)
    winner=rows[0]; s=Strategy(**winner['strategy']); test=backtest(bars,s,costs,hold,n)
    stress=backtest(bars,s,Costs(costs.fee_bps*2,costs.slip_bps*2,costs.fraction),hold,n)
    approved=all(x['return']>0 and x['trades']>=4 and x['drawdown']<.08 and not x['stopped'] for x in (winner['train'],winner['validation'],test,stress))
    return {'candidates':rows,'selected':winner['strategy'],'holdout':test,'holdout_stress':stress,'paper_eligible':approved,'holdout_end':bars[-1].ts,'costs':asdict(costs),'n':n,'holdout_buy_hold':bars[-1].close/bars[hold].open-1,'note':'One split, exploratory only; holdout is spent after this run.'}

def propose(model,bars):
    # Expose only training summary to the model. No shell, secrets, exchange credentials, or arbitrary code.
    t=bars[:int(len(bars)*.6)]; returns=[b.close/a.close-1 for a,b in zip(t,t[1:])]
    prompt=f'You are the research and strategy agent. Training bars: {len(t)}; mean one-bar return {sum(returns)/len(returns):.6f}; total return {t[-1].close/t[0].close-1:.4f}. Propose up to 5 long-only spot strategies using kind trend, breakout, or reversion, integers 2 <= fast < slow <= 100. Return JSON object with strategies array of objects containing kind, fast, slow, name. No claims of profit.'
    schema={'type':'object','properties':{'strategies':{'type':'array','maxItems':5,'items':{'type':'object','properties':{'kind':{'type':'string','enum':['trend','breakout','reversion']},'fast':{'type':'integer'},'slow':{'type':'integer'},'name':{'type':'string'}},'required':['kind','fast','slow','name'],'additionalProperties':False}}},'required':['strategies'],'additionalProperties':False}
    r=request('http://127.0.0.1:11434/api/chat',{'model':model,'messages':[{'role':'user','content':prompt}],'stream':False,'format':schema,'options':{'temperature':.3,'num_predict':600}})
    obj=json.loads(r['message']['content']); return [Strategy(**x) for x in obj['strategies'][:5]]

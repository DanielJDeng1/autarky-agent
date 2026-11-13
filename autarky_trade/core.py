from __future__ import annotations
import csv, json, math, urllib.request, urllib.parse, time, random
from dataclasses import dataclass, asdict
from pathlib import Path
from .signals import normalize, passes

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
    filters: tuple = ()
    hypothesis: str = ""
    def __post_init__(self):
        if self.kind not in ('trend','breakout','reversion') or type(self.fast) is not int or type(self.slow) is not int or not 2 <= self.fast < self.slow <= 100 or not isinstance(self.name,str) or not 1 <= len(self.name) <= 80:
            raise ValueError('Invalid strategy')
        if not isinstance(self.hypothesis,str) or len(self.hypothesis)>500:
            raise ValueError('Invalid hypothesis')
        object.__setattr__(self, 'filters', normalize(self.filters))
    def signal(self, closes):
        if len(closes) < self.slow or not passes(self.filters, closes): return False
        f=sum(closes[-self.fast:])/self.fast; s=sum(closes[-self.slow:])/self.slow
        if self.kind=='trend': return f>s
        if self.kind=='breakout': return closes[-1]>max(closes[-self.slow:-1])
        return f<s and closes[-1]>closes[-2]

BASELINES=[Strategy('trend',5,20,'trend-5-20'),Strategy('trend',12,48,'trend-12-48'),Strategy('breakout',5,20,'breakout-20'),Strategy('reversion',5,30,'reversion-5-30')]

def request(url, payload=None):
    data=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request(url,data=data,headers={'User-Agent':'Autarky-lab/0.2','Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=120) as r: return json.load(r)

def validate(bars, interval=None):
    if len(bars)<220: raise ValueError('Need at least 220 complete candles; use longer CSV history for serious research')
    for i,b in enumerate(bars):
        if type(b.ts) is not int or b.ts < 0: raise ValueError('Invalid candle timestamp')
        if not all(math.isfinite(v) for v in (b.open,b.high,b.low,b.close,b.volume)) or min(b.open,b.high,b.low,b.close)<=0 or b.volume<0 or b.low>min(b.open,b.close) or b.high<max(b.open,b.close): raise ValueError('Invalid candle')
        if i and (b.ts<=bars[i-1].ts or (interval and b.ts-bars[i-1].ts!=interval)): raise ValueError('Duplicate, unsorted or missing candle')
    return bars

def load_csv(path,interval=3600):
    with open(path,newline='') as f:
        reader=csv.reader(f); rows=[]
        for i,r in enumerate(reader):
            if i==0 and r[0].lower() in ('timestamp','time','ts'): continue
            rows.append(Bar(int(r[0]),*(float(x) for x in r[1:6])))
    return validate(rows,interval)

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
    if not 0<bid<=ask or not math.isfinite(ask): raise ValueError('Invalid quote')
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

def backtest(bars,strategy,costs,start,end,capital=1000.):
    a=Account(cash=capital,peak=capital); closes=[b.close for b in bars[:start]]
    for b in bars[start:end]:
        # Signal sees only prior completed candles. Fill at next open, not same close.
        a.fill(strategy.signal(closes) and not a.stopped,b.open,b.open,costs)
        a.mark(b.close); closes.append(b.close)
    if a.units: a.fill(False,bars[end-1].close,bars[end-1].close,costs)
    a.mark(bars[end-1].close)
    return {'return':a.cash/capital-1,'trades':a.trades,'fees':a.fees,'drawdown':a.drawdown,'stopped':a.stopped}

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

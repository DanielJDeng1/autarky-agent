"""Development-only, cost-aware strategy laboratory. Never promotes or trades."""
from __future__ import annotations
import argparse
import csv
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from contextlib import closing
import time

from .core import Account, Bar, Costs, request, validate
from .community import encode
from .models import solve

PROTOCOL = 'v0.11-development-1'
LIMIT = 100_000
WARMUP = 721
NORMAL = Costs(40, 20)
STRESS = Costs(80, 40)
FEATURES = ('momentum', 'distance', 'volatility', 'rsi', 'volume_ratio', 'breakout', 'forecast')


def digest(x):
    return hashlib.sha256(encode(x).encode()).hexdigest()


@dataclass(frozen=True)
class Rule:
    feature: str
    window: int
    op: str
    threshold: float

    def __post_init__(self):
        bounds = {'momentum': (-1, 5), 'distance': (-1, 5), 'volatility': (0, 2),
                  'rsi': (0, 100), 'volume_ratio': (0, 100), 'breakout': (-1, 5), 'forecast': (-1, 1)}
        if self.feature not in bounds or type(self.window) is not int or not 2 <= self.window <= 720 or self.op not in ('gt', 'lt'):
            raise ValueError('Invalid feature, window or comparator')
        lo, hi = bounds[self.feature]
        if type(self.threshold) not in (int, float) or not math.isfinite(self.threshold) or not lo <= self.threshold <= hi:
            raise ValueError('Invalid rule threshold')
        object.__setattr__(self, 'threshold', float(self.threshold))


@dataclass(frozen=True)
class Policy:
    name: str
    entry: tuple
    exit: tuple
    min_hold: int = 0
    cooldown: int = 0
    max_hold: int = 720
    fraction: float = .25
    hypothesis: str = ''
    predictor: dict | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 80 or not isinstance(self.hypothesis, str) or len(self.hypothesis) > 500:
            raise ValueError('Invalid policy metadata')
        for k in ('entry', 'exit'):
            rules = getattr(self, k)
            if not isinstance(rules, (tuple, list)) or not 1 <= len(rules) <= 4:
                raise ValueError('Each entry/exit needs 1..4 explicit rules')
            rules = tuple(r if isinstance(r, Rule) else Rule(**r) for r in rules)
            if len(set(rules)) != len(rules): raise ValueError('Duplicate rule')
            object.__setattr__(self, k, tuple(sorted(rules, key=lambda r: (r.feature,r.window,r.op,r.threshold))))
        if any(type(n) is not int for n in (self.min_hold,self.cooldown,self.max_hold)) or not 0 <= self.min_hold < self.max_hold <= 720 or not 0 <= self.cooldown <= 720:
            raise ValueError('Invalid holding/cooldown constraints')
        uses=[r for r in self.entry+self.exit if r.feature=='forecast']
        if bool(uses)!=(self.predictor is not None): raise ValueError('Forecast rules require exactly one predictor specification')
        if uses:
            p=self.predictor
            if not isinstance(p,dict) or set(p)!={'horizon','window','ridge'} or type(p['horizon']) is not int or not 2<=p['horizon']<=168 or type(p['window']) is not int or not 2<=p['window']<=168 or type(p['ridge']) not in (int,float) or not .001<=p['ridge']<=100 or any(r.window!=p['horizon'] for r in uses): raise ValueError('Invalid forecast specification')
            object.__setattr__(self,'predictor',dict(p))
        if type(self.fraction) not in (int,float) or not .01 <= self.fraction <= .25: raise ValueError('Invalid sizing')

    @property
    def identity(self):
        d = asdict(self); d.pop('name'); d.pop('hypothesis')
        return digest(d)

    def description(self):
        def rules(rs): return ['%s(%d) %s %g' % (r.feature,r.window,'>' if r.op=='gt' else '<',r.threshold) for r in rs]
        return {'entry_all':rules(self.entry),'exit_any':rules(self.exit),'min_hold_bars':self.min_hold,
                'cooldown_bars':self.cooldown,'max_hold_bars':self.max_hold,'cash_fraction':self.fraction,
                'predictor':self.predictor,'risk_override':'8% account drawdown; checked at next open and close; no intrabar OHLC fill assumption'}


class Features:
    """Cache causal features; index i is the decision BEFORE bar i opens."""
    def __init__(self,bars):
        self.bars=bars; self.cache={}
        self.close=[b.close for b in bars]; self.volume=[b.volume for b in bars]
        ret=[0.]+[b/a-1 for a,b in zip(self.close,self.close[1:])]
        delta=[0.]+[b-a for a,b in zip(self.close,self.close[1:])]
        def prefix(xs):
            out=[0.]
            for x in xs: out.append(out[-1]+x)
            return out
        self.sums={k:prefix(v) for k,v in {'close':self.close,'volume':self.volume,'ret':ret,'square':[x*x for x in ret],
                    'gain':[max(x,0) for x in delta],'loss':[max(-x,0) for x in delta]}.items()}

    def series(self,feature,w):
        if feature not in FEATURES or feature=='forecast': raise ValueError('Use fold-specific forecast')
        key=(feature,w)
        if key in self.cache: return self.cache[key]
        a=[None]*len(self.bars)
        def total(k,lo,hi): return self.sums[k][hi]-self.sums[k][lo]
        # breakout uses preceding highs excluding most recent completed close, via monotone deque.
        from collections import deque
        q=deque()
        for i in range(1,len(a)):
            j=i-2
            if feature=='breakout' and j>=0:
                while q and self.bars[q[-1]].high <= self.bars[j].high: q.pop()
                q.append(j)
                while q and q[0]<i-w-1: q.popleft()
            if i<=w: continue
            p=self.close[i-1]
            if feature=='momentum': v=p/self.close[i-w-1]-1
            elif feature=='distance': v=p/(total('close',i-w,i)/w)-1
            elif feature=='volatility':
                mean=total('ret',i-w,i)/w; v=math.sqrt(max(0,total('square',i-w,i)/w-mean*mean))
            elif feature=='rsi':
                g,l=total('gain',i-w,i),total('loss',i-w,i); v=100*g/(g+l) if g+l else 50.
            elif feature=='volume_ratio':
                avg=total('volume',i-w-1,i-1)/w; v=self.volume[i-1]/avg if avg>0 else None
            else: v=p/self.bars[q[0]].high-1
            a[i]=v
        self.cache[key]=a; return a


def forecast_fold(bars, f, spec, start, end):
    """Fit on earlier open-to-open labels; the last label ends strictly before the fold."""
    h,w,reg=spec['horizon'],spec['window'],spec['ridge']
    cols=[f.series(k,w) for k in ('momentum','distance','volatility','rsi')]
    indices=range(max(w+1,start-8760),start-h)
    x=[[col[i] for col in cols] for i in indices]; y=[bars[i+h].open/bars[i].open-1 for i in indices]
    n=len(y)
    if n<100: raise ValueError('Insufficient pre-fold forecast labels')
    means=[sum(col)/n for col in zip(*x)]
    scales=[max(1e-8,math.sqrt(sum((v-m)**2 for v in col)/n)) for col,m in zip(zip(*x),means)]
    z=[[(v-m)/scale for v,m,scale in zip(row,means,scales)] for row in x]; intercept=sum(y)/n
    a=[[sum(row[j]*row[k] for row in z)/n+(reg if j==k else 0.) for k in range(4)] for j in range(4)]
    b=[sum(row[j]*(v-intercept) for row,v in zip(z,y))/n for j in range(4)]
    weights=solve(a,b); predictions=[None]*len(bars)
    for i in range(start,end): predictions[i]=intercept+sum(weight*(col[i]-m)/scale for weight,col,m,scale in zip(weights,cols,means,scales))
    artifact={'horizon':h,'window':w,'ridge':reg,'means':means,'scales':scales,'weights':weights,'intercept':intercept,'samples':n,'first_label_open':bars[indices.start].ts,'last_label_end':bars[indices.stop-1+h].ts}
    artifact['training_sha256']=digest([asdict(v) for v in bars[indices.start-w-1:indices.stop+h]])
    return predictions,artifact



def forecast_diagnostics(bars, predictions, start, end, horizon):
    """Nonoverlapping in-window labels; descriptive accuracy, not a trading gate."""
    pairs=[(predictions[i], bars[i+horizon].open/bars[i].open-1)
           for i in range(start,end-horizon,horizon) if predictions[i] is not None]
    if not pairs: return {'observations':0}
    n=len(pairs)
    return {'observations':n,'rmse':math.sqrt(sum((p-y)**2 for p,y in pairs)/n),
            'zero_forecast_rmse':math.sqrt(sum(y*y for _,y in pairs)/n),
            'direction_accuracy':sum((p>0)==(y>0) for p,y in pairs)/n,
            'mean_prediction':sum(p for p,_ in pairs)/n,
            'mean_realized':sum(y for _,y in pairs)/n,
            'note':'Nonoverlapping labels; forecast accuracy is not economic performance.'}


def window_regime(bars,start,end):
    """Ex-post window description only; never input to a policy decision."""
    rs=[bars[i].close/bars[i-1].close-1 for i in range(start,end)]
    mean=sum(rs)/len(rs)
    change=bars[end-1].close/bars[start].open-1
    return {'market_return':change,'hourly_volatility':math.sqrt(sum((r-mean)**2 for r in rs)/len(rs)),
            'direction':'up' if change>.05 else 'down' if change<-.05 else 'sideways'}

def simulate(bars,policy,start,end,costs=NORMAL,features=None,forecast=None):
    if not WARMUP<=start<end<=len(bars): raise ValueError('Invalid fold bounds')
    f=features or Features(bars)
    if policy.predictor and forecast is None: forecast=forecast_fold(bars,f,policy.predictor,start,end)[0]
    def series(r): return forecast if r.feature=='forecast' else f.series(r.feature,r.window)
    entry=[(r,series(r)) for r in policy.entry]
    exit_rules=[(r,series(r)) for r in policy.exit]
    def passed(r,v): return v is not None and (v>r.threshold if r.op=='gt' else v<r.threshold)
    a=Account(); shadow=Account(); capital=1000.; held_at=None; next_entry=start; exposure=0; opportunities=0; blocked=0; rounds=0
    turnover=0.; friction=0.; reasons={}; returns=[]; previous=capital
    cost=Costs(costs.fee_bps,costs.slip_bps,policy.fraction); free=Costs(0,0,policy.fraction)
    def fill(buy,price,reason,i):
        nonlocal held_at,next_entry,rounds,turnover,friction
        before=a.equity(price); units=a.units
        a.fill(buy,price,price,cost); shadow.fill(buy,price,price,free)
        turnover+=abs(a.units-units)*price; friction+=before-a.equity(price)
        reasons[reason]=reasons.get(reason,0)+1
        if buy: held_at=i
        else: held_at=None; next_entry=i+policy.cooldown+1; rounds+=1
    for i in range(start,end):
        b=bars[i]; a.mark(b.open)
        signal=all(passed(r,s[i]) for r,s in entry); opportunities+=int(signal)
        if a.units:
            age=i-held_at
            if a.stopped: fill(False,b.open,'risk',i)
            elif age>=policy.max_hold: fill(False,b.open,'max_hold',i)
            elif age>=policy.min_hold and any(passed(r,s[i]) for r,s in exit_rules): fill(False,b.open,'signal_exit',i)
        elif signal and not a.stopped:
            if i>=next_entry: fill(True,b.open,'entry',i)
            else: blocked+=1
        exposure+=int(bool(a.units)); a.mark(b.close)
        # Risk observed at close: exit next open, never assume an intrabar stop fill.
        if i==end-1 and a.units: fill(False,b.close,'fold_end',i); a.mark(b.close)
        equity=a.equity(b.close); returns.append(math.log(equity/previous)); previous=equity
    net=a.cash/capital-1; gross=shadow.cash/capital-1
    return {'net_return':net,'same_trade_path_gross_return':gross,'cost_drag_return':gross-net,
            'fees':a.fees,'execution_friction':friction-a.fees,'turnover':turnover/capital,
            'fills':a.trades,'round_trips':rounds,'drawdown':a.drawdown,'stopped':a.stopped,
            'exposure_fraction':exposure/(end-start),'entry_opportunities':opportunities,'cooldown_blocked':blocked,
            'reasons':reasons,'log_returns':returns}


def defaults():
    def r(f,w,o,t): return Rule(f,w,o,t)
    return [Policy('trend-daily', (r('momentum',24,'gt',.015),r('distance',72,'gt',0)),(r('momentum',24,'lt',-.005),),6,6,168),
            Policy('trend-weekly', (r('momentum',168,'gt',.03),r('distance',336,'gt',0)),(r('momentum',72,'lt',-.01),),24,24,720),
            Policy('breakout-volume', (r('breakout',72,'gt',.002),r('volume_ratio',24,'gt',1.2)),(r('distance',72,'lt',-.01),),12,12,336),
            Policy('reversion-recovery', (r('distance',72,'lt',-.025),r('momentum',6,'gt',.005)),(r('distance',72,'gt',0),r('momentum',24,'lt',-.05)),6,12,168),
            Policy('trend-volatility', (r('momentum',72,'gt',.025),r('volatility',24,'lt',.02)),(r('momentum',24,'lt',-.01),),12,12,336),
            Policy('ridge-24-sign', (r('forecast',24,'gt',0),),(r('forecast',24,'lt',0),),0,0,72,predictor={'horizon':24,'window':72,'ridge':1.}),
            Policy('ridge-24-cost-aware', (r('forecast',24,'gt',.012),),(r('forecast',24,'lt',0),),0,0,72,predictor={'horizon':24,'window':72,'ridge':1.})]


def propose(model,bars):
    """Only development data, typed executable rules; no prose-to-code interpretation."""
    rule={'type':'object','properties':{'feature':{'type':'string','enum':[f for f in FEATURES if f!='forecast']},'window':{'type':'integer','minimum':2,'maximum':720},'op':{'type':'string','enum':['gt','lt']},'threshold':{'type':'number'}},'required':['feature','window','op','threshold'],'additionalProperties':False}
    props={'name':{'type':'string'},'entry':{'type':'array','minItems':1,'maxItems':4,'items':rule},'exit':{'type':'array','minItems':1,'maxItems':4,'items':rule},'min_hold':{'type':'integer'},'cooldown':{'type':'integer'},'max_hold':{'type':'integer'},'fraction':{'type':'number','enum':[0.25]},'hypothesis':{'type':'string'}}
    schema={'type':'object','properties':{'policies':{'type':'array','minItems':1,'maxItems':4,'items':{'type':'object','properties':props,'required':list(props),'additionalProperties':False}}},'required':['policies'],'additionalProperties':False}
    summary={'bars':len(bars),'first':bars[0].ts,'last':bars[-1].ts,'return':bars[-1].close/bars[0].close-1,'hourly_volatility':Features(bars).series('volatility',168)[-1]}
    payload={'model':model,'stream':False,'think':False,'format':schema,'messages':[{'role':'user','content':f'Design 1-4 long-only hourly BTC/ETH development policies. Entry ALL and exit ANY; only explicit rules execute. Momentum fractional return; distance close/SMA-1; volatility standard deviation of hourly returns; RSI 0-100; volume_ratio last volume/prior mean; breakout last close/prior maximum high-1. Windows 2..720, min_hold < max_hold <=720, cooldown 0..720, fraction exactly 0.25 for matched comparisons. Prefer multi-day hypotheses and account for 1.2% approximate round-trip friction. State falsifiable rationale matching rules, no profit claims. Development summary: {encode(summary)}'}],'options':{'temperature':.3,'num_predict':4096}}
    for _ in range(2):
        try:
            obj=json.loads(request('http://127.0.0.1:11434/api/chat',payload)['message']['content'])
            if not isinstance(obj['policies'],list) or not 1<=len(obj['policies'])<=4: raise ValueError('Invalid policy count')
            return [Policy(**dict(p, fraction=.25)) for p in obj['policies']]
        except (ValueError,KeyError,TypeError): continue
    raise ValueError('Ollama did not return valid executable policies')


def csv_history(path,cutoff):
    """Read latest contiguous hourly suffix, never manufacture absent candles."""
    from collections import deque
    bars=deque(maxlen=LIMIT); previous=None
    with open(path,newline='',encoding='utf-8-sig') as f:
        for i,row in enumerate(csv.reader(f)):
            if not row: raise ValueError('Empty CSV row')
            if i==0 and row[0].lower() in ('timestamp','time','ts'): continue
            b=Bar(int(row[0]),*(float(x) for x in row[1:6]))
            if previous is not None and b.ts<=previous: raise ValueError('Duplicate or unsorted CSV timestamps')
            if b.ts>cutoff: break
            if previous is not None and b.ts-previous!=3600: bars.clear()
            bars.append(b); previous=b.ts
    return validate(list(bars),3600)


def history(source,cutoff):
    """Read a bounded chronological development prefix from the existing hourly archive."""
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        pair,interval=c.execute('SELECT pair,interval FROM treasury').fetchone()
        if interval!=60 or pair not in ('XBTUSD','ETHUSD'): raise ValueError('Lab supports hourly BTC/ETH archives')
        rows=list(c.execute('SELECT ts,open,high,low,close,volume FROM market_bars WHERE ts<=? ORDER BY ts DESC LIMIT ?',(cutoff,LIMIT)))
    bars=[]
    for row in rows:
        b=Bar(*row)
        if bars and bars[-1].ts-b.ts!=3600: break
        bars.append(b)
    return pair,validate(list(reversed(bars)),3600)


def evaluate(bars,pair,cutoff,policies,model=None,folds=6,now=None):
    """Reusable development folds, isolated from all promotion/final ledgers."""
    now=int(time.time()) if now is None else now
    if pair not in ('XBTUSD','ETHUSD') or type(cutoff) is not int or type(folds) is not int or not 3<=folds<=12: raise ValueError('Invalid lab configuration')
    bars=validate(bars,3600)
    if len(bars)>LIMIT or bars[-1].ts>cutoff or bars[-1].ts+3600>now or len(bars)<WARMUP+folds*168: raise ValueError('Need >=721 warmup + 168 bars/fold, closed development data only')
    policies=list({p.identity:p for p in policies}.values())
    if not 1<=len(policies)<=32: raise ValueError('Need 1..32 unique policies')
    key=digest([pair,3600,[asdict(b) for b in bars]])
    bounds=[WARMUP+(len(bars)-WARMUP)*i//folds for i in range(folds+1)]
    f=Features(bars); results=[]
    for p in policies:
        windows=[]
        for start,end in zip(bounds,bounds[1:]):
            pred,artifact=forecast_fold(bars,f,p.predictor,start,end) if p.predictor else (None,None)
            n=simulate(bars,p,start,end,NORMAL,f,pred); s=simulate(bars,p,start,end,STRESS,f,pred)
            # Allocation-matched passive baseline, identical entry/exit friction and starting cash.
            passive=Account(); passive.fill(True,bars[start].open,bars[start].open,Costs(40,20,p.fraction)); passive.fill(False,bars[end-1].close,bars[end-1].close,Costs(40,20,p.fraction))
            n.pop('log_returns'); s.pop('log_returns')
            windows.append({'start':bars[start].ts,'end':bars[end-1].ts,'normal':n,'stress':s,'passive_return':passive.cash/1000-1,'model_artifact':artifact,
                            'market_regime':window_regime(bars,start,end),
                            'forecast_accuracy':forecast_diagnostics(bars,pred,start,end,p.predictor['horizon']) if p.predictor else None})
        ns=[w['normal'] for w in windows]; ss=[w['stress'] for w in windows]
        net=sum(x['net_return'] for x in ns)/folds; gross=sum(x['same_trade_path_gross_return'] for x in ns)/folds
        fills=sum(x['fills'] for x in ns); positives=sum(x['net_return']>0 for x in ns)
        reason='no trades' if not fills else 'cost dominated' if gross>0>=net else 'negative gross edge' if gross<=0 else 'unstable across windows' if positives<folds else 'positive development only'
        results.append({'policy':asdict(p),'identity':p.identity,'executable_description':p.description(),'windows':windows,'mean_net':net,'mean_gross':gross,
                        'mean_stress':sum(x['net_return'] for x in ss)/folds,'mean_passive':sum(w['passive_return'] for w in windows)/folds,'positive_windows':positives,'fills':fills,'diagnosis':reason,
                        'mean_turnover':sum(x['turnover'] for x in ns)/folds,
                        'mean_cost_drag':gross-net,'worst_drawdown':max(x['drawdown'] for x in ns),
                        'stopped_windows':sum(x['stopped'] for x in ns)})
        f.cache.clear()
    results.sort(key=lambda r:(r['mean_net'],r['mean_stress']),reverse=True)
    by_name={r['policy']['name']:r for r in results}
    comparisons=[]
    if 'ridge-24-sign' in by_name and 'ridge-24-cost-aware' in by_name:
        base,variant=(by_name[n] for n in ('ridge-24-sign','ridge-24-cost-aware'))
        # Only call this an ablation if both policies are exactly the built-in variants.
        expected={p.name:p.identity for p in defaults()}
        if all(r['identity']==expected[r['policy']['name']] for r in (base,variant)):
            comparisons.append({'base':base['policy']['name'],'variant':variant['policy']['name'],
                'change':'entry forecast threshold only: 0 to 0.012',
                'net_return_delta':variant['mean_net']-base['mean_net'],
                'turnover_delta':variant['mean_turnover']-base['mean_turnover'],
                'fills_delta':variant['fills']-base['fills'],
                'note':'Matched development ablation, not independent evidence.'})
    report={'protocol':PROTOCOL,'dataset_sha256':key,'pair':pair,'cutoff':cutoff,'first':bars[0].ts,'last':bars[-1].ts,'bars':len(bars),'folds':folds,'model':model,'development_only':True,'eligible':False,'comparisons':comparisons,'results':results}
    return report


def run(path,bars,pair,cutoff,policies,model=None,folds=6,now=None):
    now=int(time.time()) if now is None else now
    report=evaluate(bars,pair,cutoff,policies,model,folds,now)
    key=report['dataset_sha256']
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path,timeout=30)) as c,c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables-{'experiments'}: raise ValueError('Use a separate development lab database')
        c.execute('CREATE TABLE IF NOT EXISTS experiments(id INTEGER PRIMARY KEY,created INTEGER NOT NULL,dataset TEXT NOT NULL,report TEXT NOT NULL)')
        c.execute('BEGIN IMMEDIATE'); count=c.execute('SELECT count(*) FROM experiments WHERE dataset=?',(key,)).fetchone()[0]
        report['prior_dataset_experiments']=count; report['prior_lab_experiments']=c.execute('SELECT count(*) FROM experiments').fetchone()[0]; report['experiment_id']=c.execute('INSERT INTO experiments(created,dataset,report) VALUES(?,?,?)',(now,key,encode(report))).lastrowid
        c.execute('UPDATE experiments SET report=? WHERE id=?',(encode(report),report['experiment_id']))
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/lab.sqlite'); sub=p.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('run'); src=a.add_mutually_exclusive_group(required=True); src.add_argument('--source'); src.add_argument('--csv'); a.add_argument('--pair',choices=['XBTUSD','ETHUSD']); a.add_argument('--cutoff',type=int,required=True); a.add_argument('--folds',type=int,default=6); a.add_argument('--model'); a.add_argument('--policies'); sub.add_parser('status')
    x=p.parse_args()
    if x.cmd=='status':
        with closing(sqlite3.connect(Path(x.db).resolve().as_uri()+'?mode=ro',uri=True)) as c: result=[json.loads(r[0]) for r in c.execute('SELECT report FROM experiments ORDER BY id DESC LIMIT 5')]
    else:
        if x.source:
            if Path(x.source).resolve()==Path(x.db).resolve(): p.error('Separate lab database required')
            pair,bars=history(x.source,x.cutoff)
            if x.pair and x.pair!=pair: p.error('Pair mismatch')
        else:
            if not x.pair: p.error('--pair required with CSV')
            pair=x.pair; bars=csv_history(x.csv,x.cutoff)
        if len(bars)<WARMUP+x.folds*168 or bars[-1].ts+3600>int(time.time()): p.error('Need sufficient closed development history before generating proposals')
        ps=defaults()
        if x.policies: ps.extend(Policy(**v) for v in json.loads(Path(x.policies).read_text()))
        if x.model: ps.extend(propose(x.model,bars))
        result=run(x.db,bars,pair,x.cutoff,ps,x.model,x.folds)
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__': main()

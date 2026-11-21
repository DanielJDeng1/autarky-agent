"""Small deterministic predictive model; typed, serializable, no executable payloads.

Fit only on the supplied chronological training prefix. A feature at decision i
uses closes through i-1, and the label is open[i+1]/open[i]-1. The last training
bar can supply an exit price but cannot supply a label crossing the boundary.
"""
from __future__ import annotations
import hashlib
import json
import math
from dataclasses import asdict
from .signals import feature_value

FEATURES = ('momentum', 'distance', 'volatility', 'rsi')


def features(closes, window):
    return [feature_value(f, window, closes) for f in FEATURES]


def normalize_model(model):
    if model is None:
        return None
    keys = {'version','window','ridge','threshold','means','scales','weights','intercept','fit_end','training_sha256','samples'}
    if not isinstance(model, dict) or set(model) != keys or model['version'] != 1:
        raise ValueError('Invalid model artifact')
    if type(model['window']) is not int or not 2 <= model['window'] <= 100:
        raise ValueError('Invalid model window')
    if type(model['fit_end']) is not int or model['fit_end'] < 0 or type(model['samples']) is not int or model['samples'] < 100:
        raise ValueError('Invalid training metadata')
    digest=model['training_sha256']
    if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid training digest')
    def number(x):
        if type(x) not in (int,float) or not math.isfinite(x) or abs(x)>1e6:
            raise ValueError('Invalid model number')
        return float(x)
    result=dict(model)
    for key in ('means','scales','weights'):
        if not isinstance(model[key],(list,tuple)) or len(model[key])!=len(FEATURES):
            raise ValueError('Invalid model vector')
        result[key]=tuple(number(x) for x in model[key])
    for key in ('ridge','threshold','intercept'): result[key]=number(model[key])
    if min(result['scales'])<=0 or not 0<result['ridge']<=100 or not 0<=result['threshold']<=.1:
        raise ValueError('Invalid model scale or hyperparameter')
    return result


def predict(model, closes):
    x=features(closes,model['window'])
    if any(v is None for v in x): return None
    return model['intercept']+sum(w*(v-m)/s for w,v,m,s in zip(model['weights'],x,model['means'],model['scales']))


def solve(a,b):
    """Pivoted elimination for a tiny, strictly regularized normal equation."""
    n=len(b); rows=[list(r)+[v] for r,v in zip(a,b)]
    for i in range(n):
        k=max(range(i,n),key=lambda j:abs(rows[j][i])); rows[i],rows[k]=rows[k],rows[i]
        pivot=rows[i][i]
        if abs(pivot)<1e-12: raise ValueError('Singular model fit')
        rows[i]=[x/pivot for x in rows[i]]
        for j in range(n):
            if j!=i:
                f=rows[j][i]; rows[j]=[x-f*y for x,y in zip(rows[j],rows[i])]
    return [r[-1] for r in rows]


def fit(training, window=20, ridge=1., threshold=0.):
    if type(window) is not int or not 2<=window<=100 or not 0<ridge<=100 or not 0<=threshold<=.1:
        raise ValueError('Invalid model hyperparameters')
    closes=[b.close for b in training]; x=[]; y=[]
    for i in range(window+1,len(training)-1):
        x.append(features(closes[i-window-1:i],window))
        y.append(training[i+1].open/training[i].open-1)
    n=len(y)
    if n<100: raise ValueError('Need at least 100 labeled training observations')
    means=[sum(col)/n for col in zip(*x)]
    scales=[max(1e-8,math.sqrt(sum((v-m)**2 for v in col)/n)) for col,m in zip(zip(*x),means)]
    z=[[(v-m)/s for v,m,s in zip(row,means,scales)] for row in x]; intercept=sum(y)/n
    d=len(FEATURES)
    a=[[sum(row[j]*row[k] for row in z)/n+(ridge if j==k else 0.) for k in range(d)] for j in range(d)]
    b=[sum(row[j]*(v-intercept) for row,v in zip(z,y))/n for j in range(d)]
    digest=hashlib.sha256(json.dumps([asdict(v) for v in training],separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return normalize_model(dict(version=1,window=window,ridge=ridge,threshold=threshold,means=means,scales=scales,weights=solve(a,b),intercept=intercept,fit_end=training[-1].ts,training_sha256=digest,samples=n))


def rolling_validation(bars, artifact, bounds, costs):
    """Diagnostic of a predeclared refit policy, separate from the frozen model gate.

    Each fold refits on observations strictly before its first decision. It is
    not the performance of the fixed artifact and cannot replace its evaluation.
    """
    from .core import Strategy, backtest
    result=[]
    for start,end in zip(bounds,bounds[1:]):
        trained=fit(bars[:start],artifact['window'],artifact['ridge'],artifact['threshold'])
        strategy=Strategy('ridge',2,trained['window'],'rolling diagnostic',model=trained)
        result.append({'fit_end':trained['fit_end'],'train_sha256':trained['training_sha256'],
                       'start':bars[start].ts,'end':bars[end-1].ts,
                       'metrics':backtest(bars,strategy,costs,start,end)})
    return result

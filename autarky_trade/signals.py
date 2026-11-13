"""Bounded, declarative close-price features. No generated Python is executed."""
from __future__ import annotations
import math

FEATURES = {'momentum', 'volatility', 'distance', 'rsi'}


def normalize(filters):
    if not isinstance(filters, (list, tuple)) or len(filters) > 3:
        raise ValueError('At most three signal filters')
    result = []
    for rule in filters:
        if not isinstance(rule, dict) or set(rule) != {'feature', 'window', 'op', 'threshold'}:
            raise ValueError('Invalid signal filter fields')
        feature, window, op, threshold = (rule[k] for k in ('feature','window','op','threshold'))
        if feature not in FEATURES or type(window) is not int or not 2 <= window <= 100 or op not in ('gt','lt'):
            raise ValueError('Invalid signal feature, window or comparison')
        if type(threshold) not in (int,float) or not math.isfinite(threshold):
            raise ValueError('Invalid signal threshold')
        low, high = (0,100) if feature == 'rsi' else (0,2) if feature == 'volatility' else (-1,2)
        if not low <= threshold <= high:
            raise ValueError('Signal threshold outside bounds')
        result.append(dict(feature=feature, window=window, op=op, threshold=float(threshold)))
    return tuple(sorted(result,key=lambda r:(r['feature'],r['window'],r['op'],r['threshold'])))


def feature_value(feature, window, closes):
    if len(closes) < window + 1:
        return None
    x = closes[-window-1:]
    if feature == 'momentum':
        return x[-1]/x[0]-1
    if feature == 'distance':
        return x[-1]/(sum(x[1:])/window)-1
    if feature == 'volatility':
        r = [b/a-1 for a,b in zip(x,x[1:])]
        mean = sum(r)/window
        return math.sqrt(sum((v-mean)**2 for v in r)/window)
    if feature == 'rsi':
        changes = [b-a for a,b in zip(x,x[1:])]
        gains = sum(max(v,0) for v in changes)
        losses = sum(max(-v,0) for v in changes)
        return 50. if gains+losses == 0 else 100*gains/(gains+losses)
    raise ValueError('Unknown feature')


def passes(filters, closes):
    for rule in filters:
        value = feature_value(rule['feature'],rule['window'],closes)
        if value is None:
            return False
        if rule['op'] == 'gt' and not value > rule['threshold']:
            return False
        if rule['op'] == 'lt' and not value < rule['threshold']:
            return False
    return True


def filter_schema():
    return {'type':'array','maxItems':3,'items':{'type':'object','properties':{
        'feature':{'type':'string','enum':sorted(FEATURES)},'window':{'type':'integer','minimum':2,'maximum':100},
        'op':{'type':'string','enum':['gt','lt']},'threshold':{'type':'number'}},
        'required':['feature','window','op','threshold'],'additionalProperties':False}}

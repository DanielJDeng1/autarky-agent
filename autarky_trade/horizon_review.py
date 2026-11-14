"""Matched multi-horizon review of frozen cross-market research nodes; development only."""
import argparse
import json
from pathlib import Path

from . import autonomous_research as old, cross_asset as cross, cross_attribution as attribution
from . import lab, multiyear, research_tree as tree, weekly_models as wm


def review(nodes, parts, cutoff, horizons=wm.HORIZONS, now=None):
    if not nodes or not horizons or len(set(horizons)) != len(horizons):
        raise ValueError('Require distinct horizons and completed research nodes')
    for h in horizons:
        wm.horizon_slots(h)
    original = {p:old.fingerprint(parts[p],p) for p in old.PAIRS}
    aligned = cross.align(parts)
    hashes = {p:old.fingerprint(aligned[p],p) for p in old.PAIRS}
    dataset = lab.digest([cross.PROTOCOL,cutoff,original,hashes])
    if any(n['dataset'] != dataset for n in nodes):
        raise ValueError('Node and historical dataset/cutoff differ')
    results = []
    common = None
    for n in nodes:
        spec = tree.Spec(**n['spec']); parent = tree.Spec(**n['parent_spec'])
        for h in horizons:
            reports = {}
            for p in old.PAIRS:
                other = next(q for q in old.PAIRS if q != p)
                report = tree.evaluate(aligned[p],p,cutoff,parent,spec,now,
                                       other_parts=aligned[other],horizon=h)
                dates = [w['start'] for w in report['details']]
                if common is None: common = dates
                if dates != common:
                    raise ValueError('Horizon evaluation windows do not match')
                reports[p] = report
            findings = tree.facts(reports)
            metrics = {}
            for p,r in reports.items():
                m = r['summary']
                metrics[p] = dict(findings['markets'][p],
                    parent=m['parent'],mean=m['mean'],candidate=m['candidate'],
                    # Same horizon and observed labels; relative skill is comparable across horizons.
                    rmse_skill=1-m['candidate']['rmse']/m['mean']['rmse'] if m['mean']['rmse'] else None)
            results.append({'id':n['id'],'agent':n['agent'],'parent':n['parent'],
                            'horizon_hours':h,'opportunities_per_window':wm.horizon_slots(h),
                            'spec':n['spec'],'parent_spec':n['parent_spec'],
                            'markets':metrics,'classification':findings['classification'],
                            'reports':reports})
    return {'protocol':wm.HORIZON_PROTOCOL,'dataset':dataset,'datasets':hashes,'cutoff':cutoff,
            'windows':len(common),'window_starts':common,'results':results,
            'development_only':True,'eligible':False,
            'note':'Same observed 30-day windows, 12 prior windows minimum, 26-window rolling training/calibration; horizon-specific labels and trading costs. Explicit 168-hour review uses the new protocol, not the original weekly calibration. RMSE skill is relative to same-horizon mean. Returns include cash abstention and reset every window. No prospective validation or promotion.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True); p.add_argument('--node',type=int,nargs='+',required=True)
    p.add_argument('--btc',required=True); p.add_argument('--eth',required=True)
    p.add_argument('--cutoff',type=int,required=True); p.add_argument('--output',required=True)
    p.add_argument('--horizons',type=int,nargs='+',choices=wm.HORIZONS,default=list(wm.HORIZONS))
    x=p.parse_args(); out=Path(x.output)
    if out.resolve() in {Path(y).resolve() for y in (x.db,x.btc,x.eth)} or out.exists():
        p.error('Choose a new report path distinct from inputs')
    nodes=attribution.load_nodes(x.db,x.node)
    result=review(nodes,{'XBTUSD':multiyear.segments(x.btc,x.cutoff),
                         'ETHUSD':multiyear.segments(x.eth,x.cutoff)},x.cutoff,x.horizons)
    out.parent.mkdir(parents=True,exist_ok=True); temp=out.with_name(out.name+'.partial')
    with temp.open('x',encoding='utf-8') as f: json.dump(result,f,indent=2,allow_nan=False)
    temp.replace(out)
    print(json.dumps({**result,'results':[{k:v for k,v in r.items() if k!='reports'} for r in result['results']]},indent=2,allow_nan=False))


if __name__=='__main__': main()

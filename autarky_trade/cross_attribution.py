"""Read-only matched peer-feature ablation of completed cross-market research nodes."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

PROTOCOL = "v0.19-cross-attribution-1"
CROSS_MODE = "v0.18-cross-asset-1"
PAIRS = ("XBTUSD", "ETHUSD")


def load_nodes(path, ids):
    if not ids or len(set(ids)) != len(ids) or any(type(i) is not int or i < 1 for i in ids):
        raise ValueError("Provide distinct positive node IDs")
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)) as c:
        nodes = []
        for i in ids:
            row = c.execute("SELECT dataset,parent,agent,status,report FROM tree_nodes WHERE id=?", (i,)).fetchone()
            if not row:
                raise ValueError(f"Unknown node {i}")
            dataset,parent,agent,status,raw = row
            record = json.loads(raw)
            if status != "complete" or record.get("research_mode") != CROSS_MODE:
                raise ValueError(f"Node {i} is not a completed cross-asset experiment")
            if set(record.get("results", {})) != set(PAIRS):
                raise ValueError(f"Node {i} lacks both saved market reports")
            nodes.append(dict(record,id=i,dataset=dataset,parent=parent,agent=agent))
    return nodes


def market_metrics(saved, rerun=None):
    """Compare on identical windows; reject a non-reproducing replay."""
    summary = saved["summary"]
    candidate = summary["candidate"]
    if rerun is None:
        # A feature set containing only peer inputs has no local estimator.
        # Its feature-free reference is the historical mean, not a matched ablation.
        reference = summary["mean"]
        reference_type = "historical_mean_only"
    else:
        for key in ("dataset_sha256","other_dataset_sha256","pair","cutoff","windows","protocol"):
            if saved[key] != rerun[key]:
                raise ValueError(f"Replay mismatch: {key}")
        if [(w["start"],w["train_last_label_end"]) for w in saved["details"]] != [
                (w["start"],w["train_last_label_end"]) for w in rerun["details"]]:
            raise ValueError("Replay uses different windows or training labels")
        if candidate != rerun["summary"]["candidate"] or summary["mean"] != rerun["summary"]["mean"]:
            raise ValueError("Candidate or baseline did not reproduce")
        reference = rerun["summary"]["parent"]
        reference_type = "matched_local_ablation"
    fields = ("mean_net","mean_stress","rmse","mean_exposure","fills","active_windows","worst_drawdown")
    def metrics(m): return {k:m[k] for k in fields}
    def delta(m): return {k:candidate[k]-m[k] for k in ("mean_net","mean_stress","rmse","mean_exposure")}
    return {"windows":saved["windows"],"candidate":metrics(candidate),
            "parent":metrics(summary["parent"]),"mean":metrics(summary["mean"]),
            "reference":metrics(reference),"reference_type":reference_type,
            "vs_parent":delta(summary["parent"]),"vs_mean":delta(summary["mean"]),
            "vs_reference":delta(reference)}


def review(nodes, parts, cutoff):
    from . import autonomous_research as old, cross_asset as cross, lab, research_tree as tree
    original = {p:old.fingerprint(parts[p],p) for p in PAIRS}
    aligned = cross.align(parts)
    hashes = {p:old.fingerprint(aligned[p],p) for p in PAIRS}
    dataset = lab.digest([cross.PROTOCOL,cutoff,original,hashes])
    if any(n["dataset"] != dataset for n in nodes):
        raise ValueError("Node and supplied historical dataset/cutoff differ")
    output = []
    for n in nodes:
        spec = tree.Spec(**n["spec"]); parent = tree.Spec(**n["parent_spec"])
        local = tuple(k for k in spec.features if k[0] not in cross.FEATURES)
        peer = tuple(k for k in spec.features if k[0] in cross.FEATURES)
        if not peer: raise ValueError("Expected peer-market features")
        ablated = tree.Spec(local,spec.method,spec.transform,spec.calibration) if local else None
        markets = {}
        for pair in PAIRS:
            other = next(p for p in PAIRS if p != pair)
            saved = n["results"][pair]
            if saved["dataset_sha256"] != hashes[pair] or saved["other_dataset_sha256"] != hashes[other] or saved["cutoff"] != cutoff:
                raise ValueError("Saved market fingerprint differs")
            replay = tree.evaluate(aligned[pair],pair,cutoff,ablated,spec,other_parts=aligned[other]) if ablated else None
            markets[pair] = market_metrics(saved,replay)
        output.append({"id":n["id"],"agent":n["agent"],"parent":n["parent"],
                       "spec":n["spec"],"parent_spec":n["parent_spec"],
                       "changed_dimensions":[k for k in ("features","method","transform","calibration") if getattr(spec,k)!=getattr(parent,k)],
                       "peer_features":peer,"local_ablation":None if ablated is None else tree.asdict(ablated),
                       "markets":markets,
                       "peer_improves_both_stress":None if ablated is None else all(m["vs_reference"]["mean_stress"]>0 for m in markets.values()),
                       "beats_mean_both_stress":all(m["vs_mean"]["mean_stress"]>0 for m in markets.values()),
                       "positive_both_stress":all(m["candidate"]["mean_stress"]>0 and m["candidate"]["fills"]>0 for m in markets.values())})
    return {"protocol":PROTOCOL,"dataset":dataset,"cutoff":cutoff,"nodes":output,"development_only":True,"eligible":False,
            "note":"Replayed candidate and local-only ablation on identical aligned windows. No parameter retuning beyond the original causal fitting and calibration. Differences are descriptive development evidence; exposure and selection can differ."}


def main():
    from . import multiyear
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db",required=True); p.add_argument("--node",type=int,nargs="+",required=True)
    p.add_argument("--btc",required=True); p.add_argument("--eth",required=True); p.add_argument("--cutoff",type=int,required=True)
    x=p.parse_args()
    nodes=load_nodes(x.db,x.node)
    result=review(nodes,{"XBTUSD":multiyear.segments(x.btc,x.cutoff),"ETHUSD":multiyear.segments(x.eth,x.cutoff)},x.cutoff)
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=="__main__": main()

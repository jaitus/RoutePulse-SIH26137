"""QPSO vs classical PSO on the DYNAMIC recovery problem — reviewer P1-02.

The 30-seed benchmark answers a static question: given one instance and one
budget, which arm produces the better plan? That is the standard comparison and
it is worth having, but it is not the problem RoutePulse exists to solve.

RoutePulse solves a *recovery* problem: an incumbent plan exists, commitments
are frozen, an event has changed the cost layer, and a new plan must be found
before a deadline. A swarm update rule could plausibly behave differently there
— the search starts from a good incumbent rather than from scratch, the budget
is tighter, and churn matters.

So this runs the same isolation on the dynamic path. Identical instance,
identical initial incumbent, identical event, identical frozen commitments,
identical matrix, identical wall-clock budget. The only difference between the
two conditions is the line that moves a particle.

This is NOT an attempt to make QPSO win. It is the honest way to answer "does
the quantum-inspired rule contribute anything to the thing we actually built".

Run:  python scripts/dynamic_arm.py --seeds 12    -> out/dynamic_arm.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routepulse.dynamic import Engine                      # noqa: E402
from routepulse.graph import RoadGraph, synthetic_grid     # noqa: E402
from routepulse.model import ObjectiveWeights, random_instance  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The event family, mirroring the scenario suite: each condition is replayed
# against every event type so the answer is not an artefact of one kind of
# disruption.
EVENTS = ("closure", "congestion", "breakdown")


def load_graph():
    for p, label in ((os.path.join(ROOT, "data", "bengaluru_simplified.json"),
                      "OpenStreetMap Bengaluru (simplified)"),
                     (os.path.join(ROOT, "data", "bengaluru_graph.json"),
                      "OpenStreetMap Bengaluru (raw)")):
        if os.path.exists(p):
            g = RoadGraph.from_json(p)
            return g, f"{label}, {len(g.nodes):,} junctions"
    return synthetic_grid(18, 18), "synthetic grid"


def build(g, seed, n, k):
    nodes = [(nd, la, lo) for nd, (la, lo) in g.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = g.nearest_node(lat0, lon0)
    inst = random_instance(depot, nodes, n_customers=n, n_vehicles=k,
                           capacity=110, seed=seed,
                           depot_lat=g.nodes[depot][0], depot_lon=g.nodes[depot][1])
    return Engine(g, inst, ObjectiveWeights(), matrix_buckets=5), inst


def apply_event(eng, inst, kind, seed):
    """Same event, deterministically, for both conditions."""
    c = inst.customers[seed % inst.n]
    if kind == "closure":
        return len(eng.apply_closure(c.lat, c.lon, radius_m=320))
    if kind == "congestion":
        return len(eng.apply_congestion(c.lat, c.lon, multiplier=6.0,
                                        radius_m=420))
    busy = [r.vehicle_id for r in eng.incumbent.routes if r.customer_ids]
    return eng.remove_vehicle(busy[seed % len(busy)]) if busy else 0


def one_run(g, seed, n, k, kind, engine_name, budget):
    """A complete dynamic recovery under one update rule."""
    eng, inst = build(g, seed, n, k)
    # IDENTICAL starting incumbent for both conditions: built with the same
    # improver and seed, so the only difference downstream is the update rule.
    eng.initial_plan(budget=0.8, seed=seed, improver="alns")
    before = eng.incumbent.score
    apply_event(eng, inst, kind, seed)
    res = eng.replan(budget=budget, seed=seed, engines=(engine_name,))
    sol = res.solution
    return {
        "score": sol.score if math.isfinite(sol.score) else None,
        "feasible": bool(sol.feasible),
        "latency_ms": res.total_ms,
        "churn": round(sol.churn, 4),
        "accepted": bool(res.accepted),
        "incumbent_before": round(before, 1),
        "congestion_exposure_s": round(sol.congestion_exposure, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=12)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--budget", type=float, default=0.25)
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "dynamic_arm.json"))
    args = ap.parse_args()

    g, src = load_graph()
    print(f"graph  : {src}")
    print(f"config : {args.n} stops, {args.k} vehicles, {args.budget:.2f}s "
          f"budget, {args.seeds} seeds x {len(EVENTS)} event types")
    print("Identical instance, incumbent, event, commitments, matrix and "
          "budget.\nThe only difference is the particle update rule.\n")

    rows = []
    for kind in EVENTS:
        for seed in range(args.seeds):
            q = one_run(g, seed, args.n, args.k, kind, "qpso", args.budget)
            p = one_run(g, seed, args.n, args.k, kind, "pso", args.budget)
            rows.append({"event": kind, "seed": seed, "qpso": q, "pso": p})
            print(f"  {kind} {seed + 1}/{args.seeds}", end="\r", flush=True)
    print(" " * 50, end="\r")

    def paired(metric, feasible_only=True):
        out = []
        for r in rows:
            a, b = r["qpso"], r["pso"]
            if feasible_only and not (a["feasible"] and b["feasible"]):
                continue
            if a.get(metric) is None or b.get(metric) is None:
                continue
            out.append((a[metric], b[metric]))
        return out

    report = {"graph": src, "config": vars(args), "events": list(EVENTS),
              "rows": rows, "analysis": {}}

    print(f"{'metric':<22}{'QPSO':>12}{'PSO':>12}{'delta':>10}"
          f"{'95% CI':>22}{'p':>10}")
    print("-" * 88)
    for metric, lower_is_better in (("score", True), ("latency_ms", True),
                                    ("churn", True)):
        pr = paired(metric)
        if len(pr) < 5:
            continue
        xs = [a for a, _ in pr]
        ys = [b for _, b in pr]
        diffs = [b - a for a, b in pr]          # PSO minus QPSO
        mean_q, mean_p = st.mean(xs), st.mean(ys)
        delta = (mean_p - mean_q) / mean_p * 100 if abs(mean_p) > 1e-9 else 0.0
        lo = hi = pv = None
        if len(diffs) > 1 and abs(mean_p) > 1e-9:
            se = st.stdev(diffs) / math.sqrt(len(diffs))
            m = st.mean(diffs)
            lo, hi = (m - 1.96 * se) / mean_p * 100, (m + 1.96 * se) / mean_p * 100
        try:
            from scipy.stats import wilcoxon
            if not all(abs(a - b) < 1e-12 for a, b in pr):
                _s, pv = wilcoxon(xs, ys)
        except ImportError:
            pv = None
        ci = "—" if lo is None else f"[{lo:+.2f}%, {hi:+.2f}%]"
        pstr = "—" if pv is None else f"{pv:.4f}"
        print(f"{metric:<22}{mean_q:>12.1f}{mean_p:>12.1f}{delta:>9.2f}%"
              f"{ci:>22}{pstr:>10}")
        report["analysis"][metric] = {
            "n": len(pr), "qpso_mean": round(mean_q, 3),
            "pso_mean": round(mean_p, 3), "delta_pct": round(delta, 3),
            "ci95_low_pct": None if lo is None else round(lo, 3),
            "ci95_high_pct": None if hi is None else round(hi, 3),
            "p": None if pv is None else float(pv),
            "lower_is_better": lower_is_better,
            "significant": bool(pv is not None and pv < 0.05),
        }

    fq = sum(1 for r in rows if r["qpso"]["feasible"])
    fp = sum(1 for r in rows if r["pso"]["feasible"])
    report["analysis"]["feasible_runs"] = {"qpso": fq, "pso": fp,
                                           "total": len(rows)}
    print(f"\nfeasible recoveries: QPSO {fq}/{len(rows)}  PSO {fp}/{len(rows)}")

    sc = report["analysis"].get("score")
    if sc:
        if sc["significant"]:
            verdict = ("the quantum-inspired update rule DOES separate from "
                       "classical PSO on the dynamic recovery problem")
        else:
            verdict = ("the quantum-inspired update rule does NOT separate "
                       "from classical PSO on the dynamic recovery problem "
                       "either -- the same conclusion as the static benchmark")
        print(f"\nverdict: {verdict}")
        report["verdict"] = verdict

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    main()

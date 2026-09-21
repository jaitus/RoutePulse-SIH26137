"""End-to-end latency measurement — event to accepted plan, decomposed.

The claim in the README is that we report end-to-end timing, which a 2025
systematic review found no published study in this field does. That claim needs
real percentiles from the real network, not one lucky run.

Measures the OPERATIONAL path (single engine) and the DEMO path (solver race)
separately, because they are different things and conflating them would inflate
or deflate the number depending on which one flattered us.

Run:  python scripts/latency.py --trials 20 --stops 30
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routepulse.dynamic import Engine                      # noqa: E402
from routepulse.graph import RoadGraph, synthetic_grid     # noqa: E402
from routepulse.model import ObjectiveWeights, random_instance  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[k]


def load_graph():
    for p, label in ((os.path.join(ROOT, "data", "bengaluru_simplified.json"),
                      "OpenStreetMap Bengaluru (simplified)"),
                     (os.path.join(ROOT, "data", "bengaluru_graph.json"),
                      "OpenStreetMap Bengaluru (raw)")):
        if os.path.exists(p):
            g = RoadGraph.from_json(p)
            return g, f"{label}, {len(g.nodes):,} junctions"
    return synthetic_grid(18, 18), "synthetic grid"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--stops", type=int, default=30)
    ap.add_argument("--vehicles", type=int, default=5)
    ap.add_argument("--budget", type=float, default=0.25)
    ap.add_argument("--scale", default="",
                    help="comma-separated stop counts, e.g. 30,60,100 — "
                         "reviewer asks for latency SCALING, not one size")
    args = ap.parse_args()

    g, src = load_graph()
    nodes = [(nd, la, lo) for nd, (la, lo) in g.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = g.nearest_node(lat0, lon0)

    print(f"graph  : {src}")
    print(f"config : {args.stops} stops, {args.vehicles} vehicles, "
          f"{args.budget:.2f}s solver budget, {args.trials} injected incidents\n")

    # The OPERATIONAL path is a single engine: one dispatcher waiting on one
    # solver. Both single-engine arms are reported because ALNS replaced the
    # improvement layer on the strength of the 30-seed adoption gate and the
    # latency consequence of that decision has to be visible, not assumed.
    # The demo race is a separate number and is never averaged in with them.
    modes = {
        "operational (ALNS only)": ("alns",),
        "operational (QPSO only)": ("qpso",),
        "demo (4-engine race)": ("emergency", "qpso", "alns", "ortools"),
    }
    report: dict = {"graph": src, "config": vars(args), "modes": {}}

    for label, engines in modes.items():
        totals: list[float] = []
        stages: dict[str, list[float]] = {}
        for t in range(args.trials):
            inst = random_instance(depot, nodes, n_customers=args.stops,
                                   n_vehicles=args.vehicles, capacity=110, seed=t,
                                   depot_lat=g.nodes[depot][0], depot_lon=g.nodes[depot][1])
            eng = Engine(g, inst, ObjectiveWeights(), matrix_buckets=5)
            eng.initial_plan(budget=0.8, seed=t)
            # inject an incident near a random served stop
            c = inst.customers[t % len(inst.customers)]
            eng.apply_closure(c.lat, c.lon, radius_m=300)
            res = eng.replan(budget=args.budget, seed=t, engines=engines)
            totals.append(res.total_ms)
            for k, v in res.stages_ms.items():
                stages.setdefault(k, []).append(v)
            print(f"  {label}: {t + 1}/{args.trials}", end="\r", flush=True)
        print(" " * 60, end="\r")

        print(f"--- {label} ---")
        print(f"{'stage':<22}{'p50':>9}{'p95':>9}{'max':>9}")
        for k, v in stages.items():
            print(f"{k:<22}{pct(v,.5):>9.1f}{pct(v,.95):>9.1f}{max(v):>9.1f}")
        print(f"{'TOTAL':<22}{pct(totals,.5):>9.1f}{pct(totals,.95):>9.1f}"
              f"{max(totals):>9.1f}")
        target = 500.0
        ok = pct(totals, .95) < target
        print(f"  p95 {'MEETS' if ok else 'DOES NOT MEET'} the {target:.0f} ms "
              f"target  (p95 = {pct(totals,.95):.0f} ms)\n")
        report["modes"][label] = {
            "total_p50": pct(totals, .5), "total_p95": pct(totals, .95),
            "total_max": max(totals), "meets_500ms": ok,
            "stages_p95": {k: pct(v, .95) for k, v in stages.items()},
            "raw_totals": totals,
        }

    # ---- scaling sweep. One instance size is an anecdote about one instance
    # size, and the matrix is O(n^2 x buckets), so this is exactly where the
    # design either holds or stops holding. Reported per size, not averaged.
    if args.scale:
        report["scaling"] = {}
        print("--- operational path (ALNS) vs instance size ---")
        header = f"{'stops':>7}{'p50':>9}{'p95':>9}{'max':>9}"
        print(header + f"{'matrix p95':>12}{'solve p95':>11}{'<500ms':>9}")
        for n in [int(x) for x in args.scale.split(",") if x.strip()]:
            totals, mat, solve = [], [], []
            trials = max(5, args.trials // 2)
            for t in range(trials):
                inst = random_instance(depot, nodes, n_customers=n,
                                       n_vehicles=max(3, n // 6), capacity=110,
                                       seed=t, depot_lat=g.nodes[depot][0],
                                       depot_lon=g.nodes[depot][1])
                eng = Engine(g, inst, ObjectiveWeights(), matrix_buckets=5)
                eng.initial_plan(budget=0.8, seed=t)
                c = inst.customers[t % len(inst.customers)]
                eng.apply_closure(c.lat, c.lon, radius_m=300)
                res = eng.replan(budget=args.budget, seed=t, engines=("alns",))
                totals.append(res.total_ms)
                mat.append(res.stages_ms.get("matrix_rebuild", 0.0))
                solve.append(res.stages_ms.get("solve", 0.0))
            ok = pct(totals, .95) < 500.0
            print(f"{n:>7}{pct(totals,.5):>9.0f}{pct(totals,.95):>9.0f}"
                  f"{max(totals):>9.0f}{pct(mat,.95):>12.0f}"
                  f"{pct(solve,.95):>11.0f}{('yes' if ok else 'NO'):>9}")
            report["scaling"][str(n)] = {
                "stops": n, "vehicles": max(3, n // 6), "trials": trials,
                "total_p50": pct(totals, .5), "total_p95": pct(totals, .95),
                "total_max": max(totals), "matrix_p95": pct(mat, .95),
                "solve_p95": pct(solve, .95), "meets_500ms": ok,
            }
        print()

    out = os.path.join(ROOT, "out", "latency.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    print(f"written: {out}")


if __name__ == "__main__":
    main()

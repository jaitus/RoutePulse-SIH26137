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
    ap.add_argument("--budget", type=float, default=0.35)
    args = ap.parse_args()

    g, src = load_graph()
    nodes = [(nd, la, lo) for nd, (la, lo) in g.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = g.nearest_node(lat0, lon0)

    print(f"graph  : {src}")
    print(f"config : {args.stops} stops, {args.vehicles} vehicles, "
          f"{args.budget:.2f}s solver budget, {args.trials} injected incidents\n")

    modes = {
        "operational (QPSO only)": ("qpso",),
        "demo (3-engine race)": ("emergency", "qpso", "ortools"),
    }
    report: dict = {"graph": src, "config": vars(args), "modes": {}}

    for label, engines in modes.items():
        totals: list[float] = []
        stages: dict[str, list[float]] = {}
        for t in range(args.trials):
            inst = random_instance(depot, nodes, n_customers=args.stops,
                                   n_vehicles=args.vehicles, capacity=110, seed=t)
            eng = Engine(g, inst, ObjectiveWeights(), matrix_buckets=3)
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

    out = os.path.join(ROOT, "out", "latency.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    print(f"written: {out}")


if __name__ == "__main__":
    main()

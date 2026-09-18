"""Deliverable 5 — systematic performance benchmarking + ablation.

Answers the question the blueprint says must be answered before any claim is
made: does the QPSO swarm layer contribute anything, or is the local search
doing all the work?

Arms (identical instance, identical wall-clock budget, identical local search):

  A   QPSO + local search, warm-started      <- the proposed system
  A0  QPSO + local search, cold start        <- isolates warm start
  B   Random-restart keys + local search     <- isolates the SWARM UPDATE RULE
  D   QPSO, local search OFF                 <- isolates the improvement layer
  E   Greedy + local search only             <- does the population layer earn its place?
  OR  OR-Tools (same matrix, same budget)    <- external baseline

Run:  python scripts/bench.py --seeds 10 --budget 0.35
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routepulse.costs import TimeMatrix                       # noqa: E402
from routepulse.graph import RoadGraph, synthetic_grid        # noqa: E402
from routepulse.model import ObjectiveWeights, random_instance  # noqa: E402
from routepulse.solvers.heuristics import greedy_insertion, local_search  # noqa: E402
from routepulse.solvers.qpso import solve_qpso                # noqa: E402
from routepulse.validator import score                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(seed: int, n: int, k: int):
    cache = os.path.join(ROOT, "data", "bengaluru_graph.json")
    if os.path.exists(cache):
        g = RoadGraph.from_json(cache)
        src = "OpenStreetMap (Bengaluru)"
    else:
        g = synthetic_grid(rows=18, cols=18)
        src = "synthetic grid"
    nodes = [(nd, la, lo) for nd, (la, lo) in g.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = g.nearest_node(lat0, lon0)
    inst = random_instance(depot, nodes, n_customers=n, n_vehicles=k,
                           capacity=110, seed=seed)
    tm = TimeMatrix(g, [inst.depot_node] + [c.id for c in inst.customers], buckets=3)
    return g, inst, tm, src


def run_arm(arm: str, inst, tm, w, budget: float, seed: int):
    t0 = time.perf_counter()
    warm = score(inst, greedy_insertion(inst, tm, w, seed), tm, w)

    if arm == "E":          # greedy + local search only, no population layer
        s = local_search(inst, warm, tm, w, time.perf_counter() + budget)
        s = score(inst, s, tm, w)
        tel = {"iterations": 0, "evaluations": 0}
    elif arm == "A":
        s, tel = solve_qpso(inst, tm, w, budget, seed=seed, warm_start=warm)
    elif arm == "A0":
        s, tel = solve_qpso(inst, tm, w, budget, seed=seed, warm_start=None)
    elif arm == "B":
        s, tel = solve_qpso(inst, tm, w, budget, seed=seed, warm_start=warm,
                            beta_hi=0.0, beta_lo=0.0)   # no swarm pull => random restart
    elif arm == "D":
        s, tel = solve_qpso(inst, tm, w, budget, seed=seed, warm_start=warm,
                            use_local_search=False)
    elif arm == "OR":
        from routepulse.solvers.ortools_baseline import solve_ortools
        s = solve_ortools(inst, tm, w, time_budget=budget)
        s = score(inst, s, tm, w) if s else None
        tel = {"iterations": 0, "evaluations": 0}
    elif arm == "GREEDY":
        s, tel = warm, {"iterations": 0, "evaluations": 0}
    else:
        raise ValueError(arm)

    ms = (time.perf_counter() - t0) * 1000
    if s is None:
        return None
    return {"score": s.score, "travel": s.travel_time, "feasible": s.feasible,
            "ms": ms, "iters": tel.get("iterations", 0),
            "evals": tel.get("evaluations", 0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--budget", type=float, default=0.35)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "bench.json"))
    args = ap.parse_args()

    w = ObjectiveWeights()
    arms = ["GREEDY", "E", "A", "A0", "B", "D", "OR"]
    labels = {
        "GREEDY": "Greedy construction only",
        "E":      "Greedy + local search (no swarm)",
        "A":      "QPSO + LS, warm start   [proposed]",
        "A0":     "QPSO + LS, cold start",
        "B":      "Random-restart + LS (no swarm pull)",
        "D":      "QPSO, local search OFF",
        "OR":     "OR-Tools (same matrix/budget)",
    }
    results: dict[str, list[dict]] = {a: [] for a in arms}

    _, _, _, src = build(0, args.n, args.k)
    print(f"graph source : {src}")
    print(f"instance     : n={args.n} customers, k={args.k} vehicles")
    print(f"protocol     : {args.seeds} seeds x {args.budget:.2f}s budget, "
          f"identical instance and matrix per seed\n")

    for seed in range(args.seeds):
        _, inst, tm, _ = build(seed, args.n, args.k)
        for a in arms:
            r = run_arm(a, inst, tm, w, args.budget, seed)
            if r:
                results[a].append(r)
        print(f"  seed {seed + 1}/{args.seeds} done", end="\r", flush=True)
    print(" " * 30, end="\r")

    # ---------------------------------------------------------------- report
    ref = [r["score"] for r in results["E"] if r["feasible"]]
    print(f"{'arm':<38}{'mean':>10}{'sd':>9}{'best':>10}{'feas':>7}{'ms':>8}{'vs E':>9}")
    print("-" * 91)
    summary = {}
    for a in arms:
        rs = [r for r in results[a] if r["feasible"]]
        if not rs:
            print(f"{labels[a]:<38}{'no feasible result':>43}")
            continue
        sc = [r["score"] for r in rs]
        mean, sd = st.mean(sc), (st.stdev(sc) if len(sc) > 1 else 0.0)
        delta = ""
        if ref and a != "E":
            d = (st.mean(ref) - mean) / st.mean(ref) * 100
            delta = f"{d:+.2f}%"
        print(f"{labels[a]:<38}{mean:>10,.0f}{sd:>9,.0f}{min(sc):>10,.0f}"
              f"{len(rs)}/{len(results[a]):>4}{st.mean(r['ms'] for r in rs):>8.0f}{delta:>9}")
        summary[a] = {"label": labels[a], "mean": mean, "sd": sd, "best": min(sc),
                      "feasible": len(rs), "runs": len(results[a]),
                      "mean_ms": st.mean(r["ms"] for r in rs),
                      "mean_iters": st.mean(r["iters"] for r in rs),
                      "mean_evals": st.mean(r["evals"] for r in rs)}

    print("\nATTRIBUTION (lower score is better)")
    def m(a): return summary[a]["mean"] if a in summary else float("nan")
    if "A" in summary and "E" in summary:
        d = (m("E") - m("A")) / m("E") * 100
        print(f"  swarm layer contribution  (E -> A) : {d:+.2f}%")
    if "A" in summary and "B" in summary:
        d = (m("B") - m("A")) / m("B") * 100
        print(f"  swarm UPDATE RULE         (B -> A) : {d:+.2f}%")
    if "A" in summary and "A0" in summary:
        d = (m("A0") - m("A")) / m("A0") * 100
        print(f"  warm start                (A0 -> A): {d:+.2f}%")
    if "A" in summary and "D" in summary:
        d = (m("D") - m("A")) / m("D") * 100
        print(f"  local search              (D -> A) : {d:+.2f}%")
    if "A" in summary and "OR" in summary:
        d = (m("OR") - m("A")) / m("OR") * 100
        print(f"  vs OR-Tools                        : {d:+.2f}%")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"config": vars(args), "graph_source": src,
                   "weights": w.to_dict(), "summary": summary,
                   "raw": results}, f, indent=1)
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    main()

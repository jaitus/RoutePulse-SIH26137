"""Convergence analysis — a named requirement of the sponsor's Expected Solution.

"Convergence analysis" is NOT the same as an anytime curve. An anytime curve
says how good the answer is at time t. Convergence analysis characterises HOW
the algorithm approaches its answer:

  * best and mean-personal-best objective per iteration
  * swarm diversity over time (is it still exploring, or has it collapsed?)
  * the contraction-expansion coefficient beta and its effect
  * the iteration at which improvement falls below a threshold (stagnation)

Writes a PNG plus a JSON of the underlying series, so the numbers behind the
chart are inspectable rather than trapped in a picture.

Run:  python scripts/convergence.py --budget 3.0 --seeds 5
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib                                          # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                            # noqa: E402

from routepulse.costs import TimeMatrix                    # noqa: E402
from routepulse.graph import RoadGraph, synthetic_grid     # noqa: E402
from routepulse.model import ObjectiveWeights, random_instance   # noqa: E402
from routepulse.solvers.heuristics import greedy_insertion  # noqa: E402
from routepulse.solvers.qpso import solve_qpso             # noqa: E402
from routepulse.validator import score                     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_graph() -> tuple[RoadGraph, str]:
    for p, label in ((os.path.join(ROOT, "data", "bengaluru_simplified.json"),
                      "OpenStreetMap Bengaluru (simplified)"),
                     (os.path.join(ROOT, "data", "bengaluru_graph.json"),
                      "OpenStreetMap Bengaluru (raw)")):
        if os.path.exists(p):
            return RoadGraph.from_json(p), label
    return synthetic_grid(18, 18), "synthetic grid"


def stagnation_iter(best: list[float], rel_eps: float = 0.001) -> int | None:
    """First iteration after which no run of 5 iterations improves by > eps."""
    for i in range(len(best) - 5):
        window = best[i:i + 6]
        if window[0] <= 0:
            continue
        if (window[0] - min(window)) / window[0] < rel_eps:
            return i + 1
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=3.0)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--cold", action="store_true",
                    help="cold start (the measured-better default)")
    args = ap.parse_args()

    g, src = load_graph()
    nodes = [(nd, la, lo) for nd, (la, lo) in g.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = g.nearest_node(lat0, lon0)
    w = ObjectiveWeights()

    runs = []
    print(f"graph: {src} ({len(g.nodes):,} nodes)")
    for seed in range(args.seeds):
        inst = random_instance(depot, nodes, n_customers=args.n,
                               n_vehicles=args.k, capacity=110, seed=seed)
        tm = TimeMatrix(g, [inst.depot_node] + [c.id for c in inst.customers],
                        buckets=3)
        warm = None if args.cold else score(
            inst, greedy_insertion(inst, tm, w, seed), tm, w)
        _, tel = solve_qpso(inst, tm, w, time_budget=args.budget, seed=seed,
                            warm_start=warm)
        runs.append(tel["convergence"])
        print(f"  seed {seed}: {tel['iterations']} iters, "
              f"{tel['evaluations']} evals, {len(tel['convergence'])} pts")

    L = min(len(r) for r in runs if r)
    if L < 2:
        print("not enough iterations to analyse -- raise --budget")
        return
    iters = list(range(1, L + 1))
    best = [st.mean(r[i]["best"] for r in runs if r[i]["best"] is not None)
            for i in range(L)]
    meanp = [st.mean(r[i]["mean_pbest"] for r in runs
                     if r[i]["mean_pbest"] is not None) for i in range(L)]
    div = [st.mean(r[i]["diversity"] for r in runs) for i in range(L)]
    beta = [st.mean(r[i]["beta"] for r in runs) for i in range(L)]

    stag = stagnation_iter(best)
    total_gain = (best[0] - best[-1]) / best[0] * 100 if best[0] else 0.0

    # ------------------------------------------------------------------ plot
    fig, ax = plt.subplots(3, 1, figsize=(8, 8.5), sharex=True,
                           gridspec_kw={"height_ratios": [2, 1, 1]})
    fig.suptitle(f"QPSO convergence — {args.seeds} seeds, "
                 f"{args.budget:.1f}s budget, {'cold' if args.cold else 'warm'} start",
                 fontsize=11, y=0.97)

    ax[0].plot(iters, best, lw=2, label="best fitness (gbest)")
    ax[0].plot(iters, meanp, lw=1.2, ls="--", label="mean personal best")
    if stag:
        ax[0].axvline(stag, color="crimson", lw=1, ls=":",
                      label=f"stagnation ≈ iter {stag}")
    ax[0].set_ylabel("objective (lower is better)")
    ax[0].legend(fontsize=8)
    ax[0].grid(alpha=.25)

    ax[1].plot(iters, div, lw=1.6, color="#7a5af8")
    ax[1].set_ylabel("swarm diversity")
    ax[1].grid(alpha=.25)

    ax[2].plot(iters, beta, lw=1.6, color="#d29922")
    ax[2].set_ylabel("β  (contraction–expansion)")
    ax[2].set_xlabel("iteration")
    ax[2].grid(alpha=.25)

    out_png = os.path.join(ROOT, "out", "convergence.png")
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_png, dpi=140)

    out_json = os.path.join(ROOT, "out", "convergence.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"graph": src, "config": vars(args),
                   "iters": iters, "best": best, "mean_pbest": meanp,
                   "diversity": div, "beta": beta,
                   "stagnation_iter": stag,
                   "total_gain_pct": total_gain}, f, indent=1)

    print("\nCONVERGENCE ANALYSIS")
    print(f"  iterations analysed      : {L}")
    print(f"  gbest improvement        : {total_gain:+.2f}%  "
          f"({best[0]:,.0f} -> {best[-1]:,.0f})")
    print(f"  diversity                : {div[0]:.4f} -> {div[-1]:.4f} "
          f"({100*(1-div[-1]/max(1e-9, div[0])):.0f}% collapse)")
    print(f"  beta                     : {beta[0]:.3f} -> {beta[-1]:.3f}")
    print(f"  stagnation onset         : "
          f"{'iteration ' + str(stag) if stag else 'not reached in budget'}")
    print(f"\n  written: {out_png}")
    print(f"           {out_json}")


if __name__ == "__main__":
    main()

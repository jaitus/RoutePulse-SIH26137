"""Deliverable 5 — systematic performance benchmarking + ablation.

Answers the question the blueprint says must be answered before any claim is
made: does the QPSO swarm layer contribute anything, or is the local search
doing all the work?

Arms (identical instance, identical wall-clock budget, identical local search):

  A     QPSO + local search, warm-started    <- the proposed system
  A0    QPSO + local search, cold start      <- isolates warm start
  B     Random-restart keys + local search   <- isolates the SWARM UPDATE RULE
  D     QPSO, local search OFF               <- isolates the improvement layer
  E     Greedy + local search only           <- does the population layer earn its place?
  ALNS  Greedy + Traffic-Aware ALNS          <- Appendix A, behind its adoption gate
  SB    Greedy + LS + Simulated Bifurcation  <- the quantum-derived re-sequencer
  OR    OR-Tools (same matrix, same budget)  <- external baseline

ALNS and SB both replace part of E's improvement layer at an IDENTICAL total
budget, so "is it better?" is asked the only way it can be answered honestly:
same instance, same wall-clock, paired test.

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


_GRAPH_CACHE: dict[str, tuple] = {}


def build(seed: int, n: int, k: int):
    # Must load the SAME graph the server does, or the benchmark describes a
    # system nobody runs. The app prefers the simplified (junction-contracted)
    # graph; so does this.
    if "g" not in _GRAPH_CACHE:
        for p, label in (
            (os.path.join(ROOT, "data", "bengaluru_simplified.json"),
             "OpenStreetMap Bengaluru (simplified)"),
            (os.path.join(ROOT, "data", "bengaluru_graph.json"),
             "OpenStreetMap Bengaluru (raw)"),
        ):
            if os.path.exists(p):
                gg = RoadGraph.from_json(p)
                _GRAPH_CACHE["g"] = (gg, f"{label}, {len(gg.nodes):,} junctions")
                break
        else:
            _GRAPH_CACHE["g"] = (synthetic_grid(rows=18, cols=18), "synthetic grid")
    g, src = _GRAPH_CACHE["g"]
    nodes = [(nd, la, lo) for nd, (la, lo) in g.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = g.nearest_node(lat0, lon0)
    inst = random_instance(depot, nodes, n_customers=n, n_vehicles=k,
                           capacity=110, seed=seed,
                           depot_lat=g.nodes[depot][0], depot_lon=g.nodes[depot][1])
    tm = TimeMatrix(g, [inst.depot_node] + [c.id for c in inst.customers],
                    buckets=5)
    tm.base = TimeMatrix(g, tm.nodes, buckets=5, use_overlays=False)
    return g, inst, tm, src


def run_arm(arm: str, inst, tm, w, budget: float, seed: int):
    t0 = time.perf_counter()
    warm = score(inst, greedy_insertion(inst, tm, w, seed), tm, w)

    if arm == "E":          # greedy + local search only, no population layer
        s = local_search(inst, warm, tm, w, time.perf_counter() + budget)
        s = score(inst, s, tm, w)
        tel = {"iterations": 0, "evaluations": 0}
    elif arm == "ALNS":     # greedy + Traffic-Aware ALNS, same total budget as E
        from routepulse.solvers.alns import alns_with_telemetry
        s, tel = alns_with_telemetry(inst, warm, tm, w,
                                     time.perf_counter() + budget, seed=seed)
        s = score(inst, s, tm, w)
    elif arm == "SEQ_LS":   # re-sequencing head-to-head, classical side
        # 2-opt ONLY: no relocate, no swap. Routes are fixed; the only question
        # is the order within them -- the exact question SB's single-tour Ising
        # embedding can answer. Any other comparison would be rigged.
        s = local_search(inst, warm, tm, w, time.perf_counter() + budget,
                         intra_only=True)
        s = score(inst, s, tm, w)
        tel = {"iterations": 0, "evaluations": 0}
    elif arm == "SEQ_SB":   # re-sequencing head-to-head, quantum-derived side
        from routepulse.solvers.sb import sb_resequence
        s, tel = sb_resequence(inst, warm, tm, w, time.perf_counter() + budget,
                               seed=seed)
        s = score(inst, s, tm, w)
        tel = {"iterations": tel.get("routes_tried", 0),
               "evaluations": tel.get("routes_improved", 0), **tel}
    elif arm == "SB":       # greedy + LS, then Simulated Bifurcation re-sequencing
        # 75% of the budget on 2-opt, 25% on SB. The split is arbitrary but it
        # is the SAME total budget as E, which is what makes the pair testable.
        from routepulse.solvers.sb import sb_resequence
        s = local_search(inst, warm, tm, w, time.perf_counter() + budget * 0.75)
        s = score(inst, s, tm, w)
        s2, tel = sb_resequence(inst, s, tm, w,
                                time.perf_counter() + budget * 0.25, seed=seed)
        s2 = score(inst, s2, tm, w)
        if s2.score < s.score and s2.feasible:
            s = s2
        tel = {"iterations": tel.get("routes_tried", 0),
               "evaluations": tel.get("routes_improved", 0), **tel}
    elif arm == "PSO":      # CLASSICAL swarm control (reviewer P1-09)
        # Identical encoding, decoder, improvement layer, restart logic and
        # budget. The only difference from arm A is the line that moves a
        # particle. That is what isolates the *quantum-inspired* update rule
        # from "having a swarm at all", which is what arm B measures.
        s, tel = solve_qpso(inst, tm, w, budget, seed=seed, warm_start=warm,
                            update="pso")
    elif arm == "A_HYB":    # QPSO -> ALNS chained (reviewer P1-02)
        # The architecture the PPT must NOT claim without this number: run the
        # quantum-inspired stage, then hand its output to ALNS, splitting one
        # budget between them. If this does not beat both parents, the honest
        # description is "two separate engines", not "a hybrid chain".
        from routepulse.solvers.alns import alns_with_telemetry
        s, tel = solve_qpso(inst, tm, w, budget * 0.5, seed=seed, warm_start=warm)
        s = score(inst, s, tm, w)
        s, tel2 = alns_with_telemetry(inst, s, tm, w,
                                      time.perf_counter() + budget * 0.5, seed=seed)
        s = score(inst, s, tm, w)
        tel = {**tel, "alns_iterations": tel2.get("iterations", 0)}
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
    arms = ["GREEDY", "E", "ALNS", "A_HYB", "SB", "SEQ_LS", "SEQ_SB",
            "A", "A0", "PSO", "B", "D", "OR"]
    labels = {
        "GREEDY": "Greedy construction only",
        "E":      "Greedy + local search (no swarm)",
        "ALNS":   "Greedy + Traffic-Aware ALNS",
        "SB":     "Greedy + LS + Simulated Bifurcation",
        "SEQ_LS": "  re-sequencing only: 2-opt",
        "SEQ_SB": "  re-sequencing only: Simulated Bifurcation",
        "A_HYB":  "QPSO -> ALNS chained hybrid",
        "A":      "QPSO + LS, warm start   [proposed]",
        "A0":     "QPSO + LS, cold start",
        "PSO":    "Classical PSO + LS (same decoder)",
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

    # ------------------------------------------------- significance testing
    # The README claims we run hypothesis tests. That claim has to be true.
    # Wilcoxon signed-rank on PAIRED per-seed scores (same instance, same
    # matrix, same budget), which is the correct test for this design -- the
    # arms are not independent samples.
    print("\nSIGNIFICANCE — Wilcoxon signed-rank vs 'Greedy + local search'")
    tests = {}
    try:
        from scipy.stats import wilcoxon
        ref_runs = results["E"]
        for a in arms:
            if a == "E":
                continue
            pairs = [(r1["score"], r2["score"])
                     for r1, r2 in zip(ref_runs, results[a])
                     if r1["feasible"] and r2["feasible"]]
            if len(pairs) < 5:
                print(f"  {labels[a]:<38} n={len(pairs)} — too few pairs to test")
                continue
            x = [p[0] for p in pairs]
            y = [p[1] for p in pairs]
            if all(abs(xi - yi) < 1e-9 for xi, yi in zip(x, y)):
                print(f"  {labels[a]:<38} identical to reference")
                continue
            stat, p = wilcoxon(x, y)
            verdict = "significant" if p < 0.05 else "NOT significant"
            print(f"  {labels[a]:<38} p={p:.4f}  n={len(pairs)}  {verdict}")
            tests[a] = {"p": float(p), "stat": float(stat), "n": len(pairs)}
    except ImportError:
        print("  scipy not installed — cannot run the test, so we do not claim one")

    print("\n  NOTE: with 6 seeds the minimum attainable two-sided p is ~0.031,")
    print("  so anything here is indicative only. 30 seeds is the protocol the")
    print("  literature review recommends; run --seeds 30 before quoting these.")

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
    if "A" in summary and "PSO" in summary:
        d = (m("PSO") - m("A")) / m("PSO") * 100
        p = tests.get("PSO", {}).get("p")
        print(f"  QUANTUM vs CLASSICAL swarm(PSO->A) : {d:+.2f}%"
              + ("" if p is None else f"   (arm p={p:.4f} vs E)"))
    if "A" in summary and "OR" in summary:
        d = (m("OR") - m("A")) / m("OR") * 100
        print(f"  vs OR-Tools                        : {d:+.2f}%")

    if all(k in summary for k in ("A_HYB", "A", "ALNS")):
        print("\nIS THE QPSO -> ALNS HYBRID A REAL THING? (reviewer P1-02)")
        vs_a = (m("A") - m("A_HYB")) / m("A") * 100
        vs_al = (m("ALNS") - m("A_HYB")) / m("ALNS") * 100
        print(f"  hybrid vs QPSO+LS alone            : {vs_a:+.2f}%")
        print(f"  hybrid vs ALNS alone               : {vs_al:+.2f}%")
        verdict = ("the chain beats both parents -- 'hybrid' is a fair label"
                   if vs_a > 0 and vs_al > 0
                   else "the chain does NOT beat both parents -- describe them "
                        "as separate engines, not a hybrid")
        print(f"  verdict: {verdict}")

    # ---- the comparisons the claims are actually made from.
    # Testing every arm against ONE reference answers "does this arm beat
    # greedy+LS", which is not the question for most of them. The quantum
    # update rule has to be tested against the CLASSICAL swarm, and the hybrid
    # against its own parents, or the headline sentence has no statistic
    # behind it.
    print("\nKEY PAIRWISE COMPARISONS (paired Wilcoxon on the same instances)")
    try:
        from scipy.stats import wilcoxon as _wx2

        def compare(a, b, label):
            if a not in results or b not in results:
                return
            pr = [(x["score"], y["score"]) for x, y in zip(results[a], results[b])
                  if x["feasible"] and y["feasible"]]
            if len(pr) < 5:
                print(f"  {label:<40} n={len(pr)} — too few pairs")
                return
            xs = [u for u, _ in pr]
            ys = [v for _, v in pr]
            if all(abs(u - v) < 1e-9 for u, v in pr):
                print(f"  {label:<40} identical")
                return
            _s, pv = _wx2(xs, ys)
            d = (st.mean(ys) - st.mean(xs)) / st.mean(ys) * 100
            verdict = "significant" if pv < 0.05 else "NOT significant"
            # 95% CONFIDENCE INTERVAL on the mean PAIRED difference. A p-value
            # says whether an effect is distinguishable from zero; an interval
            # says how big it might plausibly be. For a result like "+0.1%,
            # p = 0.95" the interval is the more useful statement, because it
            # bounds the effect rather than merely failing to detect it.
            diffs = [v - u for u, v in pr]          # reference minus arm
            lo = hi = None
            if len(diffs) > 1:
                sd = st.stdev(diffs)
                se = sd / math.sqrt(len(diffs))
                half = 1.96 * se
                m = st.mean(diffs)
                base = st.mean(ys)
                lo, hi = (m - half) / base * 100, (m + half) / base * 100
            ci = "" if lo is None else f"  95% CI [{lo:+.2f}%, {hi:+.2f}%]"
            print(f"  {label:<40} {d:+6.2f}%{ci}  p={pv:.4f}  n={len(pr)}"
                  f"  {verdict}")
            tests[f"{a}_vs_{b}"] = {"p": float(pv), "n": len(pr),
                                    "delta_pct": round(d, 3),
                                    "ci95_low_pct": None if lo is None else round(lo, 3),
                                    "ci95_high_pct": None if hi is None else round(hi, 3)}

        compare("A", "PSO", "QPSO vs CLASSICAL PSO (same decoder)")
        compare("A", "E", "QPSO+LS vs greedy+LS")
        compare("ALNS", "E", "ALNS vs greedy+LS")
        compare("A_HYB", "ALNS", "QPSO->ALNS hybrid vs ALNS alone")
        compare("A_HYB", "A", "QPSO->ALNS hybrid vs QPSO+LS alone")
        compare("ALNS", "OR", "ALNS vs OR-Tools")
    except ImportError:
        print("  scipy not installed — no test, so no claim")

    print("\nADOPTION GATES (same start, same total budget, vs 'Greedy + LS')")
    for arm, name in (("ALNS", "Traffic-Aware ALNS"),
                      ("SB", "Simulated Bifurcation")):
        if arm not in summary or "E" not in summary:
            continue
        d = (m("E") - m(arm)) / m("E") * 100
        p = tests.get(arm, {}).get("p")
        if p is None:
            verdict = "no test"
        elif p < 0.05 and d > 0:
            verdict = "ADOPT — better and significant"
        elif p < 0.05:
            verdict = "REJECT — significantly WORSE"
        else:
            verdict = "DO NOT ADOPT — inside the noise"
        ptxt = "p=n/a" if p is None else f"p={p:.4f}"
        print(f"  {name:<24} {d:+6.2f}%  {ptxt:<10} {verdict}")

    if "SEQ_LS" in summary and "SEQ_SB" in summary:
        d = (m("SEQ_LS") - m("SEQ_SB")) / m("SEQ_LS") * 100
        print("\nRE-SEQUENCER HEAD-TO-HEAD (identical routes, identical budget)")
        print(f"  Simulated Bifurcation vs 2-opt : {d:+.2f}%  "
              f"(positive = the Ising machine wins)")
        try:
            from scipy.stats import wilcoxon as _wx
            pairs = [(a["score"], b["score"]) for a, b in
                     zip(results["SEQ_LS"], results["SEQ_SB"])
                     if a["feasible"] and b["feasible"]]
            if len(pairs) >= 5 and not all(abs(x - y) < 1e-9 for x, y in pairs):
                _s, pv = _wx([x for x, _ in pairs], [y for _, y in pairs])
                print(f"  paired Wilcoxon                : p={pv:.4f}  n={len(pairs)}"
                      f"  {'significant' if pv < 0.05 else 'NOT significant'}")
                tests["SEQ_SB_vs_SEQ_LS"] = {"p": float(pv), "n": len(pairs)}
        except ImportError:
            pass

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    # The run manifest: exactly which instances and seeds produced these
    # numbers, and the direction of the objective. "30 seeds" is not a
    # reproducible statement; this is.
    manifest = {
        "seeds": list(range(args.seeds)),
        "instance_family": f"random_instance(n={args.n}, k={args.k}, "
                           f"capacity=110, zone_radius_m=1400)",
        "instances": [f"demo-n{args.n}-k{args.k}-s{sd}" for sd in range(args.seeds)],
        "budget_s": args.budget,
        "matrix_buckets": 5,
        "paired": True,
        "reference_arm": "E",
        "objective_direction": "lower is better",
        "paired_rows": [k for k in tests if "_vs_" in k],
        "reference_only_rows": [k for k in tests if "_vs_" not in k],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"config": vars(args), "graph_source": src,
                   "manifest": manifest,
                   "weights": w.to_dict(), "summary": summary, "wilcoxon": tests,
                   "raw": results}, f, indent=1)
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    main()

"""Simulated Bifurcation — validation, calibration and scaling.

Three questions, in the order they have to be asked:

  1. IS THE SOLVER CORRECT?  Does the bifurcation dynamics actually find Ising
     ground states? Checked against exhaustive enumeration on random instances
     small enough to brute-force. If this fails, nothing else means anything.

  2. IS THE EMBEDDING CORRECT?  Does the QUBO reduction of a tour produce valid
     permutations, and does the penalty weight A control that the way the
     theory says? Swept, because "we used a penalty" is not a result and the
     failure mode -- spin configurations that are not tours -- is the standard
     objection to QUBO routing.

  3. IS IT WORTH ANYTHING HERE?  Against 2-opt, on the same routes from the
     real Bengaluru network, with both the quality gap and the time cost
     reported, and against the exact optimum wherever a route is small enough
     to enumerate.

Run:  python scripts/sb_eval.py            -> out/sb.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                             # noqa: E402

from routepulse.energy import EnergyMeter                       # noqa: E402
from routepulse.model import ObjectiveWeights                   # noqa: E402
from routepulse.solvers.heuristics import (greedy_insertion,    # noqa: E402
                                           local_search, _route_penalty)
from routepulse.solvers.sb import (decode_permutation,          # noqa: E402
                                   ising_energy, simulated_bifurcation,
                                   tsp_ising)
from routepulse.validator import score                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------- 1. solver correctness

def validate_ground_states(trials: int = 20, sizes=(10, 12, 14)) -> list[dict]:
    """Brute-force check. 2^14 = 16,384 configurations is enumerable; beyond
    that it is not, which is exactly why heuristics exist."""
    out = []
    for N in sizes:
        rng = np.random.default_rng(N)
        hits, gaps = 0, []
        t0 = time.perf_counter()
        for t in range(trials):
            J = rng.normal(0, 1, (N, N))
            J = (J + J.T) / 2
            np.fill_diagonal(J, 0.0)
            h = rng.normal(0, 1, N)
            best = min(itertools.product([-1.0, 1.0], repeat=N),
                       key=lambda s: ising_energy(J, h, s))
            e_opt = ising_energy(J, h, best)
            _s, e_sb, _info = simulated_bifurcation(J, h, steps=400, agents=24,
                                                    seed=t)
            hits += abs(e_sb - e_opt) < 1e-6
            gaps.append(abs(e_sb - e_opt) / max(1e-9, abs(e_opt)) * 100)
        out.append({
            "spins": N, "trials": trials,
            "ground_states_found": hits,
            "rate": round(hits / trials, 3),
            "mean_gap_pct": round(st.mean(gaps), 4),
            "worst_gap_pct": round(max(gaps), 4),
            "ms_per_instance": round((time.perf_counter() - t0) / trials * 1000, 1),
        })
        print(f"  Ising N={N:<3} ground state found {hits}/{trials}  "
              f"mean gap {st.mean(gaps):.3f}%")
    return out


# ---------------------------------------------------- 2. penalty calibration

def calibrate_penalty(trials: int = 20, m: int = 6) -> list[dict]:
    """How the constraint penalty A controls validity AND quality.

    A is expressed as a multiple of the largest leg cost. Too small and the
    dynamics buy objective at the cost of the constraints, producing spin
    patterns that are not tours. Too large and the objective is drowned: every
    tour looks the same to the solver and it returns an arbitrary valid one.
    """
    rng = np.random.default_rng(1)
    cases = []
    for _t in range(trials):
        pts = rng.uniform(0, 10, (m, 2))
        o = rng.uniform(0, 10, 2)
        d = rng.uniform(0, 10, 2)
        D = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=-1)
        np.fill_diagonal(D, 0.0)
        orow = np.linalg.norm(pts - o, axis=1)
        dcol = np.linalg.norm(pts - d, axis=1)

        def length(p, D=D, orow=orow, dcol=dcol):
            return (orow[p[0]] + sum(D[p[i], p[i + 1]] for i in range(len(p) - 1))
                    + dcol[p[-1]])

        best = min(itertools.permutations(range(m)), key=length)
        cases.append((D, orow, dcol, length, length(best)))

    out = []
    # penalty_scale s puts A at 2*s*max_leg; report the multiple, not the knob
    for ps in (0.25, 0.5, 1.0, 2.0):
        valid, gaps = 0, []
        for t, (D, orow, dcol, length, opt) in enumerate(cases):
            J, h, _off = tsp_ising(D, orow, dcol, penalty_scale=ps)
            s, _e, _i = simulated_bifurcation(J, h, steps=500, agents=32, seed=t)
            order, raw_valid = decode_permutation(s, m)
            valid += raw_valid
            gaps.append((length(order) - opt) / opt * 100)
        out.append({
            "A_over_max_leg": 2 * ps,
            "raw_valid_decodes": valid, "trials": trials,
            "valid_rate": round(valid / trials, 3),
            "mean_gap_vs_optimal_pct": round(st.mean(gaps), 3),
        })
        print(f"  A = {2*ps:>4.1f}x max leg   valid {valid}/{trials}   "
              f"mean gap {st.mean(gaps):+.2f}%")
    return out


# ------------------------------------------------------- 3. head-to-head value

def resequence_study(seeds: int = 8, n: int = 30, k: int = 5,
                     exact_limit: int = 8) -> dict:
    """SB vs 2-opt vs the exact optimum, on routes from the real network.

    Both operators are given the SAME routes (a greedy construction) and asked
    only to reorder them, which is the only comparison the single-tour Ising
    embedding can fairly enter.

    Every route is scored under TWO objectives, and the difference between them
    is the whole point:

      TRAVEL ONLY   sum of leg times. This is what the Ising model actually
                    encodes, so it is the objective SB is entitled to be judged
                    on.
      FULL          travel + 8 x lateness, which is the objective the fleet is
                    actually judged on, and which the quadratic form cannot
                    express because lateness depends on cumulative arrival time.

    An engine that wins the first and loses the second has not failed at
    optimisation. It has been handed an incomplete problem.
    """
    from bench import build                                    # noqa: E402
    from routepulse.solvers.heuristics import _route_cost
    from routepulse.solvers.sb import position_field

    w = ObjectiveWeights()
    rows: list[dict] = []
    for seed in range(seeds):
        _g, inst, tm, _src = build(seed, n, k)
        base = score(inst, greedy_insertion(inst, tm, w, seed), tm, w)
        veh = {v.id: v for v in inst.vehicles}
        for r in base.routes:
            seq = list(r.customer_ids)
            m = len(seq)
            if m < 3:
                continue
            v = veh[r.vehicle_id]
            c0 = _route_penalty(inst, tm, v, seq, w)
            if not math.isfinite(c0):
                continue

            # --- 2-opt, run to convergence on this one route
            t0 = time.perf_counter()
            cur, improved = list(seq), True
            c_ls = c0
            while improved:
                improved = False
                for i in range(len(cur) - 1):
                    for j in range(i + 1, len(cur)):
                        trial = cur[:i] + cur[i:j + 1][::-1] + cur[j + 1:]
                        c = _route_penalty(inst, tm, v, trial, w)
                        if c < c_ls - 1e-9:
                            cur, c_ls, improved = trial, c, True
            ls_ms = (time.perf_counter() - t0) * 1000

            # --- shared Ising inputs
            t_ref = max(inst.horizon_start, v.available_at)
            D = [[0.0 if a == b else tm.tt(seq[a], seq[b], t_ref)
                  for b in range(m)] for a in range(m)]
            orow = [tm.tt(v.start_node, c, t_ref) for c in seq]
            dcol = [tm.tt(c, inst.depot_node, t_ref) for c in seq]
            if any(math.isinf(x) for x in orow + dcol):
                continue
            legs = [D[a][b] for a in range(m) for b in range(m)
                    if a != b and math.isfinite(D[a][b])]
            mean_leg = sum(legs) / len(legs) if legs else 0.0

            sb_out = {}
            for tag, P in (("sb", None),
                           ("sb_tw", position_field(inst, seq, w, t_ref, mean_leg))):
                t0 = time.perf_counter()
                J, h, _off = tsp_ising(D, orow, dcol, position_cost=P)
                s, _e, info = simulated_bifurcation(J, h, steps=800, agents=48,
                                                    seed=seed)
                order, raw_valid = decode_permutation(s, m)
                sb_seq = [seq[i] for i in order]
                sb_out[tag] = {
                    "seq": sb_seq, "valid": raw_valid, "spins": info["N"],
                    "ms": (time.perf_counter() - t0) * 1000,
                }

            # --- exact optimum under BOTH objectives
            opt_full = opt_travel = None
            if m <= exact_limit:
                perms = list(itertools.permutations(seq))
                opt_full = min(_route_penalty(inst, tm, v, list(p), w) for p in perms)
                opt_travel = min(_route_cost(inst, tm, v, list(p)) for p in perms)

            rows.append({
                "seed": seed, "stops": m, "spins": sb_out["sb"]["spins"],
                "greedy": round(c0, 2),
                "two_opt": round(c_ls, 2), "two_opt_ms": round(ls_ms, 2),
                "two_opt_travel": round(_route_cost(inst, tm, v, cur), 2),
                "sb": round(_route_penalty(inst, tm, v, sb_out["sb"]["seq"], w), 2),
                "sb_travel": round(_route_cost(inst, tm, v, sb_out["sb"]["seq"]), 2),
                "sb_ms": round(sb_out["sb"]["ms"], 2),
                "sb_raw_valid": sb_out["sb"]["valid"],
                "sb_tw": round(_route_penalty(inst, tm, v,
                                              sb_out["sb_tw"]["seq"], w), 2),
                "sb_tw_raw_valid": sb_out["sb_tw"]["valid"],
                "optimal": None if opt_full is None else round(opt_full, 2),
                "optimal_travel": None if opt_travel is None else round(opt_travel, 2),
            })

    def gap(a, b):
        return (a - b) / b * 100 if b else 0.0

    exact = [r for r in rows if r["optimal"] is not None]
    summary = {
        "routes": len(rows),
        "mean_stops": round(st.mean(r["stops"] for r in rows), 2) if rows else 0,
        "mean_spins": round(st.mean(r["spins"] for r in rows), 1) if rows else 0,
        "raw_valid_rate": round(st.mean(1.0 if r["sb_raw_valid"] else 0.0
                                        for r in rows), 3) if rows else None,
        "raw_valid_rate_tw_field": round(st.mean(1.0 if r["sb_tw_raw_valid"] else 0.0
                                                 for r in rows), 3) if rows else None,
        "sb_beats_2opt": sum(1 for r in rows if r["sb"] < r["two_opt"] - 1e-9),
        "2opt_beats_sb": sum(1 for r in rows if r["two_opt"] < r["sb"] - 1e-9),
        "tie": sum(1 for r in rows if abs(r["sb"] - r["two_opt"]) <= 1e-9),
        "sb_tw_beats_sb": sum(1 for r in rows if r["sb_tw"] < r["sb"] - 1e-9),
        "mean_ms_2opt": round(st.mean(r["two_opt_ms"] for r in rows), 2) if rows else 0,
        "mean_ms_sb": round(st.mean(r["sb_ms"] for r in rows), 2) if rows else 0,
    }
    if exact:
        summary["exact_subset"] = {
            "routes": len(exact),
            "max_stops": max(r["stops"] for r in exact),
            # FULL objective (travel + lateness) — what the fleet is judged on
            "two_opt_gap_pct": round(st.mean(gap(r["two_opt"], r["optimal"])
                                             for r in exact), 3),
            "sb_gap_pct": round(st.mean(gap(r["sb"], r["optimal"])
                                        for r in exact), 3),
            "sb_tw_gap_pct": round(st.mean(gap(r["sb_tw"], r["optimal"])
                                           for r in exact), 3),
            # TRAVEL ONLY — the objective the Ising model actually encodes
            "two_opt_travel_gap_pct": round(
                st.mean(gap(r["two_opt_travel"], r["optimal_travel"])
                        for r in exact), 3),
            "sb_travel_gap_pct": round(
                st.mean(gap(r["sb_travel"], r["optimal_travel"])
                        for r in exact), 3),
            "two_opt_optimal": sum(1 for r in exact
                                   if abs(r["two_opt"] - r["optimal"]) < 1e-6),
            "sb_optimal": sum(1 for r in exact
                              if abs(r["sb"] - r["optimal"]) < 1e-6),
            "sb_travel_optimal": sum(1 for r in exact
                                     if abs(r["sb_travel"] - r["optimal_travel"]) < 1e-6),
        }
    return {"rows": rows, "summary": summary}


def edd_sweep(seeds: int = 8, n: int = 30, k: int = 5,
              weights=(0.0, 0.1, 0.2, 0.5, 1.0, 2.0)) -> list[dict]:
    """How much of the time-window damage the local field can undo.

    Reported against the GREEDY order SB was handed, so 0% means "SB's tour is
    as good as the one it replaced" and positive means worse.
    """
    from bench import build                                    # noqa: E402
    from routepulse.solvers.sb import position_field

    w = ObjectiveWeights()
    cases = []
    for seed in range(seeds):
        _g, inst, tm, _src = build(seed, n, k)
        base = score(inst, greedy_insertion(inst, tm, w, seed), tm, w)
        veh = {v.id: v for v in inst.vehicles}
        for r in base.routes:
            seq = list(r.customer_ids)
            m = len(seq)
            if m < 3:
                continue
            v = veh[r.vehicle_id]
            t = max(inst.horizon_start, v.available_at)
            D = [[0.0 if a == b else tm.tt(seq[a], seq[b], t) for b in range(m)]
                 for a in range(m)]
            orow = [tm.tt(v.start_node, c, t) for c in seq]
            dcol = [tm.tt(c, inst.depot_node, t) for c in seq]
            if any(math.isinf(x) for x in orow + dcol):
                continue
            legs = [D[a][b] for a in range(m) for b in range(m) if a != b]
            c0 = _route_penalty(inst, tm, v, seq, w)
            if not math.isfinite(c0) or c0 <= 0:
                continue
            cases.append((inst, tm, v, seq, m, D, orow, dcol,
                          sum(legs) / len(legs), t, c0))

    out = []
    for ew in weights:
        deltas, valid = [], 0
        for (inst, tm, v, seq, m, D, orow, dcol, mean_leg, t, c0) in cases:
            P = position_field(inst, seq, w, t, mean_leg, edd_weight=ew)
            J, h, _off = tsp_ising(D, orow, dcol, position_cost=P)
            s, _e, _i = simulated_bifurcation(J, h, steps=800, agents=48, seed=0)
            order, raw_valid = decode_permutation(s, m)
            valid += raw_valid
            c = _route_penalty(inst, tm, v, [seq[i] for i in order], w)
            deltas.append((c - c0) / c0 * 100)
        out.append({"edd_weight": ew, "routes": len(cases),
                    "mean_pct_vs_greedy": round(st.mean(deltas), 2),
                    "raw_valid": valid})
        print(f"  edd_weight {ew:<5} {st.mean(deltas):+9.2f}% vs the greedy order"
              f"   valid {valid}/{len(cases)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "sb.json"))
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    EnergyMeter.probe()

    print("1. SOLVER CORRECTNESS — bifurcation dynamics vs exhaustive search")
    ground = validate_ground_states(trials=args.trials)

    print("\n2. EMBEDDING CALIBRATION — constraint penalty A, 6-stop tours")
    penalty = calibrate_penalty(trials=args.trials)

    print("\n3. VALUE — re-sequencing real routes: SB vs 2-opt vs exact")
    with EnergyMeter("sb_resequence_study") as meter:
        study = resequence_study(seeds=args.seeds)
    s = study["summary"]
    print(f"  {s['routes']} routes, mean {s['mean_stops']} stops "
          f"({s['mean_spins']} spins)")
    print(f"  raw-valid decode rate           : {s['raw_valid_rate']} "
          f"(with time-window field: {s['raw_valid_rate_tw_field']})")
    print(f"  SB better / 2-opt better / tie  : "
          f"{s['sb_beats_2opt']} / {s['2opt_beats_sb']} / {s['tie']}")
    print(f"  mean time  2-opt / SB           : "
          f"{s['mean_ms_2opt']} ms / {s['mean_ms_sb']} ms")
    if "exact_subset" in s:
        e = s["exact_subset"]
        print(f"\n  vs EXACT optimum, {e['routes']} routes of <= {e['max_stops']} stops")
        print(f"    TRAVEL ONLY  (the objective the Ising model encodes)")
        print(f"      2-opt {e['two_opt_travel_gap_pct']:+7.3f}%     "
              f"SB {e['sb_travel_gap_pct']:+7.3f}%     "
              f"SB reached it {e['sb_travel_optimal']}/{e['routes']}")
        print(f"    FULL         (travel + 8x lateness — what the fleet pays)")
        print(f"      2-opt {e['two_opt_gap_pct']:+7.3f}%     "
              f"SB {e['sb_gap_pct']:+7.3f}%     "
              f"SB + time-window field {e['sb_tw_gap_pct']:+7.3f}%")
        print(f"    reached the full optimum      : "
              f"2-opt {e['two_opt_optimal']}/{e['routes']}   "
              f"SB {e['sb_optimal']}/{e['routes']}")
        print(f"    time-window field helped on   : "
              f"{s['sb_tw_beats_sb']}/{s['routes']} routes")

    print("\n4. TIME WINDOWS — what the local field can and cannot recover")
    sweep = edd_sweep(seeds=args.seeds)

    payload = {
        "ground_state_validation": ground,
        "penalty_calibration": penalty,
        "resequencing": study,
        "time_window_field_sweep": sweep,
        "energy": meter.reading.to_dict() if meter.reading else None,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    main()

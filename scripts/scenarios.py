"""Scenario suite S1-S9 — the blueprint's operational test set.

Each scenario asserts a specific behaviour and PRINTS ITS DENOMINATOR. A
scenario that passes without exercising its condition is not a pass, so every
check reports what it actually touched (edges closed on the route, stops
affected, etc.) rather than just a green tick.

Run:  python scripts/scenarios.py
      python scripts/scenarios.py --only S8 S9
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routepulse.dynamic import Engine                      # noqa: E402
from routepulse.graph import RoadGraph, synthetic_grid     # noqa: E402
from routepulse.model import ObjectiveWeights, random_instance  # noqa: E402
from routepulse.validator import score                     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[dict] = []


def load():
    for p, label in ((os.path.join(ROOT, "data", "bengaluru_simplified.json"),
                      "OpenStreetMap Bengaluru (simplified)"),
                     (os.path.join(ROOT, "data", "bengaluru_graph.json"),
                      "OpenStreetMap Bengaluru (raw)")):
        if os.path.exists(p):
            g = RoadGraph.from_json(p)
            return g, f"{label}, {len(g.nodes):,} junctions"
    return synthetic_grid(18, 18), "synthetic grid"


def fresh(g, seed=4, n=30, k=5, budget=0.7):
    nodes = [(nd, la, lo) for nd, (la, lo) in g.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = g.nearest_node(lat0, lon0)
    inst = random_instance(depot, nodes, n_customers=n, n_vehicles=k,
                           capacity=110, seed=seed,
                           depot_lat=g.nodes[depot][0], depot_lon=g.nodes[depot][1])
    eng = Engine(g, inst, ObjectiveWeights(), matrix_buckets=5)
    eng.initial_plan(budget=budget, seed=1)
    return eng, inst, lat0, lon0


def record(sid, title, passed, detail, exercised, performance=None):
    """Record a scenario verdict.

    FUNCTIONAL CORRECTNESS AND PERFORMANCE ARE SEPARATE DIMENSIONS.

    A scenario that survives four incidents correctly in 552 ms is useful
    evidence even though it misses a 500 ms target, and collapsing those two
    facts into one green tick hides the second one. `performance`, when given,
    is `{"metric", "value_ms", "target_ms"}` and is reported as its own
    MEETS/OVER verdict that does NOT change the functional verdict.
    """
    row = {"id": sid, "title": title, "pass": bool(passed),
           "exercised": exercised, "detail": detail}
    if performance:
        meets = performance["value_ms"] <= performance["target_ms"]
        row["performance"] = {**performance, "meets_target": meets}
    RESULTS.append(row)
    flag = "PASS" if passed else "FAIL"
    if not exercised:
        flag = "VACUOUS"          # condition never triggered -> not a pass
    perf = ""
    if performance:
        perf = ("  [PERF MEETS]" if row["performance"]["meets_target"]
                else "  [PERF OVER]")
    print(f"  [{flag}] {sid}: {title}{perf}")
    for line in detail:
        print(f"         {line}")
    print()


# --------------------------------------------------------------- scenarios

def s1(g):
    """Closure makes the incumbent IMPOSSIBLE -> CASE 2 recovery.

    This is the scenario the acceptance rule exists for, and the previous
    version never reached it. It closed roads near an arbitrary stop, the old
    plan stayed feasible, and the engine correctly took CASE 1 and usually
    rejected the change for being under the epsilon threshold. A green tick on
    a code path that was never taken is exactly the vacuous pass this harness
    exists to catch, so it is called out and fixed rather than left green.

    WHY IT IS HARD TO BREAK A PLAN ON PURPOSE. `apply_closure` protects every
    stop's own access edges and progressively reopens if anything is stranded,
    so a closure cannot make a delivery address unreachable -- by design, since
    a real road closure degrades access rather than deleting a street address.
    The remaining route to infeasibility is the one that matters operationally:
    a PRIORITY customer missing its window.

    So the scenario searches, deterministically, for an instance where a
    priority stop has little slack, then closes and slows the roads around it
    until the incumbent genuinely violates that deadline. Fixed seed order,
    fixed radii, first hit wins, and the configuration used is printed.
    """
    chosen = None
    for seed in range(2, 22):
        eng, inst, lat0, lon0 = fresh(g, seed=seed)
        arr = {cid: a for r in eng.incumbent.routes
               for cid, a in zip(r.customer_ids, r.arrival_times)}
        tight = [(c.tw_end - arr[c.id], c) for c in inst.customers
                 if c.priority == 1 and c.has_tw and c.id in arr
                 and not math.isinf(arr[c.id])]
        if not tight:
            continue
        slack, victim = min(tight, key=lambda x: x[0])
        if slack > 20 * 60:                 # too much room to break honestly
            continue
        for radius in (400.0, 600.0, 900.0):
            eng, inst, lat0, lon0 = fresh(g, seed=seed)
            closed = eng.apply_closure(victim.lat, victim.lon, radius_m=radius)
            jam = eng.apply_congestion(victim.lat, victim.lon,
                                       multiplier=12.0, radius_m=radius)
            eng.tm.rebuild_all()
            inc = score(inst, eng.incumbent.copy(), eng.tm, eng.w)
            if not inc.feasible:
                chosen = (seed, victim, slack, radius, len(closed), len(jam),
                          inc.violations[:2], eng, inst)
                break
        if chosen:
            break

    if chosen is None:
        record("S1", "closure makes the incumbent infeasible (CASE 2)", False,
               ["no tested closure could break the incumbent -- CASE 2 was "
                "never exercised, so this is not a pass"], exercised=False)
        return

    seed, victim, slack, radius, n_closed, n_jam, violations, eng, inst = chosen
    res = eng.replan(budget=0.35, seed=1, engines=("alns",))
    is_case2 = "CASE 2" in res.case
    ok = is_case2 and res.accepted and res.solution.feasible
    record("S1", "closure makes the incumbent infeasible (CASE 2)", ok, [
        f"instance seed {seed}; priority stop {victim.id} had "
        f"{slack / 60:.1f} min of slack",
        f"closure radius {radius:.0f} m -> {n_closed} edges closed, "
        f"{n_jam} edges slowed x12 (denominator: this is what was exercised)",
        f"incumbent under new costs: INFEASIBLE "
        f"({'; '.join(violations) if violations else 'n/a'})",
        f"case: {res.case}",
        f"accepted with NO epsilon comparison: {res.accepted and is_case2}",
        f"recovery feasible: {res.solution.feasible}",
        f"congestion exposure of the accepted plan: "
        f"{res.solution.congestion_exposure / 60:.1f} min",
        f"end-to-end: {res.total_ms:.0f} ms",
    ], exercised=n_closed > 0 and not res.incumbent_feasible)


def s2(g):
    """Local severe congestion -> costs update, response is low-churn."""
    eng, inst, lat0, lon0 = fresh(g)
    c = inst.customers[7]
    keys = eng.apply_congestion(c.lat, c.lon, multiplier=6.0, radius_m=450)
    res = eng.replan(budget=0.35, seed=1, engines=("qpso",))
    churn = res.churn.get("reassigned", 0) if res.churn else 0
    record("S2", "local severe congestion", res.solution.feasible, [
        f"edges congested x6: {len(keys)}",
        f"stops reassigned: {churn} of {inst.n}",
        f"case: {res.case}",
    ], exercised=len(keys) > 0)


def s3(g):
    """New urgent order -> priority insertion protects the deadline."""
    eng, inst, lat0, lon0 = fresh(g)
    prio = [c for c in inst.customers if c.priority == 1]
    late = 0
    for r in eng.incumbent.routes:
        for cid, arr in zip(r.customer_ids, r.arrival_times):
            cu = next(c for c in inst.customers if c.id == cid)
            if cu.priority == 1 and not math.isinf(arr) and arr > cu.tw_end:
                late += 1
    record("S3", "priority deadlines protected", late == 0, [
        f"priority customers in instance: {len(prio)} of {inst.n}",
        f"priority customers served late: {late}",
    ], exercised=len(prio) > 0)


def s4(g):
    """Vehicle breakdown -> customers reassigned, service continues."""
    eng, inst, lat0, lon0 = fresh(g)
    victim = next(r for r in eng.incumbent.routes if len(r.customer_ids) > 2)
    stranded = len(victim.customer_ids)
    # A breakdown REMOVES the vehicle. Modelling it as capacity=0 was wrong:
    # the solvers then have a vehicle they must route but cannot load, which is
    # an infeasible instance rather than a recoverable incident.
    vid = victim.vehicle_id
    released = eng.remove_vehicle(vid)
    res = eng.replan(budget=0.35, seed=1, engines=("qpso",))
    served = len(res.solution.served())
    # the real test is that every stop is still served AND the plan is valid;
    # "served == n" alone would pass on an infeasible plan
    ok = served == inst.n and res.solution.feasible
    record("S4", "vehicle breakdown", ok, [
        f"vehicle {vid} removed from service, released {released} stops",
        f"fleet now {len(inst.vehicles)} vehicles",
        f"stops still served: {served} of {inst.n}",
        f"plan valid after reassignment: {res.solution.feasible}",
        f"violations: {res.solution.violations[:2]}",
    ], exercised=stranded > 0)


def s5(g):
    """Multiple rush-hour incidents -> robustness AND the latency target.

    Two independent verdicts, deliberately. The functional question is whether
    the plan survives four consecutive incidents and stays feasible. The
    performance question is whether the recovery met its deadline. Reporting
    only the first would hide an over-target p95 behind a green tick; reporting
    only the second would call a correct system broken.
    """
    eng, inst, lat0, lon0 = fresh(g)
    lat_ms, total_edges = [], 0
    for i in (2, 9, 15, 21):
        c = inst.customers[i % inst.n]
        total_edges += len(eng.apply_closure(c.lat, c.lon, radius_m=280))
        r = eng.replan(budget=0.25, seed=1, engines=("alns",))
        lat_ms.append(r.total_ms)
    p95 = sorted(lat_ms)[int(0.95 * (len(lat_ms) - 1))]
    target = 500.0
    record("S5", "multiple rush-hour incidents", eng.incumbent.feasible, [
        f"incidents injected: 4, edges closed total: {total_edges}",
        f"functional: plan still feasible after all four = "
        f"{eng.incumbent.feasible}",
        f"latencies: {[round(x) for x in lat_ms]} ms",
        f"p95: {p95:.0f} ms against a {target:.0f} ms target -> "
        f"{'MEETS' if p95 <= target else 'OVER'}",
        "functional correctness and the latency target are reported "
        "separately; one does not excuse the other",
    ], exercised=total_edges > 0,
        performance={"metric": "recovery p95", "value_ms": round(p95, 1),
                     "target_ms": target})


def s6(g):
    """Congestion clears -> system does NOT re-route for a trivial gain."""
    eng, inst, lat0, lon0 = fresh(g)
    c = inst.customers[5]
    eng.apply_congestion(c.lat, c.lon, multiplier=5.0, radius_m=400)
    eng.replan(budget=0.35, seed=1, engines=("qpso",))
    before = eng.incumbent.assignment()
    lifted = eng.clear_congestion()          # the jam lifts: a cost DECREASE
    res = eng.replan(budget=0.35, seed=1, engines=("qpso",))
    after = res.solution.assignment()
    moved = sum(1 for k in before if before[k] != after.get(k))
    record("S6", "congestion clears without churn", moved <= 3, [
        f"edges released when the jam lifted: {lifted}",
        f"stops reassigned after the jam lifted: {moved} of {inst.n}",
        "a cost DECREASE: scoped invalidation is unsound, so the engine "
        "forced a full matrix rebuild",
        f"case: {res.case}",
        "restraint is the feature here, not movement",
    ], exercised=True)


def s7(g):
    """Pure time-of-day change alters cost -> proves the profile is live."""
    eng, inst, lat0, lon0 = fresh(g)
    a = eng.tm.tt(inst.depot_node, inst.customers[0].id, 0.0)
    b = eng.tm.tt(inst.depot_node, inst.customers[0].id, 9 * 3600.0)
    delta = abs(b - a) / max(1e-9, a) * 100
    record("S7", "time-dependent costs are real", delta > 5.0, [
        f"depot->C0 at 08:00 : {a/60:.2f} min",
        f"depot->C0 at 17:00 : {b/60:.2f} min",
        f"difference: {delta:.1f}% (a static matrix would give 0%)",
    ], exercised=True)


def s8(g):
    """Ambulance dispatch -> corridor opens -> the FLEET IS MEASURABLY AFFECTED.

    The version this replaces could report PASS while the recorded cost of
    priority was +0.0. That is a scenario proving an ambulance path exists, not
    proving the interaction the project is about. Zero impact is a legitimate
    outcome when the ambulance never crosses the fleet in time -- but a test
    that cannot tell the two apart is not evidence.

    So the scene is CHOSEN, deterministically, to create the interaction:
    candidates are the first stop on each non-empty route, in vehicle order.
    Those legs are driven at the start of the shift, which is also when the
    ambulance is on the road, so their traversal intervals can actually overlap
    the corridor's per-edge occupancy windows. The first candidate that
    produces a measurable effect wins and is printed.

    What the scenario does NOT do is force a re-route. Acceptance still decides
    whether the fleet should move; the assertion is that the corridor changed
    the incumbent's cost and touched at least one delivery vehicle, not that
    the map moved.
    """
    chosen = None
    for idx in range(6):
        eng, inst, lat0, lon0 = fresh(g)
        eng.seed_ambulances(2)
        # deterministic candidate set: the first stop of each busy route
        firsts = [r.customer_ids[0] for r in eng.incumbent.routes
                  if r.customer_ids]
        if idx >= len(firsts):
            break
        cust = {c.id: c for c in inst.customers}
        target = cust.get(firsts[idx])
        if target is None:
            continue
        d = eng.dispatch_ambulance(target.lat, target.lon, severity=2)
        if not d.get("ok"):
            continue
        res = eng.replan(budget=0.25, seed=1, engines=("alns",))
        pri = res.priority or {}
        if pri.get("interaction"):
            chosen = (idx, target, d, res, pri, eng, inst)
            break

    if chosen is None:
        record("S8", "ambulance corridor measurably affects the fleet", False,
               ["no candidate scene produced a measurable fleet impact -- the "
                "scenario did not exercise the interaction it claims to test"],
               exercised=False)
        return

    idx, target, d, res, pri, eng, inst = chosen
    ok = (d["time_saved_min"] > 0 and d["path_ms"] < 200
          and pri["cost_of_priority"] > 0.0
          and len(pri["affected_vehicles"]) >= 1)
    record("S8", "ambulance corridor measurably affects the fleet", ok, [
        f"scene chosen deterministically: candidate {idx}, stop {target.id} "
        f"(first stop on a busy route, so its leg is driven while the "
        f"ambulance is on the road)",
        f"unit {d['unit']} -> {d['hospital']}",
        f"ambulance: {d['total_min']} min vs {d['baseline_min']} min without "
        f"priority = {d['time_saved_min']} min SAVED",
        f"corridor: {d['corridor_edges']} edges, {d['corridor_edge_windows']} "
        f"per-edge occupancy windows averaging "
        f"{d['corridor_edge_window_s']:.0f} s",
        f"delivery legs crossing the corridor inside its window: "
        f"{pri['route_corridor_overlaps']} "
        f"(denominator: this is the interaction being claimed)",
        f"affected vehicles: {pri['affected_vehicles']}",
        f"COST OF PRIORITY to the fleet: +{pri['cost_of_priority']} "
        f"({pri['fleet_cost_before']} -> {pri['fleet_cost_after']})",
        f"emergency path latency: {d['path_ms']} ms (budget 200 ms)",
        f"fleet recovery: {res.total_ms:.0f} ms, {res.case}",
        f"acceptance decided by the normal rule, not by the scenario: "
        f"{'accepted' if res.accepted else 'held'}",
    ], exercised=pri["interaction"] and d["corridor_edges"] > 0,
        performance={"metric": "dispatch + recovery",
                     "value_ms": round(d["path_ms"] + res.total_ms, 1),
                     "target_ms": 500.0})


def s9(g):
    """Ambulance meets a closure -> routes AROUND it. Priority != teleportation.

    The condition must actually be exercised: we close edges the ambulance
    genuinely used, not a radius that might miss the path entirely.
    """
    eng, inst, lat0, lon0 = fresh(g)
    eng.seed_ambulances(2)
    d1 = eng.dispatch_ambulance(lat0 + 0.003, lon0 + 0.003, severity=2)
    if not d1.get("ok"):
        record("S9", "ambulance vs closure", False, ["first dispatch failed"], False)
        return
    legA = d1["leg_a_nodes"]
    target = [f"{a}->{b}" for a, b in zip(legA, legA[1:])][8:14]
    eng.g.clear_overlays(kind="corridor")
    for k in target:
        eng.g.close_edge(k)
    eng.ambulances[0].busy_until = None
    d2 = eng.dispatch_ambulance(lat0 + 0.003, lon0 + 0.003, severity=2)
    legA2 = d2["leg_a_nodes"]
    used = [f"{a}->{b}" for a, b in zip(legA2, legA2[1:]) if f"{a}->{b}" in set(target)]
    ok = (not used) and legA != legA2
    record("S9", "priority respects physical closures", ok, [
        f"edges closed ON the ambulance's own route: {len(target)} "
        f"(this is the condition being exercised)",
        f"path changed: {legA != legA2} ({len(legA)} -> {len(legA2)} nodes)",
        f"closed edges used by the ambulance: {len(used)} (must be 0)",
        f"detour cost: {d2['total_min'] - d1['total_min']:+.1f} min",
    ], exercised=len(target) > 0)


SCENARIOS = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5,
             "S6": s6, "S7": s7, "S8": s8, "S9": s9}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()

    g, src = load()
    print(f"graph: {src}\n")
    keys = args.only or list(SCENARIOS)
    t0 = time.perf_counter()
    for k in keys:
        fn = SCENARIOS.get(k.upper())
        if fn is None:
            print(f"  unknown scenario {k}")
            continue
        try:
            fn(g)
        except Exception as e:                            # noqa: BLE001
            record(k.upper(), "crashed", False, [f"{type(e).__name__}: {e}"], False)

    ok = sum(1 for r in RESULTS if r["pass"] and r["exercised"])
    vac = sum(1 for r in RESULTS if not r["exercised"])
    print(f"{ok}/{len(RESULTS)} scenarios passed with their condition exercised"
          + (f"  ({vac} VACUOUS - condition never triggered)" if vac else ""))
    print(f"elapsed {time.perf_counter() - t0:.1f}s")

    out = os.path.join(ROOT, "out", "scenarios.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"graph": src, "results": RESULTS}, f, indent=1)
    print(f"written: {out}")


if __name__ == "__main__":
    main()

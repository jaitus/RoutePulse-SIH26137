"""Independent route validator + the ONE official scoring function.

Two rules this file exists to enforce:

1. FEASIBILITY IS A HARD GATE. No finite penalty weight decides whether a plan
   is acceptable -- this validator does. A solver may optimise whatever internal
   objective it likes; nothing is accepted or ranked until it passes here.

2. ONE SCORER. Every engine's output is re-scored by `score()` so the comparison
   is apples to apples. Solvers never report their own numbers.
"""
from __future__ import annotations

import math

from .costs import TimeMatrix
from .model import Instance, ObjectiveWeights, Solution


def simulate(inst: Instance, sol: Solution, tm: TimeMatrix) -> None:
    """Replay every route through the TIME-DEPENDENT matrix, filling in
    arrival times, load, travel time and completion time."""
    cust = {c.id: c for c in inst.customers}
    veh = {v.id: v for v in inst.vehicles}

    for r in sol.routes:
        v = veh[r.vehicle_id]
        t = max(inst.horizon_start, v.available_at)
        node = v.start_node
        load = 0.0
        travel = 0.0
        r.arrival_times = []
        for cid in r.customer_ids:
            c = cust[cid]
            leg = tm.tt(node, c.id, t)
            if math.isinf(leg):
                travel = math.inf
                r.arrival_times.append(math.inf)
                break
            travel += leg
            t += leg
            if t < c.tw_start:             # wait for the window to open
                t = c.tw_start
            r.arrival_times.append(t)
            t += c.service_time
            load += c.demand
            node = c.id
        # return to depot
        if not math.isinf(travel):
            back = tm.tt(node, inst.depot_node, t)
            if math.isinf(back):
                travel = math.inf
            else:
                travel += back
                t += back
        r.load = load
        r.travel_time = travel
        r.completion_time = t


def validate(inst: Instance, sol: Solution) -> tuple[bool, list[str]]:
    """Hard feasibility gate. Returns (feasible, violations)."""
    v: list[str] = []
    cust = {c.id: c for c in inst.customers}
    veh = {x.id: x for x in inst.vehicles}

    served = [cid for r in sol.routes for cid in r.customer_ids]
    # flow conservation: every customer exactly once
    if len(served) != len(set(served)):
        dupes = {c for c in served if served.count(c) > 1}
        v.append(f"customer served more than once: {sorted(dupes)[:5]}")
    missing = set(cust) - set(served)
    if missing:
        v.append(f"{len(missing)} customer(s) unserved")
    unknown = set(served) - set(cust)
    if unknown:
        v.append(f"unknown customer id(s): {sorted(unknown)[:5]}")

    for r in sol.routes:
        if r.vehicle_id not in veh:
            v.append(f"unknown vehicle {r.vehicle_id}")
            continue
        cap = veh[r.vehicle_id].capacity
        if r.load > cap + 1e-6:
            v.append(f"vehicle {r.vehicle_id} over capacity ({r.load:.1f} > {cap:.1f})")
        if math.isinf(r.travel_time):
            v.append(f"vehicle {r.vehicle_id} has an unreachable leg (road closed?)")
        for cid, arr in zip(r.customer_ids, r.arrival_times):
            c = cust.get(cid)
            if c is None or math.isinf(arr):
                continue
            if arr > c.tw_end + 1e-6 and c.priority == 1:
                v.append(f"priority customer {cid} late "
                         f"({(arr - c.tw_end) / 60:.1f} min past window)")

        # committed leg must be honoured
        cm = veh[r.vehicle_id].committed_customer
        if cm is not None and (not r.customer_ids or r.customer_ids[0] != cm):
            v.append(f"vehicle {r.vehicle_id} broke its committed leg to {cm}")

    return (len(v) == 0), v


def score(inst: Instance, sol: Solution, tm: TimeMatrix, w: ObjectiveWeights,
          previous: Solution | None = None) -> Solution:
    """THE official evaluation function. Every engine's plan goes through this."""
    simulate(inst, sol, tm)
    cust = {c.id: c for c in inst.customers}

    travel = sum(r.travel_time for r in sol.routes)
    if math.isinf(travel):
        sol.travel_time = math.inf
        sol.score = math.inf
        sol.feasible = False
        sol.violations = ["unreachable leg"]
        return sol

    lateness = 0.0
    for r in sol.routes:
        for cid, arr in zip(r.customer_ids, r.arrival_times):
            c = cust[cid]
            if arr > c.tw_end:
                late = arr - c.tw_end
                lateness += late * (3.0 if c.priority == 1 else 1.0)

    congestion = 0.0
    for r in sol.routes:
        congestion += max(0.0, r.travel_time)
    congestion *= 0.0   # placeholder term; corridor exposure enters via tt()

    churn = churn_value(sol, previous) if previous is not None else 0.0

    sol.travel_time = travel
    sol.lateness = lateness
    sol.congestion_exposure = congestion
    sol.churn = churn
    sol.score = (w.alpha_travel * travel
                 + w.beta_lateness * lateness
                 + w.gamma_congestion * congestion
                 + w.delta_churn * churn)
    sol.feasible, sol.violations = validate(inst, sol)
    return sol


def churn_value(sol: Solution, previous: Solution) -> float:
    """Explicit churn formula (the review asked for one).

        churn = w1*|reassigned| + w2*|resequenced| + w3*|vehicles touched|
                normalised by the number of pending customers

    Road-level path changes that alter neither assignment nor sequence are NOT
    churn -- the driver has not been re-tasked.
    """
    w1, w2, w3 = 1.0, 0.5, 0.5
    a_old, a_new = previous.assignment(), sol.assignment()
    p_old, p_new = previous.sequence_pred(), sol.sequence_pred()
    common = set(a_old) & set(a_new)
    if not common:
        return 0.0
    reassigned = sum(1 for c in common if a_old[c] != a_new[c])
    resequenced = sum(1 for c in common if p_old.get(c) != p_new.get(c))
    touched = len({a_new[c] for c in common if a_old[c] != a_new[c]})
    return (w1 * reassigned + w2 * resequenced + w3 * touched) / max(1, len(common))


def churn_components(sol: Solution, previous: Solution) -> dict:
    a_old, a_new = previous.assignment(), sol.assignment()
    p_old, p_new = previous.sequence_pred(), sol.sequence_pred()
    common = set(a_old) & set(a_new)
    reassigned = sum(1 for c in common if a_old[c] != a_new[c])
    resequenced = sum(1 for c in common if p_old.get(c) != p_new.get(c))
    touched = sorted({a_new[c] for c in common if a_old[c] != a_new[c]})
    return {"reassigned": reassigned, "resequenced": resequenced,
            "vehicles_changed": touched, "pending": len(common)}

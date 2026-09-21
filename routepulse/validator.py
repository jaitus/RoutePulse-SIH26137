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


def congestion_exposure(inst: Instance, sol: Solution, tm: TimeMatrix) -> float:
    """Seconds this plan spends inside degraded traffic. MEASURED, not assumed.

    The objective has always carried a gamma-weighted congestion term, and it
    was multiplied by zero — a placeholder that made the term decorative and
    the formulation untrue. The problem was that "exposure" needs a definition
    you can compute and reproduce, not an adjective.

    The definition used here:

        exposure(plan) = SUM over legs of  max(0, tt_live - tt_baseline)

    where `tt_live` is the travel time under every active overlay (incidents,
    corridor) and `tt_baseline` is the same leg at the same departure time
    under the time-of-day profile ALONE. The difference is exactly the extra
    seconds the plan is predicted to spend because of events, which is the
    thing the term was always supposed to price.

    Two properties make it usable as an objective component:

      * it is ZERO on an undisturbed network, so it cannot quietly inflate
        every score and make benchmark arms incomparable;
      * it is strictly additive over legs and needs no second Dijkstra — the
        baseline matrix is built once at engine start and never rebuilt,
        because the base profile does not change.

    Note this is DIFFERENT from lateness. A route can be badly congested and
    still hit every window; the fleet still paid for the congestion in fuel,
    driver hours and risk, and a plan that avoids a jam should score better
    than one that sits in it even when both arrive on time.
    """
    base = getattr(tm, "base", None)
    if base is None:
        return 0.0
    cust = {c.id: c for c in inst.customers}
    veh = {v.id: v for v in inst.vehicles}
    total = 0.0
    for r in sol.routes:
        v = veh.get(r.vehicle_id)
        if v is None or not r.customer_ids:
            continue
        t = max(inst.horizon_start, v.available_at)
        node = v.start_node
        for cid in r.customer_ids:
            c = cust.get(cid)
            if c is None:
                continue
            live = tm.tt(node, cid, t)
            if math.isinf(live):
                return total
            try:
                flat = base.tt(node, cid, t)
            except KeyError:
                flat = live
            if not math.isinf(flat):
                total += max(0.0, live - flat)
            t += live
            if t < c.tw_start:
                t = c.tw_start
            t += c.service_time
            node = cid
        back_live = tm.tt(node, inst.depot_node, t)
        if not math.isinf(back_live):
            try:
                back_flat = base.tt(node, inst.depot_node, t)
            except KeyError:
                back_flat = back_live
            if not math.isinf(back_flat):
                total += max(0.0, back_live - back_flat)
    return total


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

    congestion = congestion_exposure(inst, sol, tm)

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

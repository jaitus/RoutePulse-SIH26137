"""Construction + local search.

`greedy_insertion` doubles as the EMERGENCY MODE solver: when an incident lands
and the deadline is brutal, we need something feasible immediately, not
something good eventually.
"""
from __future__ import annotations

import math
import random
import time

from ..costs import TimeMatrix
from ..model import Instance, ObjectiveWeights, Route, Solution


def _route_cost(inst: Instance, tm: TimeMatrix, vehicle, seq: list[int]) -> float:
    """Travel time of one route through the time-dependent matrix."""
    cust = {c.id: c for c in inst.customers}
    t = max(inst.horizon_start, vehicle.available_at)
    node = vehicle.start_node
    total = 0.0
    for cid in seq:
        c = cust[cid]
        leg = tm.tt(node, cid, t)
        if math.isinf(leg):
            return math.inf
        total += leg
        t += leg
        if t < c.tw_start:
            t = c.tw_start
        t += c.service_time
        node = cid
    back = tm.tt(node, inst.depot_node, t)
    return math.inf if math.isinf(back) else total + back


def _route_penalty(inst: Instance, tm: TimeMatrix, vehicle, seq: list[int],
                   w: ObjectiveWeights) -> float:
    """Search-time cost: travel + lateness + a large (but soft) capacity term.

    The big term guides SEARCH only. Acceptance is decided by the validator.

    MEMOISED. The Split DP and the local search re-evaluate the same route
    segments thousands of times per second; without this the swarm never
    completes a generation inside the budget. The cache lives on the
    TimeMatrix so it dies exactly when the costs it depends on are rebuilt.
    """
    cache = getattr(tm, "_pen_cache", None)
    if cache is None:
        cache = tm._pen_cache = {}          # type: ignore[attr-defined]
    key = (vehicle.id, vehicle.available_at, tuple(seq))
    hit = cache.get(key)
    if hit is not None:
        return hit
    val = _route_penalty_uncached(inst, tm, vehicle, seq, w)
    if len(cache) < 400_000:
        cache[key] = val
    return val


def _route_penalty_uncached(inst: Instance, tm: TimeMatrix, vehicle,
                            seq: list[int], w: ObjectiveWeights) -> float:
    cust = {c.id: c for c in inst.customers}
    t = max(inst.horizon_start, vehicle.available_at)
    node = vehicle.start_node
    travel = 0.0
    late = 0.0
    load = 0.0
    for cid in seq:
        c = cust[cid]
        leg = tm.tt(node, cid, t)
        if math.isinf(leg):
            return math.inf
        travel += leg
        t += leg
        if t < c.tw_start:
            t = c.tw_start
        if t > c.tw_end:
            late += (t - c.tw_end) * (3.0 if c.priority == 1 else 1.0)
        t += c.service_time
        load += c.demand
        node = cid
    back = tm.tt(node, inst.depot_node, t)
    if math.isinf(back):
        return math.inf
    travel += back
    over = max(0.0, load - vehicle.capacity)
    return (w.alpha_travel * travel + w.beta_lateness * late
            + w.lambda_search_penalty * over)


def greedy_insertion(inst: Instance, tm: TimeMatrix, w: ObjectiveWeights,
                     seed: int = 0) -> Solution:
    """Cheapest-insertion construction honouring committed legs."""
    rng = random.Random(seed)
    cust = {c.id: c for c in inst.customers}
    routes: dict[int, list[int]] = {v.id: [] for v in inst.vehicles}
    veh = {v.id: v for v in inst.vehicles}
    loads = {v.id: 0.0 for v in inst.vehicles}

    # committed legs are frozen at position 0 and never moved
    pending = set(cust)
    for v in inst.vehicles:
        if v.committed_customer is not None and v.committed_customer in pending:
            routes[v.id].append(v.committed_customer)
            loads[v.id] += cust[v.committed_customer].demand
            pending.discard(v.committed_customer)

    # priority + tight windows first, then the rest
    order = sorted(pending, key=lambda c: (-cust[c].priority, cust[c].tw_end,
                                           rng.random()))
    for cid in order:
        c = cust[cid]
        best = (math.inf, None, None)
        for vid, seq in routes.items():
            if loads[vid] + c.demand > veh[vid].capacity + 1e-9:
                continue
            lo = 1 if veh[vid].committed_customer is not None else 0
            base = _route_penalty(inst, tm, veh[vid], seq, w)
            if math.isinf(base):
                base = 0.0
            for pos in range(lo, len(seq) + 1):
                trial = seq[:pos] + [cid] + seq[pos:]
                cost = _route_penalty(inst, tm, veh[vid], trial, w)
                delta = cost - base
                if delta < best[0]:
                    best = (delta, vid, pos)
        if best[1] is None:                     # no capacity anywhere
            vid = min(loads, key=lambda k: loads[k])
            routes[vid].append(cid)
            loads[vid] += c.demand
        else:
            _, vid, pos = best
            routes[vid].insert(pos, cid)
            loads[vid] += c.demand

    return Solution(routes=[Route(vehicle_id=vid, customer_ids=seq)
                            for vid, seq in routes.items()])


# --------------------------------------------------------------- local search

def local_search(inst: Instance, sol: Solution, tm: TimeMatrix,
                 w: ObjectiveWeights, deadline: float,
                 max_passes: int = 6) -> Solution:
    """2-opt within routes + relocate and swap between routes.

    This is the IMPROVEMENT LAYER. Appendix A of the blueprint proposes
    replacing it with Traffic-Aware ALNS; that swap happens here and nowhere
    else, which is why it is isolated behind one function.
    """
    veh = {v.id: v for v in inst.vehicles}
    cust = {c.id: c for c in inst.customers}
    seqs = {r.vehicle_id: list(r.customer_ids) for r in sol.routes}
    costs = {vid: _route_penalty(inst, tm, veh[vid], s, w) for vid, s in seqs.items()}

    def frozen(vid: int) -> int:
        return 1 if veh[vid].committed_customer is not None else 0

    improved = True
    passes = 0
    while improved and passes < max_passes and time.perf_counter() < deadline:
        improved = False
        passes += 1

        # ---- 2-opt within each route
        for vid, seq in seqs.items():
            lo = frozen(vid)
            n = len(seq)
            for i in range(lo, n - 1):
                if time.perf_counter() > deadline:
                    break
                for j in range(i + 1, n):
                    trial = seq[:i] + seq[i:j + 1][::-1] + seq[j + 1:]
                    c = _route_penalty(inst, tm, veh[vid], trial, w)
                    if c < costs[vid] - 1e-9:
                        seqs[vid] = trial
                        seq = trial
                        costs[vid] = c
                        improved = True

        # ---- relocate one customer to another route
        vids = list(seqs)
        for a in vids:
            if time.perf_counter() > deadline:
                break
            for idx in range(frozen(a), len(seqs[a])):
                cid = seqs[a][idx]
                rem = seqs[a][:idx] + seqs[a][idx + 1:]
                ca = _route_penalty(inst, tm, veh[a], rem, w)
                for b in vids:
                    if b == a:
                        continue
                    load_b = sum(cust[x].demand for x in seqs[b])
                    if load_b + cust[cid].demand > veh[b].capacity + 1e-9:
                        continue
                    for pos in range(frozen(b), len(seqs[b]) + 1):
                        trial = seqs[b][:pos] + [cid] + seqs[b][pos:]
                        cb = _route_penalty(inst, tm, veh[b], trial, w)
                        if ca + cb < costs[a] + costs[b] - 1e-9:
                            seqs[a], seqs[b] = rem, trial
                            costs[a], costs[b] = ca, cb
                            improved = True
                            break
                    else:
                        continue
                    break
                else:
                    continue
                break

        # ---- swap two customers between routes
        for a in vids:
            if time.perf_counter() > deadline:
                break
            for b in vids:
                if b <= a:
                    continue
                for i in range(frozen(a), len(seqs[a])):
                    for j in range(frozen(b), len(seqs[b])):
                        x, y = seqs[a][i], seqs[b][j]
                        sa = seqs[a][:i] + [y] + seqs[a][i + 1:]
                        sb = seqs[b][:j] + [x] + seqs[b][j + 1:]
                        if (sum(cust[t].demand for t in sa) > veh[a].capacity + 1e-9
                                or sum(cust[t].demand for t in sb) > veh[b].capacity + 1e-9):
                            continue
                        ca = _route_penalty(inst, tm, veh[a], sa, w)
                        cb = _route_penalty(inst, tm, veh[b], sb, w)
                        if ca + cb < costs[a] + costs[b] - 1e-9:
                            seqs[a], seqs[b] = sa, sb
                            costs[a], costs[b] = ca, cb
                            improved = True

    return Solution(routes=[Route(vehicle_id=vid, customer_ids=s)
                            for vid, s in seqs.items()])

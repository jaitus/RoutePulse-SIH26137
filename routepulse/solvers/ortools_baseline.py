"""OR-Tools comparator.

Fairness note that belongs in the report, not buried here:

OR-Tools' routing library consumes a STATIC integer distance callback. It has
no native notion of a departure-time-dependent arc cost. We therefore hand it a
snapshot of our time-dependent matrix taken at mid-horizon -- the most
representative single slice available -- and we say so.

That is a genuine modelling advantage for our solver on the dynamic metric, and
pretending otherwise would be exactly the kind of handicapped-baseline
comparison the blueprint forbids. On the STATIC benchmark both solvers see the
same matrix and the comparison is clean.
"""
from __future__ import annotations

import math

from ..costs import TimeMatrix
from ..model import Instance, ObjectiveWeights, Route, Solution

SCALE = 10          # seconds -> deciseconds, keeps integers small


def solve_ortools(inst: Instance, tm: TimeMatrix, w: ObjectiveWeights,
                  time_budget: float = 0.45,
                  snapshot_t: float | None = None) -> Solution | None:
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError:
        return None

    cust = {c.id: c for c in inst.customers}
    K = len(inst.vehicles)
    if snapshot_t is None:
        snapshot_t = tm.horizon / 2.0

    # FAIRNESS: honour committed legs. Without this OR-Tools produces plans that
    # look faster but are operationally invalid -- it would be handicapped by a
    # constraint nobody told it about. We model each committed stop by starting
    # that vehicle FROM it and removing it from the pool, then prepend it back.
    committed = {v.id: v.committed_customer for v in inst.vehicles
                 if v.committed_customer is not None}
    excluded = set(committed.values())

    free_cust = [c.id for c in inst.customers if c.id not in excluded]
    nodes = [inst.depot_node] + free_cust
    n = len(nodes)

    starts, ends = [], []
    idx_of = {nid: i for i, nid in enumerate(nodes)}
    extra_nodes: list[int] = []
    for v in inst.vehicles:
        cm = committed.get(v.id)
        if cm is None:
            starts.append(0)
        else:
            if cm not in idx_of:
                idx_of[cm] = n + len(extra_nodes)
                extra_nodes.append(cm)
            starts.append(idx_of[cm])
        ends.append(0)
    nodes = nodes + extra_nodes
    n = len(nodes)

    # static snapshot of the time-dependent matrix
    M = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            v = tm.tt(nodes[i], nodes[j], snapshot_t)
            if math.isinf(v):
                v = 10 ** 7
            svc = cust[nodes[i]].service_time if nodes[i] in cust else 0.0
            M[i][j] = int((v + svc) * SCALE)

    mgr = pywrapcp.RoutingIndexManager(n, K, starts, ends)
    routing = pywrapcp.RoutingModel(mgr)

    def cb(a, b):
        return M[mgr.IndexToNode(a)][mgr.IndexToNode(b)]

    transit = routing.RegisterTransitCallback(cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    # capacity
    demands = [0] + [int(cust[nid].demand * SCALE) for nid in nodes[1:]]

    def dcb(a):
        return demands[mgr.IndexToNode(a)]

    dem_idx = routing.RegisterUnaryTransitCallback(dcb)
    routing.AddDimensionWithVehicleCapacity(
        dem_idx, 0, [int(v.capacity * SCALE) for v in inst.vehicles], True, "Capacity")

    # FAIRNESS: give OR-Tools the time windows too. Without this it solves a
    # capacity-only relaxation and then loses on a lateness term it was never
    # allowed to optimise -- which would make our win an artefact of the setup.
    horizon = int(tm.horizon * SCALE)
    routing.AddDimension(transit, horizon, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    for i, nid in enumerate(nodes):
        c = cust.get(nid)
        if c is None or not c.has_tw:
            continue
        try:
            idx = mgr.NodeToIndex(i)
        except Exception:                                # noqa: BLE001
            continue
        if idx < 0:
            continue
        lo = int(max(0.0, c.tw_start) * SCALE)
        hi = int(min(tm.horizon, c.tw_end) * SCALE)
        if lo <= hi:
            # soft upper bound: OR-Tools may exceed it, at a cost, exactly like
            # our lateness penalty -- a hard window would let it declare the
            # instance infeasible and return nothing.
            time_dim.CumulVar(idx).SetMin(lo)
            time_dim.SetCumulVarSoftUpperBound(
                idx, hi, int(w.beta_lateness * (3 if c.priority == 1 else 1)))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
    params.time_limit.FromMilliseconds(max(50, int(time_budget * 1000)))
    params.log_search = False

    sol = routing.SolveWithParameters(params)
    if sol is None:
        return None

    routes: list[Route] = []
    for k in range(K):
        idx = routing.Start(k)
        seq: list[int] = []
        while not routing.IsEnd(idx):
            nd = mgr.IndexToNode(idx)
            nid = nodes[nd]
            if nid != inst.depot_node and nid not in seq:
                seq.append(nid)
            idx = sol.Value(routing.NextVar(idx))
        vid = inst.vehicles[k].id
        cm = committed.get(vid)
        if cm is not None:
            seq = [cm] + [c for c in seq if c != cm]   # committed leg leads
        routes.append(Route(vehicle_id=vid, customer_ids=seq))
    return Solution(routes=routes)

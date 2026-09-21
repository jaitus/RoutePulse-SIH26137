"""Dynamic recovery engine — the live part.

Implements:
  * commitment freezing (a vehicle en route to its next stop finishes that leg)
  * the THREE-CASE acceptance rule (an infeasible incumbent must not be
    compared against by epsilon)
  * DECOMPOSED end-to-end latency: event -> accepted plan, with every stage
    timed separately. The matrix rebuild is inside the number, not outside it.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .costs import TimeMatrix
from .energy import EnergyMeter
from .emergency import (Ambulance, EmergencyCall, EmergencyResult,
                        EmergencyService, Hospital, pick_hospitals,
                        DEFAULT_CORRIDOR_MULT)
from .graph import RoadGraph, subgraph_around
from .model import Instance, ObjectiveWeights, Solution
from .solvers.heuristics import greedy_insertion, local_search
from .solvers.qpso import solve_qpso
from .validator import churn_components, score


EPSILON_REL = 0.01          # accept only a >1% improvement (anti-churn)


@dataclass
class ReplanResult:
    accepted: bool
    case: str
    solution: Solution
    incumbent_feasible: bool
    stages_ms: dict[str, float] = field(default_factory=dict)
    total_ms: float = 0.0
    candidates: list[dict] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    churn: dict = field(default_factory=dict)
    alert: str | None = None
    telemetry: dict = field(default_factory=dict)
    energy: dict = field(default_factory=dict)


class Engine:
    def __init__(self, graph: RoadGraph, inst: Instance, weights: ObjectiveWeights,
                 matrix_buckets: int = 5) -> None:
        self.inst = inst
        self.w = weights
        self.nodes = [inst.depot_node] + [c.id for c in inst.customers]
        # Route over the SERVICE AREA, not the whole city extract. Measured:
        # 93 Dijkstras over 6,420 junctions cost 4.4 s and blew the budget 9x.
        # subgraph_around() falls back to the full graph if pruning would
        # strand any stop, so this can only make things faster, never wrong.
        from .graph import subgraph_around
        self.g = subgraph_around(graph, self.nodes, margin_m=900.0)
        self.buckets = matrix_buckets
        self.tm = TimeMatrix(self.g, self.nodes, buckets=matrix_buckets)
        # BASELINE matrix: time-of-day only, no incidents, no corridor. The
        # congestion-exposure term in the official scorer is the difference
        # between the live matrix and this one, so "exposure" means a measured
        # number of extra seconds rather than a placeholder. The base profile
        # never changes, so this is built once and never rebuilt.
        self.tm_base = TimeMatrix(self.g, self.nodes, buckets=matrix_buckets,
                                  use_overlays=False)
        self.tm.base = self.tm_base
        self.incumbent: Solution | None = None

        # ------------------------------------------------------------------
        # THE SIMULATION CLOCK. One authoritative origin, in seconds from the
        # start of the planning horizon, shared by every event timestamp,
        # corridor window, vehicle availability time and ETA.
        #
        # This used to be three clocks that disagreed: ambulance dispatch
        # defaulted to now=0.0, UI events were stamped with wall-clock
        # time.time(), and the planner horizon was relative simulation time. A
        # corridor opened "now" therefore covered a window the fleet's ETAs
        # were not even expressed in.
        # ------------------------------------------------------------------
        self.now: float = 0.0
        self.event_log: list[dict] = []

        # edge keys whose cost CHANGED since the last matrix build. Tracked so
        # the re-plan can rebuild only the affected rows instead of everything.
        self.changed_keys: set[str] = set()
        # Scoped invalidation is only sound for cost INCREASES: a newly cheaper
        # path need never have been in the old shortest-path tree. Any decrease
        # -- a reopened road, an expired corridor -- sets this and forces a full
        # rebuild. Cheap insurance against silently stale ETAs.
        self.changed_decrease: bool = False
        # ALNS operator weights, persisted per EVENT TYPE across re-plans
        # (reviewer P1-04). A closure and an ambulance corridor are different
        # problems and must not share a prior.
        self.alns_memory: dict[str, dict] = {}
        self.last_event_type: str = "generic"
        self.last_event_keys: set[str] = set()
        # --- emergency layer (blueprint sections 4-5) -------------------------
        self.ems = EmergencyService(self.g)
        lat0 = sum(self.g.nodes[n][0] for n in self.nodes) / len(self.nodes)
        lon0 = sum(self.g.nodes[n][1] for n in self.nodes) / len(self.nodes)
        self.hospitals: list[Hospital] = pick_hospitals(self.g, lat0, lon0)
        self.ambulances: list[Ambulance] = []
        self.last_emergency: EmergencyResult | None = None

    def seed_ambulances(self, count: int = 2) -> list[Ambulance]:
        """Park ambulances on real junctions away from the depot."""
        pool = [n for n in self.g.nodes if n != self.inst.depot_node]
        pool.sort()
        step = max(1, len(pool) // max(1, count + 1))
        self.ambulances = [Ambulance(id=i, node=pool[min(len(pool) - 1,
                                                        (i + 1) * step)],
                                     name=f"AMB-{i + 1}")
                           for i in range(count)]
        return self.ambulances

    def dispatch_ambulance(self, lat: float, lon: float, severity: int = 2,
                           corridor_mult: float = DEFAULT_CORRIDOR_MULT,
                           now: float | None = None) -> dict:
        """Dispatch, publish the green corridor, and MEASURE BOTH SIDES.

        Priority is not free. The corridor that speeds the ambulance up slows
        the delivery fleet down, and the blueprint is explicit that both numbers
        get reported. We compute the fleet's cost under current costs, apply the
        corridor, recompute, and report the difference as the price of priority.
        """
        if not self.ambulances:
            self.seed_ambulances()
        # ONE clock. The dispatch time is simulation time, the same origin the
        # fleet's ETAs and the corridor windows are expressed in.
        now = self.now if now is None else float(now)
        node = self.g.nearest_node(lat, lon)
        call = EmergencyCall(id=len(self.event_log) + 1, node=node,
                             severity=severity, dispatch_time=now)

        res = self.ems.dispatch(call, self.ambulances, self.hospitals)
        self.last_emergency = res
        if res.unreachable:
            return {"ok": False, "reason": "no reachable unit or hospital"}

        # fleet cost BEFORE the corridor exists
        before = None
        if self.incumbent is not None:
            before = score(self.inst, self.incumbent.copy(), self.tm, self.w).score

        t0, t1 = res.corridor_window
        self.apply_corridor(res.leg_a_nodes + res.leg_b_nodes,
                            multiplier=corridor_mult, t0=t0, window=t1 - t0,
                            edge_windows=res.corridor_windows,
                            tag=f"corridor-{call.id}")

        # the corridor changed edge costs, so the matrix is stale for the
        # comparison below -- rebuild before measuring, or the "cost of
        # priority" would be computed against a matrix that has not seen it
        rows = self.tm.rows_affected_by(self.changed_keys)
        if rows:
            self.tm.rebuild_rows(rows)
        after = None
        if self.incumbent is not None:
            after = score(self.inst, self.incumbent.copy(), self.tm, self.w).score

        cost_of_priority = (after - before) if (before is not None
                                                and after is not None) else None
        unit = next((a for a in self.ambulances if a.id == res.unit_id), None)
        if unit is not None:
            unit.busy_until = now + res.total_seconds

        self.log_event("ambulance",
                       f"Ambulance {unit.name if unit else res.unit_id} → "
                       f"{res.hospital}", lat=lat, lon=lon,
                       edges=len(res.corridor_keys), call_id=call.id)

        return {
            "ok": True,
            "call_id": call.id,
            "corridor_tag": f"corridor-{call.id}",
            "unit": unit.name if unit else f"AMB-{res.unit_id}",
            "hospital": res.hospital,
            "to_scene_min": round(res.leg_a_seconds / 60, 1),
            "to_hospital_min": round(res.leg_b_seconds / 60, 1),
            "total_min": round(res.total_seconds / 60, 1),
            "baseline_min": round(res.baseline_seconds / 60, 1),
            "time_saved_min": round(res.time_saved / 60, 1),
            "path_ms": round(res.latency_ms, 1),
            "corridor_edges": len(res.corridor_keys),
            "corridor_window_min": [round(t0 / 60, 1), round(t1 / 60, 1)],
            # Per-edge occupancy is the whole point of P0-04: report the SPREAD
            # of individual edge windows, not one route-level blanket.
            "corridor_edge_windows": len(res.corridor_windows),
            "corridor_edge_window_s": (
                round(sum(b - a for _k, a, b in res.corridor_windows)
                      / len(res.corridor_windows), 1)
                if res.corridor_windows else None),
            "fleet_cost_before": None if before is None else round(before, 1),
            "fleet_cost_after": None if after is None else round(after, 1),
            "cost_of_priority": None if cost_of_priority is None
            else round(cost_of_priority, 1),
            "leg_a_nodes": res.leg_a_nodes,
            "leg_b_nodes": res.leg_b_nodes,
        }

    # ------------------------------------------------------------- planning

    def initial_plan(self, budget: float = 1.2, seed: int = 0,
                     improver: str = "alns") -> Solution:
        """Initial planning warm-starts from a greedy construction.

        IMPROVER DEFAULT: `alns`, and that default was earned rather than
        chosen. Traffic-Aware ALNS was benchmarked against the 2-opt/relocate/
        swap layer from the same greedy start, at an identical wall-clock
        budget, over 30 paired seeds: **7.2% better, p < 0.0001**. The
        blueprint required an explicit adoption gate before ALNS could replace
        the improvement layer; it passed, so it is the default. Pass
        `improver="qpso"` to get the previous behaviour.

        QPSO is not displaced by this. It remains Deliverable 3's
        quantum-inspired module and it runs in every re-plan race, where its
        contribution is reported rather than assumed -- which is exactly how
        ALNS came to be measured in the first place.

        This setting is GRAPH-DEPENDENT and we have measured both ways:

          synthetic grid   warm start was 11.4% WORSE than cold
          real Bengaluru   warm start is   2.2% BETTER than cold

        On the sparse synthetic grid, seeding the swarm from greedy collapsed
        diversity (attractor, gbest and mbest coincide, well width -> 0). On the
        real road network the search space is far rougher and a good incumbent
        is worth more than the diversity it costs. We follow the real-graph
        measurement because that is the deployment target, and we record the
        contradiction rather than quietly picking the flattering number.

        Re-planning warm-starts for a second, independent reason: CHURN. A cold
        solve returns a completely different plan and re-tasks every driver. The
        benchmark scores solution quality only, so it cannot see that.
        """
        base = score(self.inst, greedy_insertion(self.inst, self.tm, self.w, seed),
                     self.tm, self.w)
        if improver == "alns":
            from .solvers.alns import alns_with_telemetry
            sol, _ = alns_with_telemetry(self.inst, base, self.tm, self.w,
                                         time.perf_counter() + budget, seed=seed,
                                         memory=self.alns_memory,
                                         event_type="initial")
        else:
            sol, _ = solve_qpso(self.inst, self.tm, self.w, time_budget=budget,
                                seed=seed, warm_start=base)
        sol = score(self.inst, sol, self.tm, self.w)
        if base.feasible and (not sol.feasible or base.score < sol.score):
            sol = base
        self.incumbent = sol
        return sol

    def remove_vehicle(self, vehicle_id: int) -> int:
        """Take a vehicle out of service (breakdown).

        Its stops are released back into the pending pool and its route is
        dropped from the incumbent. Modelling a breakdown as capacity=0 instead
        would leave a vehicle the solver must route but cannot load -- an
        infeasible instance rather than a recoverable incident.
        """
        released = 0
        self.inst.vehicles = [v for v in self.inst.vehicles if v.id != vehicle_id]
        if self.incumbent is not None:
            keep = []
            for r in self.incumbent.routes:
                if r.vehicle_id == vehicle_id:
                    released = len(r.customer_ids)
                else:
                    keep.append(r)
            self.incumbent.routes = keep
        return released

    def freeze_commitments(self) -> list[int]:
        """Mark each vehicle's next stop as committed and non-replannable."""
        frozen: list[int] = []
        if self.incumbent is None:
            return frozen
        veh = {v.id: v for v in self.inst.vehicles}
        for r in self.incumbent.routes:
            # a route can outlive its vehicle if one was removed mid-plan
            v = veh.get(r.vehicle_id)
            if v is not None and r.customer_ids:
                v.committed_customer = r.customer_ids[0]
                frozen.append(r.customer_ids[0])
        return frozen

    # --------------------------------------------------------------- events

    def apply_closure(self, lat: float, lon: float, radius_m: float = 320.0) -> list[str]:
        """Close roads, but never orphan a delivery address.

        A real road closure degrades access; it does not make a street address
        unreachable. If we closed every edge incident to a stop, the instance
        would become trivially infeasible and the demo would only ever show the
        escalation path. So we protect the last in-edge and out-edge of every
        node that is a depot or a customer.
        """
        protected = {self.inst.depot_node} | {c.id for c in self.inst.customers}
        # Never close an edge that is a stop's own access. Those are exactly the
        # edges that strand a delivery address, and closing them forced the
        # connectivity guard below to reopen EVERYTHING -- which silently turned
        # scenarios S1 and S5 into vacuous no-ops that still reported green.
        # Excluding them up front means the guard has a feasible subset to keep.
        candidate = [k for k in self.g.edges_near(lat, lon, radius_m)
                     if int(k.split("->")[0]) not in protected
                     and int(k.split("->")[1]) not in protected]
        closed: list[str] = []
        for k in candidate:
            self.g.close_edge(k)
            closed.append(k)
            self.changed_keys.add(k)
        self.last_event_type = "closure"
        self.last_event_keys = set(closed)

        # Connectivity guard, progressive. The previous version reopened only
        # edges TOUCHING a stranded stop, but a stop is usually stranded by
        # edges nowhere near it -- its access road leads into a sealed pocket.
        # Nothing matched, so it fell through to reopening everything, which
        # silently turned S1 and S5 into vacuous no-ops. Reopening in batches
        # (most-recently-closed first) restores reachability while keeping most
        # of the closure, and it always terminates.
        if self._stranded(protected):
            batch = max(1, len(closed) // 8)
            while closed and self._stranded(protected):
                for k in closed[-batch:]:
                    self.g.reopen_edge(k)
                    self.changed_keys.discard(k)
                closed = closed[:-batch]
        return closed

    def _stranded(self, stops: set[int]) -> set[int]:
        """Stops that can no longer be reached from the depot, or reach it."""
        fwd = self._bfs(self.inst.depot_node, reverse=False)
        bwd = self._bfs(self.inst.depot_node, reverse=True)
        return {s for s in stops if s not in fwd or s not in bwd}

    def _bfs(self, src: int, reverse: bool) -> set[int]:
        if reverse and not hasattr(self, "_rev_cache"):
            rev: dict[int, list[tuple[int, str]]] = {}
            for u, out in self.g.adj.items():
                for (v, _, _, key) in out:
                    rev.setdefault(v, []).append((u, key))
            self._rev_cache = rev
        seen = {src}
        stack = [src]
        while stack:
            u = stack.pop()
            if reverse:
                nbrs = [(w, key) for (w, key) in self._rev_cache.get(u, ())]
            else:
                nbrs = [(v, key) for (v, _, _, key) in self.g.adj.get(u, ())]
            for w, key in nbrs:
                if w in seen:
                    continue
                if self.g.is_closed(key):
                    continue
                seen.add(w)
                stack.append(w)
        return seen

    def apply_congestion(self, lat: float, lon: float, multiplier: float = 4.0,
                         radius_m: float = 420.0, tag: str = "") -> list[str]:
        """Add a soft slowdown. It composes with any closure already on the
        edge instead of overwriting it -- a jam on a closed road leaves the
        road closed, which is the whole point of P0-07."""
        keys = self.g.edges_near(lat, lon, radius_m)
        tag = tag or f"jam-{len(self.event_log) + 1}"
        for k in keys:
            self.g.congest_edge(k, multiplier, tag=tag)
            self.changed_keys.add(k)
        self.last_event_type = "congestion"
        self.last_event_keys = set(keys)
        return keys

    def clear_congestion(self, tag: str | None = None) -> int:
        """Lift a jam. A cost DECREASE, so it forces a full matrix rebuild."""
        n = self.g.clear_overlays(kind="congestion", tag=tag)
        if n:
            self.changed_decrease = True
            self.log_event("jam_cleared", f"Congestion cleared ({n} edges)")
        return n

    def reopen_roads(self, keys: list[str]) -> int:
        """Lift a closure. Also a decrease, also a full rebuild."""
        n = 0
        for k in keys:
            if self.g.is_closed(k):
                self.g.reopen_edge(k)
                n += 1
        if n:
            self.changed_decrease = True
            self.log_event("reopened", f"Road reopened ({n} edges)")
        return n

    def apply_corridor(self, path_nodes: list[int], multiplier: float = 3.0,
                       t0: float = 0.0, window: float = 1800.0,
                       edge_windows: list[tuple[str, float, float]] | None = None,
                       tag: str = "corridor") -> list[str]:
        """Publish the green corridor as PER-EDGE occupancy windows.

        The old version applied one route-level window to every edge, which
        says the far end of a 5 km corridor is blocked from the moment the
        ambulance leaves the depot. It is not: the ambulance reaches that edge
        minutes later and clears it seconds after that. A delivery vehicle
        crossing before or after that slot should pay nothing, and under the
        route-level window it paid in full.

        `edge_windows` carries (edge_key, start, end) derived from the
        predicted ambulance arrival time at each edge. The keys-only form is
        retained for callers that genuinely want one shared window.
        """
        if edge_windows:
            self.g.open_corridor(edge_windows, multiplier=multiplier, tag=tag)
            keys = [k for k, _a, _b in edge_windows]
        else:
            keys = [f"{a}->{b}" for a, b in zip(path_nodes, path_nodes[1:])]
            self.g.open_corridor(keys, multiplier, t0, t0 + window, tag=tag)
        self.changed_keys.update(keys)
        self.last_event_type = "ambulance"
        self.last_event_keys = set(keys)
        return keys

    def expire_corridor(self, tag: str = "corridor") -> int:
        """Corridor expiry is an EVENT, not a quiet cleanup.

        It lowers costs, which scoped invalidation cannot reason about, so it
        flags a full matrix rebuild. Returns how many overlays were removed.
        """
        n = self.g.clear_overlays(kind="corridor", tag=tag)
        if n:
            self.changed_decrease = True
            self.log_event("corridor_expired", f"Green corridor expired ({n} edges)")
        return n

    # ----------------------------------------------------------- the clock

    def log_event(self, kind: str, label: str, **extra) -> dict:
        """Every event is stamped with the ONE simulation clock."""
        ev = {"kind": kind, "label": label, "t": round(self.now, 2), **extra}
        self.event_log.append(ev)
        return ev

    def advance(self, seconds: float) -> dict:
        """Move the simulation clock forward and let the fleet actually drive.

        Until this existed, "dynamic re-planning" always restarted from the
        depot with the full customer set: the incumbent changed but the world
        never did. Advancing the clock does what a real shift does --

          * stops whose planned arrival has passed are SERVED and leave the
            instance, because a delivered parcel is not pending work;
          * each vehicle's position becomes its last served stop and its
            earliest availability becomes the moment it finished there;
          * the travel-time matrix is rebuilt over the remaining node set.

        Re-planning then starts from where the fleet is, not from the depot.
        """
        self.now = max(self.now, self.now + max(0.0, seconds))
        if self.incumbent is None:
            return {"served": 0, "remaining": self.inst.n, "now": self.now}

        veh = {v.id: v for v in self.inst.vehicles}
        served: list[int] = []
        cust = {c.id: c for c in self.inst.customers}

        for r in self.incumbent.routes:
            v = veh.get(r.vehicle_id)
            if v is None:
                continue
            last_node, last_t = None, None
            keep: list[int] = []
            for cid, arr in zip(r.customer_ids, r.arrival_times):
                c = cust.get(cid)
                if c is None or math.isinf(arr):
                    keep.append(cid)
                    continue
                done_at = arr + c.service_time
                if done_at <= self.now:
                    served.append(cid)
                    last_node, last_t = cid, done_at
                else:
                    keep.append(cid)
            r.customer_ids = keep
            if last_node is not None:
                v.start_node = last_node
                v.available_at = max(v.available_at, last_t)
            if v.committed_customer in served:
                v.committed_customer = None

        if served:
            done = set(served)
            self.inst.customers = [c for c in self.inst.customers
                                   if c.id not in done]
            self.inst.__post_init__()
            self._rebuild_node_set()
            self.log_event("advance",
                           f"Clock +{seconds / 60:.0f} min · {len(served)} "
                           f"stop(s) completed", served=len(served))
        return {"served": len(served), "remaining": self.inst.n, "now": self.now}

    def _rebuild_node_set(self) -> None:
        """The matrix is indexed by depot + pending customers. When customers
        leave the instance that index changes, so the matrix and its baseline
        are rebuilt together -- keeping one and not the other would make the
        congestion-exposure term compare two different node sets."""
        self.nodes = [self.inst.depot_node] + [c.id for c in self.inst.customers]
        extra = {v.start_node for v in self.inst.vehicles}
        for n in sorted(extra):
            if n not in self.nodes and n in self.g.nodes:
                self.nodes.append(n)
        self.tm = TimeMatrix(self.g, self.nodes, buckets=self.buckets)
        self.tm_base = TimeMatrix(self.g, self.nodes, buckets=self.buckets,
                                  use_overlays=False)
        self.tm.base = self.tm_base
        self.changed_keys.clear()
        self.changed_decrease = False

    # -------------------------------------------------------------- re-plan

    def replan(self, budget: float = 0.45, seed: int = 0,
               scoped_nodes: set[int] | None = None,
               engines: tuple[str, ...] = ("emergency", "qpso", "alns", "ortools"),
               ) -> ReplanResult:
        """Event -> accepted plan, with every stage timed."""
        t_start = time.perf_counter()
        t_cpu = time.process_time()
        stages: dict[str, float] = {}

        # ---- stage 1: freeze commitments
        t = time.perf_counter()
        self.freeze_commitments()
        stages["freeze_commitments"] = (time.perf_counter() - t) * 1000

        # ---- stage 2: FIFO re-assert on the effective profile
        t = time.perf_counter()
        # Only edges carrying a time-varying overlay can break FIFO; the base
        # profile is verified once at load. Scanning all 16,413 edges here cost
        # 400 ms of a 500 ms budget and told us nothing.
        overlay_keys = self.g.dynamic_keys()
        fifo_bad = self.g.check_fifo(samples=12, only_keys=overlay_keys)
        stages["fifo_assert"] = (time.perf_counter() - t) * 1000

        # ---- stage 3: travel-time matrix rebuild (INSIDE the budget)
        t = time.perf_counter()
        if self.changed_decrease:
            # A cost DECREASE -- a reopened road, an expired corridor, a jam
            # lifted. Scoped invalidation is unsound here: a newly cheaper path
            # need never have appeared in the old shortest-path tree, so there
            # is nothing to match against. Full rebuild, and say so.
            self.tm.rebuild_all()
            self.changed_decrease = False
        elif scoped_nodes:
            self.tm.rebuild_rows(scoped_nodes)
        elif self.changed_keys:
            # Every remaining event is a cost INCREASE (closure, congestion,
            # corridor), so a source's row is stale only if one of the changed
            # edges is in its shortest-path tree. That is provably sufficient
            # and typically touches a handful of rows instead of all of them.
            rows = self.tm.rows_affected_by(self.changed_keys)
            if rows:
                self.tm.rebuild_rows(rows)
            else:
                self.tm.last_pairs_rebuilt = 0
        else:
            self.tm.rebuild_all()
        self.changed_keys.clear()
        stages["matrix_rebuild"] = (time.perf_counter() - t) * 1000
        pairs = self.tm.last_pairs_rebuilt

        # ---- stage 4: evaluate the incumbent under CURRENT costs
        t = time.perf_counter()
        incumbent_feasible = False
        inc_score = math.inf
        if self.incumbent is not None:
            inc = score(self.inst, self.incumbent.copy(), self.tm, self.w)
            incumbent_feasible = inc.feasible
            inc_score = inc.score
            self.incumbent = inc
        stages["evaluate_incumbent"] = (time.perf_counter() - t) * 1000

        # ---- stage 5: solve, under ONE GLOBAL DEADLINE
        #
        # This used to give every engine its own `budget`, so a four-engine
        # race took four budgets plus overhead and the "500 ms target" was
        # quietly a per-engine target. A dispatcher does not wait per engine.
        # Now `budget` is the wall-clock allowance for the WHOLE solve stage,
        # shared across the engines that were asked for, and every engine is
        # additionally clamped by the hard deadline so an overrun in one
        # cannot eat another's time.
        t = time.perf_counter()
        candidates: list[dict] = []
        best: Solution | None = None
        telemetry: dict = {}

        deadline = t + budget
        left = [len(engines)]          # engines still to run

        def remaining() -> float:
            return max(0.0, deadline - time.perf_counter())

        def slot() -> float:
            """This engine's slice of what is LEFT, never past the deadline.

            Dividing the remaining time rather than the original budget is
            self-correcting: an engine that overruns its slice shrinks every
            later slice instead of pushing the whole stage past the deadline.
            A fixed 1/N share compounds overruns; this one absorbs them.
            """
            n = max(1, left[0])
            left[0] -= 1
            return max(0.01, remaining() / n)

        if "emergency" in engines:
            te = time.perf_counter()
            s = greedy_insertion(self.inst, self.tm, self.w, seed)
            s = local_search(self.inst, s, self.tm, self.w,
                             min(deadline, time.perf_counter() + slot() * 0.6))
            s = score(self.inst, s, self.tm, self.w, self.incumbent)
            candidates.append(self._cand("Emergency heuristic", s,
                                         (time.perf_counter() - te) * 1000))
            if s.feasible and (best is None or s.score < best.score):
                best = s

        if "qpso" in engines and remaining() > 0.02:
            tq = time.perf_counter()
            s, tel = solve_qpso(self.inst, self.tm, self.w, time_budget=slot(),
                                seed=seed, warm_start=self.incumbent,
                                previous=self.incumbent)
            telemetry = tel
            candidates.append(self._cand("QPSO + local search", s,
                                         (time.perf_counter() - tq) * 1000))
            if s.feasible and (best is None or s.score < best.score):
                best = s

        if "alns" in engines and remaining() > 0.02:
            ta = time.perf_counter()
            from .solvers.alns import alns_with_telemetry
            base = self.incumbent.copy() if self.incumbent is not None else \
                greedy_insertion(self.inst, self.tm, self.w, seed)
            s, atel = alns_with_telemetry(
                self.inst, base, self.tm, self.w,
                min(deadline, time.perf_counter() + slot()), seed=seed,
                event_keys=set(self.last_event_keys),
                memory=self.alns_memory, event_type=self.last_event_type)
            s = score(self.inst, s, self.tm, self.w, self.incumbent)
            telemetry["alns"] = atel
            candidates.append(self._cand("Traffic-Aware ALNS", s,
                                         (time.perf_counter() - ta) * 1000))
            if s.feasible and (best is None or s.score < best.score):
                best = s

        if "sb" in engines and remaining() > 0.02:
            tb = time.perf_counter()
            try:
                from .solvers.sb import sb_resequence
                base = (best or self.incumbent)
                if base is not None:
                    s, stel = sb_resequence(
                        self.inst, base.copy(), self.tm, self.w,
                        min(deadline, time.perf_counter() + slot()), seed=seed)
                    s = score(self.inst, s, self.tm, self.w, self.incumbent)
                    telemetry["sb"] = stel
                    candidates.append(self._cand("Simulated Bifurcation", s,
                                                 (time.perf_counter() - tb) * 1000))
                    if s.feasible and (best is None or s.score < best.score):
                        best = s
            except Exception as e:                       # noqa: BLE001
                candidates.append({"engine": "Simulated Bifurcation",
                                   "error": str(e)[:120]})

        if "ortools" in engines:
            try:
                from .solvers.ortools_baseline import solve_ortools
                to = time.perf_counter()
                s = (solve_ortools(self.inst, self.tm, self.w,
                                   time_budget=slot())
                     if remaining() > 0.02 else None)
                if s is not None:
                    s = score(self.inst, s, self.tm, self.w, self.incumbent)
                    candidates.append(self._cand("OR-Tools", s,
                                                 (time.perf_counter() - to) * 1000))
                    if s.feasible and (best is None or s.score < best.score):
                        best = s
            except Exception as e:                       # noqa: BLE001
                candidates.append({"engine": "OR-Tools", "error": str(e)[:120]})
        stages["solve"] = (time.perf_counter() - t) * 1000

        # ---- stage 6: acceptance (three cases)
        t = time.perf_counter()
        accepted = False
        alert = None
        if best is None:
            # CASE 3 -- nothing feasible found
            case = "CASE 3: no feasible plan found before deadline"
            alert = ("No feasible recovery plan within the deadline. Holding the "
                     "incumbent and escalating to the dispatcher. Violations: "
                     + "; ".join((self.incumbent.violations if self.incumbent else [])[:3]))
            final = self.incumbent if self.incumbent is not None else Solution()
        elif not incumbent_feasible:
            # CASE 2 -- incumbent invalid, any feasible plan wins
            case = "CASE 2: incumbent infeasible -> accept best feasible, no epsilon test"
            final = best
            accepted = True
        else:
            # CASE 1 -- both feasible, require a real improvement
            gain = (inc_score - best.score) / max(1e-9, inc_score)
            if gain > EPSILON_REL:
                case = f"CASE 1: accepted, {gain*100:.1f}% better than incumbent"
                final = best
                accepted = True
            else:
                case = (f"CASE 1: rejected, only {gain*100:.1f}% better "
                        f"(< {EPSILON_REL*100:.0f}% threshold) - not worth the churn")
                final = self.incumbent      # type: ignore[assignment]
        stages["acceptance"] = (time.perf_counter() - t) * 1000

        churn = {}
        explanation: list[str] = []
        if accepted and self.incumbent is not None:
            churn = churn_components(final, self.incumbent)
            explanation = self._explain(final, self.incumbent, churn, inc_score)

        if accepted:
            # unfreeze for the next cycle
            for v in self.inst.vehicles:
                v.committed_customer = None
            self.incumbent = final

        total = (time.perf_counter() - t_start) * 1000
        # Energy for THIS re-plan, on the same event -> accepted plan boundary
        # as the latency number. Reported per invocation because that is the
        # unit a depot actually pays for: this fires on every incident, not
        # once a night.
        energy = EnergyMeter.account("replan", total / 1000.0,
                                     time.process_time() - t_cpu)
        return ReplanResult(
            accepted=accepted, case=case, solution=final,
            incumbent_feasible=incumbent_feasible,
            stages_ms={k: round(v, 2) for k, v in stages.items()},
            total_ms=round(total, 2), candidates=candidates,
            explanation=explanation, churn=churn, alert=alert,
            telemetry={**telemetry, "matrix_pairs_rebuilt": pairs,
                       "fifo_violations": len(fifo_bad)},
            energy=energy.to_dict(),
        )

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _cand(name: str, s: Solution, ms: float) -> dict:
        return {
            "engine": name,
            "score": None if math.isinf(s.score) else round(s.score, 1),
            "travel_min": None if math.isinf(s.travel_time) else round(s.travel_time / 60, 1),
            "feasible": s.feasible,
            "violations": s.violations[:2],
            "ms": round(ms, 1),
        }

    def _explain(self, new: Solution, old: Solution, churn: dict,
                 old_score: float) -> list[str]:
        """Explanation generated from the STATE DIFF, not from prose."""
        out: list[str] = []
        a_old, a_new = old.assignment(), new.assignment()
        moved = [c for c in a_new if c in a_old and a_old[c] != a_new[c]]
        if moved:
            out.append(f"{len(moved)} stop(s) reassigned to a different vehicle: "
                       f"{sorted(moved)[:5]}")
        if churn.get("vehicles_changed"):
            out.append(f"Vehicles whose route changed: {churn['vehicles_changed']}")
        else:
            out.append("No vehicle needed a route change.")
        saved = (old_score - new.score) / 60.0
        if saved > 0:
            out.append(f"Expected fleet cost reduced by {saved:.1f} minute-equivalents.")
        committed = [v.committed_customer for v in self.inst.vehicles
                     if v.committed_customer is not None]
        if committed:
            out.append(f"{len(committed)} vehicle(s) completed a committed leg first "
                       f"- commitment beats re-planning.")
        late = sum(1 for r in new.routes for cid, arr in
                   zip(r.customer_ids, r.arrival_times)
                   if not math.isinf(arr))
        out.append(f"{late} stops re-timed against the updated travel-time profile.")
        return out

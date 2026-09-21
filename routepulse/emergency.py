"""Emergency-vehicle priority and the green corridor.

THE DESIGN DECISION THAT KEEPS THIS CLEAN
=========================================
An ambulance is NOT a vehicle in the VRP. The delivery problem is a capacitated,
time-windowed, multi-vehicle routing problem. The ambulance problem is a
single-origin, single-destination, time-dependent shortest path under a
different cost model and a different deadline.

Forcing the ambulance into the CVRP as "a vehicle with one very high-priority
stop" would corrupt the objective, the capacity constraints and the churn
penalty simultaneously. So the two are solved SEPARATELY and coupled through the
cost layer -- which turns out to need no new machinery at all, because a
corridor is structurally identical to a congestion incident, and the recovery
engine already knows how to handle one of those.

WHAT PRIORITY DOES AND DOES NOT MEAN
====================================
  * DOES: signal pre-emption and yielding, modelled as a speed factor and the
    removal of junction delay.
  * DOES NOT: ignore one-way streets or drive through physical closures.
    An ambulance gets priority, not teleportation. Scenario S9 exists to prove
    this -- it routes AROUND a closure rather than through it.

THE CORRIDOR IS A FORECAST, NOT A MEASUREMENT
=============================================
At planning time the ambulance has not yet traversed the corridor, so nothing
has been observed. kappa encodes ANTICIPATED delay over the forward window
[t0, t1] only. Any measured slowdown belongs to elapsed time. The two cover
disjoint time ranges and must never be summed for the same instant -- that
double-counts one physical effect, which is the mistake the pre-freeze review
flagged.
"""
from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field

from .graph import RoadGraph


# Emergency vehicles travel faster than the free-flow limit under priority, but
# within a bounded factor. Value is recorded in every run log rather than
# hard-coded into a claim.
DEFAULT_SPEED_FACTOR = 1.6
DEFAULT_CORRIDOR_MULT = 3.0
DEFAULT_CORRIDOR_WINDOW = 1500.0     # seconds the corridor stays warm
ON_SCENE_SECONDS = 300.0             # time the crew spends at the scene


@dataclass
class Hospital:
    node: int
    name: str
    tier: int = 1          # 1 = general, 2 = trauma-capable
    lat: float = 0.0
    lon: float = 0.0


@dataclass
class Ambulance:
    id: int
    node: int              # current position
    name: str = ""
    available_at: float = 0.0
    busy_until: float | None = None


@dataclass
class EmergencyCall:
    id: int
    node: int
    severity: int = 1      # 1 = routine, 2 = critical (needs a tier-2 hospital)
    dispatch_time: float = 0.0


@dataclass
class EmergencyResult:
    call_id: int
    unit_id: int
    hospital: str
    leg_a_nodes: list[int] = field(default_factory=list)
    leg_b_nodes: list[int] = field(default_factory=list)
    leg_a_seconds: float = 0.0
    leg_b_seconds: float = 0.0
    total_seconds: float = 0.0
    baseline_seconds: float = 0.0     # same route WITHOUT priority
    corridor_keys: list[str] = field(default_factory=list)
    corridor_window: tuple[float, float] = (0.0, 0.0)
    # PER-EDGE occupancy: (edge_key, window_start, window_end), derived from
    # when the ambulance is actually predicted to be on that edge.
    corridor_windows: list[tuple[str, float, float]] = field(default_factory=list)
    latency_ms: float = 0.0
    unreachable: bool = False

    @property
    def time_saved(self) -> float:
        return max(0.0, self.baseline_seconds - self.total_seconds)


class EmergencyService:
    """Time-dependent shortest path under the emergency cost model."""

    def __init__(self, graph: RoadGraph,
                 speed_factor: float = DEFAULT_SPEED_FACTOR) -> None:
        self.g = graph
        self.speed_factor = speed_factor

    # ------------------------------------------------------------- cost model

    def _edge_seconds(self, u: int, v: int, length_m: float, spd: float,
                      key: str, t: float, priority: bool) -> float:
        """Emergency cost model.

        Priority divides travel time by the speed factor and drops junction
        delay. Closures stay closed -- `edge_multiplier` returns inf and we
        propagate it -- and one-ways are already encoded in the directed graph.
        """
        m = self.g.edge_multiplier(key, t)
        if math.isinf(m):
            return math.inf
        base = length_m / (spd * 1000.0 / 3600.0)
        if not priority:
            return base * m
        # Under priority the vehicle is far less affected by congestion, but not
        # immune to it: a jammed road is still slow even with a siren.
        eased = 1.0 + (m - 1.0) * 0.35
        return base * eased / self.speed_factor

    def shortest(self, src: int, dst: int, depart_t: float,
                 priority: bool = True) -> tuple[float, list[int]]:
        """Returns (seconds, node path). inf/[] when unreachable."""
        dist = {src: 0.0}
        prev: dict[int, int] = {}
        pq: list[tuple[float, int]] = [(0.0, src)]
        seen: set[int] = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == dst:
                break
            for (v, L, spd, key) in self.g.adj.get(u, ()):
                s = self._edge_seconds(u, v, L, spd, key, depart_t + d, priority)
                if math.isinf(s):
                    continue
                nd = d + s
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if dst not in dist:
            return math.inf, []
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        return dist[dst], list(reversed(path))

    def edge_windows(self, path: list[int], start_t: float,
                     lead_s: float = 120.0, tail_s: float = 180.0,
                     priority: bool = True) -> list[tuple[str, float, float]]:
        """Predicted occupancy window for each edge of an ambulance path.

        Walk the path forward at the emergency cost model and record when the
        vehicle enters and leaves each edge. The window is padded by `lead_s`
        before (traffic clearing ahead of the siren) and `tail_s` after (the
        queue taking a while to re-form), which is what actually makes a
        corridor a corridor rather than an instant.

        This is the difference between "the whole route is blocked for 25
        minutes" and "this edge is busy between t+4:10 and t+9:30". The second
        one is the claim we can defend.
        """
        out: list[tuple[str, float, float]] = []
        t = start_t
        for a, b in zip(path, path[1:]):
            edge = next(((L, spd, key) for (v, L, spd, key)
                         in self.g.adj.get(a, ()) if v == b), None)
            if edge is None:
                continue
            L, spd, key = edge
            secs = self._edge_seconds(a, b, L, spd, key, t, priority)
            if math.isinf(secs):
                break
            out.append((key, max(0.0, t - lead_s), t + secs + tail_s))
            t += secs
        return out

    # --------------------------------------------------------------- dispatch

    def dispatch(self, call: EmergencyCall, units: list[Ambulance],
                 hospitals: list[Hospital]) -> EmergencyResult:
        """Nearest AVAILABLE unit by travel time, nearest SUITABLE hospital.

        "Suitable" matters: a critical call needs a trauma-capable facility, so
        the nearest hospital is not always the right hospital. Keeping the
        suitability rule explicit and small is deliberate -- real triage is a
        clinical decision and is out of scope (see README limitations).
        """
        t0 = time.perf_counter()
        free = [u for u in units
                if u.busy_until is None or u.busy_until <= call.dispatch_time]
        if not free:
            free = units

        best_unit, best_a, best_path = None, math.inf, []
        for u in free:
            secs, path = self.shortest(u.node, call.node, call.dispatch_time)
            if secs < best_a:
                best_unit, best_a, best_path = u, secs, path

        if best_unit is None or math.isinf(best_a):
            return EmergencyResult(call_id=call.id, unit_id=-1, hospital="",
                                   unreachable=True,
                                   latency_ms=(time.perf_counter() - t0) * 1000)

        scene_done = call.dispatch_time + best_a + ON_SCENE_SECONDS
        eligible = [h for h in hospitals if h.tier >= call.severity] or hospitals
        best_h, best_b, best_hpath = None, math.inf, []
        for h in eligible:
            secs, path = self.shortest(call.node, h.node, scene_done)
            if secs < best_b:
                best_h, best_b, best_hpath = h, secs, path

        if best_h is None or math.isinf(best_b):
            return EmergencyResult(call_id=call.id, unit_id=best_unit.id,
                                   hospital="", unreachable=True,
                                   latency_ms=(time.perf_counter() - t0) * 1000)

        # counterfactual: the same dispatch WITHOUT priority, so "time saved"
        # is measured against something real rather than asserted
        base_a, _ = self.shortest(best_unit.node, call.node,
                                  call.dispatch_time, priority=False)
        base_b, _ = self.shortest(call.node, best_h.node,
                                  scene_done, priority=False)
        baseline = (base_a + base_b) if not (math.isinf(base_a) or math.isinf(base_b)) \
            else best_a + best_b

        keys: list[str] = []
        for seq in (best_path, best_hpath):
            for a, b in zip(seq, seq[1:]):
                keys.append(f"{a}->{b}")

        # Per-edge occupancy, on the same clock as the dispatch itself.
        windows = (self.edge_windows(best_path, call.dispatch_time)
                   + self.edge_windows(best_hpath, scene_done))

        return EmergencyResult(
            call_id=call.id, unit_id=best_unit.id, hospital=best_h.name,
            leg_a_nodes=best_path, leg_b_nodes=best_hpath,
            leg_a_seconds=best_a, leg_b_seconds=best_b,
            total_seconds=best_a + best_b, baseline_seconds=baseline,
            corridor_keys=keys,
            corridor_window=(call.dispatch_time,
                             max([w[2] for w in windows], default=
                                 call.dispatch_time + DEFAULT_CORRIDOR_WINDOW)),
            corridor_windows=windows,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )


def pick_hospitals(g: RoadGraph, centre_lat: float, centre_lon: float,
                   count: int = 3, radius_m: float = 2500.0) -> list[Hospital]:
    """Place synthetic receiving hospitals on real junctions.

    These are NOT real hospital locations -- OSM hospital tags are not in our
    extract, and inventing real facility names would be worse than labelling
    them plainly. They are plausible receiving points for a traffic-interaction
    demo, and the README says so.
    """
    from .graph import haversine_m
    cands = [(n, la, lo) for n, (la, lo) in g.nodes.items()
             if haversine_m(la, lo, centre_lat, centre_lon) <= radius_m]
    if not cands:
        cands = [(n, la, lo) for n, (la, lo) in g.nodes.items()]
    cands.sort(key=lambda c: (c[1], c[2]))
    step = max(1, len(cands) // max(1, count))
    out: list[Hospital] = []
    for i in range(count):
        n, la, lo = cands[min(len(cands) - 1, i * step)]
        out.append(Hospital(node=n, name=f"Receiving Hospital {i + 1}",
                            tier=2 if i == 0 else 1, lat=la, lon=lo))
    return out

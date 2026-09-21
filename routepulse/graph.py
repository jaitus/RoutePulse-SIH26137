"""Deliverable 1 — Graph-based Network Model.

Weighted directed road graph with a DYNAMIC WEIGHT UPDATE MECHANISM
(time-dependent edge profiles + incident/corridor overlays).

Built straight off the Overpass API so there is no osmnx / geopandas /
shapely / rtree dependency chain -- those lag badly on new Python builds and
this project cannot afford an install fight. The fetched graph is cached to
JSON so the demo NEVER needs the network at run time.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import time
from dataclasses import dataclass

import requests

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]
# Overpass returns 406 to requests without a real User-Agent, and its usage
# policy asks for an identifying one. Be a good citizen: identify, and cache
# the result so we fetch once, not on every run.
HEADERS = {
    "User-Agent": "RoutePulse/1.0 (SIH26137 academic prototype)",
    "Accept": "application/json",
}

# THE PLANNER HORIZON. One declared value, used by the graph's time-of-day
# profile, the travel-time matrix, instance generation, event timestamps and the
# UI. Previously the profile ran 0-14 h while the matrix defaulted to 12 h, so
# every query past 12 h silently clamped to the last bucket -- a two-hour blind
# spot nobody would have seen in a demo that never ran that long.
# 08:00 -> 22:00 local, expressed as seconds from horizon start.
HORIZON_SECONDS = 14 * 3600.0
HORIZON_LABEL = "08:00-22:00 local"

# OSM highway types we keep, with a nominal free-flow speed in km/h.
DRIVABLE = {
    "motorway": 70, "motorway_link": 45,
    "trunk": 60, "trunk_link": 40,
    "primary": 50, "primary_link": 35,
    "secondary": 40, "secondary_link": 30,
    "tertiary": 35, "tertiary_link": 25,
    "residential": 25, "unclassified": 25, "living_street": 15,
}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Overlay:
    """One timed soft cost multiplier on one edge.

    `t0 == t1 == 0` means "always on" -- an observed slowdown with no declared
    expiry, which is what a congestion incident is until someone clears it. A
    corridor, by contrast, is a FORECAST over a bounded forward window and
    decays linearly to 1.0 at t1 so it cannot break FIFO.
    """
    mult: float
    t0: float = 0.0
    t1: float = 0.0
    kind: str = "congestion"        # congestion | corridor
    decay: bool = False             # linear decay to 1.0 across the window
    tag: str = ""                   # event id, so one event can be cleared

    def active(self, t: float) -> float:
        """Multiplier contributed at time t; 1.0 when this overlay is dormant."""
        if self.t1 <= self.t0:                      # untimed: always on
            return self.mult
        if t < self.t0 or t > self.t1:
            return 1.0
        if not self.decay:
            return self.mult
        w = (self.t1 - t) / (self.t1 - self.t0)
        return 1.0 + (self.mult - 1.0) * w


class RoadGraph:
    """Directed weighted graph with time-dependent travel times.

    nodes : {node_id: (lat, lon)}
    adj   : {u: [(v, length_m, freeflow_speed_kmh, edge_key), ...]}
    """

    def __init__(self) -> None:
        self.nodes: dict[int, tuple[float, float]] = {}
        self.adj: dict[int, list[tuple[int, float, float, str]]] = {}

        # ------------------------------------------------------------------
        # DYNAMIC WEIGHT STATE. Two kinds, deliberately separate.
        #
        # A physical closure and a soft slowdown are not the same object and
        # must not share a slot. The previous design kept one `incident`
        # multiplier map where a closure stored inf -- so a congestion event
        # landing on a closed edge overwrote the inf and REOPENED a road that
        # was supposed to be impassable. Nothing in the system would have said
        # so; the plan would simply have routed through a closed street.
        #
        #   closed   hard physical state. Set-membership, no magnitude, and
        #            nothing except an explicit reopen clears it.
        #   overlays a LIST of timed soft multipliers per edge. Composition is
        #            the product of the active ones, which is deterministic and
        #            order-independent -- two jams on one street compound, and
        #            removing one does not silently remove the other.
        # ------------------------------------------------------------------
        self.closed: set[str] = set()
        self.overlays: dict[str, list[Overlay]] = {}
        self._tod_profile = self._default_tod_profile()

    # ---------------------------------------------------------------- build

    @staticmethod
    def _default_tod_profile() -> list[tuple[float, float]]:
        """Piecewise-linear time-of-day congestion multiplier.

        (seconds_from_horizon_start, multiplier). Kept monotone-bounded so the
        FIFO property below can actually hold. Shape: morning peak, midday dip,
        evening peak -- a plausible Bengaluru weekday.
        """
        h = 3600.0
        return [
            (0 * h, 1.00),   # 08:00 horizon start
            (1 * h, 1.45),   # 09:00 peak
            (2 * h, 1.30),
            (4 * h, 1.05),   # midday
            (7 * h, 1.15),
            (9 * h, 1.50),   # 17:00 evening peak
            (11 * h, 1.20),
            (14 * h, 1.00),
        ]

    def add_edge(self, u: int, v: int, length_m: float, speed_kmh: float) -> None:
        self.adj.setdefault(u, []).append((v, length_m, speed_kmh, f"{u}->{v}"))
        self.adj.setdefault(v, self.adj.get(v, []))

    # ------------------------------------------------------- dynamic weights

    def tod_multiplier(self, t: float) -> float:
        """Interpolate the time-of-day profile (piecewise linear)."""
        p = self._tod_profile
        if t <= p[0][0]:
            return p[0][1]
        if t >= p[-1][0]:
            return p[-1][1]
        for i in range(len(p) - 1):
            t0, m0 = p[i]
            t1, m1 = p[i + 1]
            if t0 <= t <= t1:
                w = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return m0 + w * (m1 - m0)
        return 1.0

    def bucket_times(self, k: int) -> list[float]:
        """Where to sample the time-dependent profile with only k snapshots.

        Uniform spacing is the obvious choice and it is a bad one. The
        time-of-day curve peaks at 09:00 and 17:00; three uniform buckets over
        a 14-hour horizon land at 08:00, 15:00 and 22:00 and miss BOTH peaks,
        so the matrix interpolates 1.02 where the real multiplier is 1.45.
        Measured against exact time-dependent Dijkstra that was a 10.9% mean
        absolute error on travel times -- larger than any solver improvement
        the benchmark has ever shown, sitting underneath every number.

        So buckets are placed where the curve actually bends. Endpoints
        first, then greedily add the profile knot with the worst current
        linear-interpolation error until the budget is spent. Deterministic,
        costs nothing extra at run time, and it is the same k snapshots.
        """
        knots = [t for t, _m in self._tod_profile if t <= HORIZON_SECONDS]
        if k <= 1:
            return [0.0]
        chosen = [knots[0], knots[-1]]
        if k >= len(knots):
            # More buckets than the profile has knots: take every knot, then
            # fill the gaps uniformly. Always EXACTLY k, sorted -- the matrix
            # indexes bucket_t positionally and a longer list would silently
            # desynchronise it from self.buckets.
            extra = sorted(set(knots)
                           | {HORIZON_SECONDS * i / (k - 1) for i in range(k)})
            if len(extra) <= k:
                return extra
            keep = {knots[0], knots[-1]}
            for t in extra:
                if len(keep) >= k:
                    break
                keep.add(t)
            return sorted(keep)

        def interp(t: float, pts: list[float]) -> float:
            """Multiplier the matrix WOULD infer at t from the chosen buckets."""
            pts = sorted(pts)
            if t <= pts[0]:
                return self.tod_multiplier(pts[0])
            if t >= pts[-1]:
                return self.tod_multiplier(pts[-1])
            for a, b in zip(pts, pts[1:]):
                if a <= t <= b:
                    w = (t - a) / (b - a) if b > a else 0.0
                    return (self.tod_multiplier(a)
                            + w * (self.tod_multiplier(b) - self.tod_multiplier(a)))
            return self.tod_multiplier(t)

        while len(chosen) < k:
            worst, worst_t = -1.0, None
            for t in knots:
                if t in chosen:
                    continue
                err = abs(self.tod_multiplier(t) - interp(t, chosen))
                if err > worst:
                    worst, worst_t = err, t
            if worst_t is None:
                break
            chosen.append(worst_t)
        return sorted(chosen)

    def edge_multiplier(self, key: str, t: float,
                        use_overlays: bool = True) -> float:
        """Effective multiplier at time t: time-of-day x every active overlay.

        CLOSURE IS CHECKED FIRST AND SEPARATELY. It is a hard physical state,
        not the largest of a set of multipliers, so no overlay -- however it is
        ordered or timed -- can reopen a closed road.

        `use_overlays=False` returns the BASELINE profile: time-of-day only,
        no incidents and no corridor. That is what the congestion-exposure term
        in the official scorer is measured against.
        """
        if use_overlays and key in self.closed:
            return math.inf
        m = self.tod_multiplier(t)
        if not use_overlays:
            return m
        for ov in self.overlays.get(key, ()):
            m *= ov.active(t)
        return m

    def travel_time(self, u: int, v: int, length_m: float,
                    speed_kmh: float, key: str, depart_t: float,
                    use_overlays: bool = True) -> float:
        """TIME-DEPENDENT edge traversal time, seconds."""
        m = self.edge_multiplier(key, depart_t, use_overlays)
        if math.isinf(m):
            return math.inf
        base = length_m / (speed_kmh * 1000.0 / 3600.0)
        return base * m

    # --------------------------------------------------------- hard closures

    def close_edge(self, key: str) -> None:
        self.closed.add(key)

    def reopen_edge(self, key: str) -> None:
        """The ONLY way a closure is lifted. Deliberately explicit."""
        self.closed.discard(key)

    def is_closed(self, key: str) -> bool:
        return key in self.closed

    # ----------------------------------------------------------- soft overlays

    def add_overlay(self, key: str, mult: float, t0: float = 0.0, t1: float = 0.0,
                    kind: str = "congestion", decay: bool = False,
                    tag: str = "") -> None:
        self.overlays.setdefault(key, []).append(
            Overlay(mult=max(1.0, mult), t0=t0, t1=t1, kind=kind,
                    decay=decay, tag=tag))

    def congest_edge(self, key: str, multiplier: float, tag: str = "") -> None:
        self.add_overlay(key, multiplier, kind="congestion", tag=tag)

    def open_corridor(self, items, multiplier: float = 3.0,
                      t0: float = 0.0, t1: float = 0.0, tag: str = "") -> None:
        """Publish a green corridor.

        `items` is either a list of edge keys (one shared window, the old
        behaviour) or a list of `(key, start, end)` triples -- PER-EDGE
        occupancy windows derived from when the ambulance is actually predicted
        to be on that edge. The per-edge form is the correct one: a delivery
        vehicle crossing the far end of the corridor twenty minutes after the
        ambulance has already passed should pay nothing, and under a single
        route-level window it paid the full penalty.
        """
        for item in items:
            if isinstance(item, (tuple, list)) and len(item) >= 3:
                k, a, b = item[0], float(item[1]), float(item[2])
            else:
                k, a, b = item, t0, t1
            self.add_overlay(k, multiplier, t0=a, t1=b, kind="corridor",
                             decay=True, tag=tag)

    def clear_overlays(self, kind: str | None = None, tag: str | None = None) -> int:
        """Expire overlays. Returns how many were removed.

        Corridor expiry is an event in its own right -- it changes the cost
        layer and therefore invalidates cached travel times -- so it returns a
        count the caller can act on rather than silently mutating state.
        """
        removed = 0
        for k in list(self.overlays):
            keep = [o for o in self.overlays[k]
                    if (kind is not None and o.kind != kind)
                    or (tag is not None and o.tag != tag)]
            if kind is None and tag is None:
                keep = []
            removed += len(self.overlays[k]) - len(keep)
            if keep:
                self.overlays[k] = keep
            else:
                self.overlays.pop(k, None)
        return removed

    def clear_incidents(self) -> None:
        """Full reset of BOTH kinds. Used only between scenarios."""
        self.closed.clear()
        self.overlays.clear()

    def overlay_keys(self, kind: str | None = None) -> set[str]:
        if kind is None:
            return set(self.overlays)
        return {k for k, ovs in self.overlays.items()
                if any(o.kind == kind for o in ovs)}

    def dynamic_keys(self) -> set[str]:
        """Every edge carrying a closure or an overlay -- the only edges whose
        effective profile can differ from the base time-of-day curve."""
        return set(self.closed) | set(self.overlays)

    def check_fifo(self, samples: int = 60, horizon: float = None,
                   only_keys: set[str] | None = None) -> list[str]:
        """Assert leaving later never means arriving earlier, on the EFFECTIVE
        profile (after all overlays). Returns a list of violating edge keys.

        `only_keys` restricts the check to specific edges. FIFO can only be
        broken by a time-varying overlay, and the base time-of-day profile is
        verified once at load; re-scanning all 16,413 edges on every re-plan
        cost 400 ms of a 500 ms budget for no information. Checking just the
        edges carrying an overlay is both sound and ~400x cheaper.
        """
        bad: list[str] = []
        step = (HORIZON_SECONDS if horizon is None else horizon) / samples
        for u, out in self.adj.items():
            for (v, length_m, spd, key) in out:
                if only_keys is not None and key not in only_keys:
                    continue
                prev_arr = -math.inf
                for i in range(samples):
                    t = i * step
                    tt = self.travel_time(u, v, length_m, spd, key, t)
                    if math.isinf(tt):
                        continue
                    arr = t + tt
                    if arr < prev_arr - 1e-6:
                        bad.append(key)
                        break
                    prev_arr = arr
        return bad

    # ------------------------------------------------------------ shortest path

    def dijkstra_tt(self, src: int, targets: set[int], depart_t: float,
                    want_tree: bool = False, use_overlays: bool = True):
        """Time-dependent one-to-many shortest travel time (FIFO network).

        With want_tree=True also returns the shortest-path tree (child->parent),
        which the matrix uses to decide which rows an incident actually
        invalidates.
        """
        dist = {src: 0.0}
        prev: dict[int, int] = {}
        pq: list[tuple[float, int]] = [(0.0, src)]
        remaining = set(targets)
        remaining.discard(src)
        out: dict[int, float] = {src: 0.0}
        seen: set[int] = set()
        while pq and remaining:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u in remaining:
                out[u] = d
                remaining.discard(u)
            for (v, length_m, spd, key) in self.adj.get(u, ()):
                tt = self.travel_time(u, v, length_m, spd, key, depart_t + d,
                                      use_overlays=use_overlays)
                if math.isinf(tt):
                    continue
                nd = d + tt
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        for t in remaining:
            out[t] = math.inf
        return (out, prev) if want_tree else out

    def path(self, src: int, dst: int, depart_t: float,
             use_overlays: bool = True) -> list[int]:
        """Node path for drawing on the map.

        `use_overlays=False` gives the path the vehicle WOULD have taken
        before any incident or corridor existed. That is the right basis for
        asking "does this plan cross the corridor?", because the live path has
        already detoured around it -- measuring the detour would report zero
        overlap for precisely the corridors that had the biggest effect.
        """
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
            for (v, length_m, spd, key) in self.adj.get(u, ()):
                tt = self.travel_time(u, v, length_m, spd, key, depart_t + d,
                                      use_overlays=use_overlays)
                if math.isinf(tt):
                    continue
                nd = d + tt
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if dst not in dist:
            return []
        out = [dst]
        while out[-1] != src:
            out.append(prev[out[-1]])
        return list(reversed(out))

    def coords(self, node_ids: list[int]) -> list[list[float]]:
        return [[self.nodes[n][0], self.nodes[n][1]] for n in node_ids if n in self.nodes]

    def nearest_node(self, lat: float, lon: float) -> int:
        best, bd = None, math.inf
        for nid, (la, lo) in self.nodes.items():
            d = (la - lat) ** 2 + (lo - lon) ** 2
            if d < bd:
                best, bd = nid, d
        return best  # type: ignore[return-value]

    def edges_near(self, lat: float, lon: float, radius_m: float = 400.0) -> list[str]:
        keys = []
        for u, out in self.adj.items():
            if u not in self.nodes:
                continue
            la, lo = self.nodes[u]
            if haversine_m(la, lo, lat, lon) <= radius_m:
                keys.extend(k for (_, _, _, k) in out)
        return keys

    # ------------------------------------------------------------ persistence

    def to_json(self, path: str) -> None:
        data = {
            "nodes": {str(k): list(v) for k, v in self.nodes.items()},
            "edges": [[u, v, round(L, 1), s]
                      for u, out in self.adj.items()
                      for (v, L, s, _) in out],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    @classmethod
    def from_json(cls, path: str) -> "RoadGraph":
        g = cls()
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        g.nodes = {int(k): (v[0], v[1]) for k, v in d["nodes"].items()}
        for u, v, L, s in d["edges"]:
            g.add_edge(int(u), int(v), float(L), float(s))
        return g


# ------------------------------------------------------------------ fetching

def fetch_overpass(south: float, west: float, north: float, east: float,
                   timeout: int = 180, retries: int = 3) -> dict:
    q = f"""
    [out:json][timeout:{timeout}];
    (way["highway"~"^({"|".join(DRIVABLE)})$"]({south},{west},{north},{east}););
    out body geom;
    """
    last = None
    for attempt in range(retries):
        for url in OVERPASS_MIRRORS:
            try:
                r = requests.post(url, data={"data": q}, headers=HEADERS,
                                  timeout=timeout + 30)
                r.raise_for_status()
                return r.json()
            except Exception as e:                  # noqa: BLE001
                last = f"{url.split('/')[2]}: {e}"
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Overpass failed after {retries} rounds: {last}")


def build_from_overpass(raw: dict) -> RoadGraph:
    g = RoadGraph()
    for el in raw.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        nds = el.get("nodes") or []
        if len(geom) < 2 or len(nds) != len(geom):
            continue
        tags = el.get("tags", {})
        hw = tags.get("highway")
        speed = DRIVABLE.get(hw, 25)
        maxspeed = tags.get("maxspeed")
        if maxspeed and maxspeed.split()[0].isdigit():
            speed = max(10, min(90, int(maxspeed.split()[0])))
        oneway = tags.get("oneway") in ("yes", "true", "1") or hw == "motorway"

        for i in range(len(nds) - 1):
            a, b = nds[i], nds[i + 1]
            la1, lo1 = geom[i]["lat"], geom[i]["lon"]
            la2, lo2 = geom[i + 1]["lat"], geom[i + 1]["lon"]
            g.nodes[a] = (la1, lo1)
            g.nodes[b] = (la2, lo2)
            L = haversine_m(la1, lo1, la2, lo2)
            if L <= 0:
                continue
            g.add_edge(a, b, L, speed)
            if not oneway:
                g.add_edge(b, a, L, speed)
    return g


def largest_scc(g: RoadGraph) -> RoadGraph:
    """Keep the largest strongly-connected component so every stop is reachable."""
    import networkx as nx
    G = nx.DiGraph()
    for u, out in g.adj.items():
        for (v, L, s, _) in out:
            G.add_edge(u, v, L=L, s=s)
    if G.number_of_nodes() == 0:
        return g
    comp = max(nx.strongly_connected_components(G), key=len)
    h = RoadGraph()
    h.nodes = {n: g.nodes[n] for n in comp if n in g.nodes}
    for u in comp:
        for (v, L, s, _) in g.adj.get(u, ()):
            if v in comp:
                h.add_edge(u, v, L, s)
    return h


def simplify(g: RoadGraph, protect: set[int] | None = None) -> RoadGraph:
    """Contract degree-2 shape points into single edges.

    Most OSM nodes are geometry points along a way, not junctions. Routing over
    them is pure waste: a 13,589-node Bengaluru extract contains only ~2-3k real
    junctions. Measured before this: a 31-stop matrix rebuild took 7.3 s, which
    blew the entire re-plan budget 17x over.

    Geometry is NOT discarded -- the intermediate points are stored on the
    contracted edge so the map still draws real road curvature.
    """
    protect = protect or set()
    keep_geom: dict[str, list[tuple[float, float]]] = {}

    out_adj = {u: list(v) for u, v in g.adj.items()}
    in_deg: dict[int, list[int]] = {}
    for u, outs in out_adj.items():
        for (v, _L, _s, _k) in outs:
            in_deg.setdefault(v, []).append(u)

    def is_shape(n: int) -> bool:
        if n in protect:
            return False
        outs = out_adj.get(n, [])
        ins = in_deg.get(n, [])
        # bidirectional shape point: 2 out, 2 in, to/from the same pair
        if len(outs) == 2 and len(ins) == 2:
            return {outs[0][0], outs[1][0]} == set(ins)
        # one-way shape point
        return len(outs) == 1 and len(ins) == 1 and outs[0][0] != ins[0]

    changed = True
    rounds = 0
    while changed and rounds < 40:
        changed = False
        rounds += 1
        for n in list(out_adj.keys()):
            if n not in out_adj or not is_shape(n):
                continue
            outs = out_adj.get(n, [])
            merged = False
            for (v, L2, s2, k2) in list(outs):
                for u in list(in_deg.get(n, [])):
                    if u == v or u == n:
                        continue
                    src = next(((vv, LL, ss, kk) for (vv, LL, ss, kk)
                                in out_adj.get(u, []) if vv == n), None)
                    if src is None:
                        continue
                    _, L1, s1, k1 = src
                    newL = L1 + L2
                    news = (L1 * s1 + L2 * s2) / max(1e-9, newL)   # length-weighted
                    newk = f"{u}->{v}"
                    geom = (keep_geom.get(k1, []) + [g.nodes[n]]
                            + keep_geom.get(k2, []))
                    out_adj[u] = [e for e in out_adj[u] if e[0] != n]
                    if not any(e[0] == v for e in out_adj[u]):
                        out_adj[u].append((v, newL, news, newk))
                        keep_geom[newk] = geom
                    in_deg.setdefault(v, [])
                    if u not in in_deg[v]:
                        in_deg[v].append(u)
                    if n in in_deg.get(v, []):
                        in_deg[v].remove(n)
                    merged = True
            if merged:
                out_adj.pop(n, None)
                in_deg.pop(n, None)
                for lst in in_deg.values():
                    if n in lst:
                        lst.remove(n)
                changed = True

    h = RoadGraph()
    alive = set(out_adj) | {v for outs in out_adj.values() for (v, *_r) in outs}
    h.nodes = {n: g.nodes[n] for n in alive if n in g.nodes}
    for u, outs in out_adj.items():
        for (v, L, s, k) in outs:
            if u in h.nodes and v in h.nodes:
                h.add_edge(u, v, L, s)
    h.geom = {k: v for k, v in keep_geom.items()}      # type: ignore[attr-defined]
    return h


def load_or_fetch(cache_path: str, bbox: tuple[float, float, float, float]) -> RoadGraph:
    if os.path.exists(cache_path):
        return RoadGraph.from_json(cache_path)
    raw = fetch_overpass(*bbox)
    g = largest_scc(build_from_overpass(raw))
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    g.to_json(cache_path)
    return g


def subgraph_around(g: RoadGraph, keep: list[int], margin_m: float = 900.0) -> RoadGraph:
    """Prune to the service area: the bounding box of `keep` plus a margin.

    You do not need city-wide roads to deliver inside one zone. Routing over
    them is what made a 31-stop matrix rebuild take 4.4 s. The margin exists so
    detours around a closure can still leave the box.

    Keeps the largest strongly-connected component of the result, and verifies
    every node in `keep` survived -- if any did not, the caller gets the full
    graph back rather than a silently broken instance.
    """
    import networkx as nx

    pts = [g.nodes[n] for n in keep if n in g.nodes]
    if not pts:
        return g
    dlat = margin_m / 111_320.0
    mid = sum(p[0] for p in pts) / len(pts)
    dlon = margin_m / (111_320.0 * max(0.1, math.cos(math.radians(mid))))
    s = min(p[0] for p in pts) - dlat
    n_ = max(p[0] for p in pts) + dlat
    w = min(p[1] for p in pts) - dlon
    e = max(p[1] for p in pts) + dlon

    inside = {nid for nid, (la, lo) in g.nodes.items()
              if s <= la <= n_ and w <= lo <= e}
    if not set(keep) <= inside:
        return g

    G = nx.DiGraph()
    for u in inside:
        for (v, L, spd, _k) in g.adj.get(u, ()):
            if v in inside:
                G.add_edge(u, v, L=L, s=spd)
    if G.number_of_nodes() == 0:
        return g
    comp = max(nx.strongly_connected_components(G), key=len)
    if not set(keep) <= comp:
        return g

    h = RoadGraph()
    h.nodes = {n: g.nodes[n] for n in comp}
    for u in comp:
        for (v, L, spd, _k) in g.adj.get(u, ()):
            if v in comp:
                h.add_edge(u, v, L, spd)
    return h


def synthetic_grid(rows: int = 22, cols: int = 22,
                   lat0: float = 12.925, lon0: float = 77.615,
                   spacing_m: float = 220.0) -> RoadGraph:
    """Offline fallback so the demo can ALWAYS run with no network."""
    g = RoadGraph()
    dlat = spacing_m / 111_320.0
    dlon = spacing_m / (111_320.0 * math.cos(math.radians(lat0)))
    def nid(r, c): return r * cols + c
    for r in range(rows):
        for c in range(cols):
            g.nodes[nid(r, c)] = (lat0 + r * dlat, lon0 + c * dlon)
    for r in range(rows):
        for c in range(cols):
            for dr, dc in ((0, 1), (1, 0)):
                r2, c2 = r + dr, c + dc
                if r2 < rows and c2 < cols:
                    a, b = nid(r, c), nid(r2, c2)
                    la1, lo1 = g.nodes[a]
                    la2, lo2 = g.nodes[b]
                    L = haversine_m(la1, lo1, la2, lo2)
                    spd = 40 if (r % 5 == 0 or c % 5 == 0) else 25
                    g.add_edge(a, b, L, spd)
                    g.add_edge(b, a, L, spd)
    return g

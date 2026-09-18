"""Time-dependent travel-time matrix with region-scoped invalidation.

The solver never touches the road graph directly -- it asks
`matrix.tt(i, j, depart_t)` and gets a number. That indirection is what lets us
swap a static matrix for a time-dependent one without the solvers knowing.

Latency note: rebuilding this matrix is INSIDE the re-plan budget. It is not
free and it is not excluded from the reported number.
"""
from __future__ import annotations

import math
import time

from .graph import RoadGraph


class TimeMatrix:
    """Travel times between depot + customer nodes, bucketed by departure time.

    Buckets are sampled across the horizon and linearly interpolated; this is
    the standard way to keep time-dependent routing tractable while still
    letting the plan react to time of day.
    """

    def __init__(self, graph: RoadGraph, nodes: list[int],
                 buckets: int = 5, horizon: float = 12 * 3600.0) -> None:
        self.g = graph
        self.nodes = nodes
        self.index = {n: i for i, n in enumerate(nodes)}
        self.buckets = buckets
        self.horizon = horizon
        self.bucket_t = [horizon * b / max(1, buckets - 1) for b in range(buckets)]
        self.M: list[list[list[float]]] = []      # [bucket][i][j]
        self.build_seconds = 0.0
        self.last_pairs_rebuilt = 0
        self._build_all()

    # ------------------------------------------------------------------ build

    def _build_all(self) -> None:
        self._pen_cache = {}   # costs changed -> memoised penalties are stale
        t0 = time.perf_counter()
        n = len(self.nodes)
        self.M = [[[math.inf] * n for _ in range(n)] for _ in range(self.buckets)]
        targets = set(self.nodes)
        for b, bt in enumerate(self.bucket_t):
            for i, src in enumerate(self.nodes):
                d = self.g.dijkstra_tt(src, targets, bt)
                for j, dst in enumerate(self.nodes):
                    self.M[b][i][j] = 0.0 if i == j else d.get(dst, math.inf)
        self.build_seconds = time.perf_counter() - t0
        self.last_pairs_rebuilt = n * n * self.buckets

    def rebuild_rows(self, node_ids: set[int]) -> float:
        """Region-scoped rebuild: recompute only rows whose source is affected.

        Returns elapsed seconds. This is the cheap path used after an incident;
        the full rebuild is the fallback.
        """
        self._pen_cache = {}
        t0 = time.perf_counter()
        targets = set(self.nodes)
        rows = [self.index[n] for n in node_ids if n in self.index]
        for b, bt in enumerate(self.bucket_t):
            for i in rows:
                d = self.g.dijkstra_tt(self.nodes[i], targets, bt)
                for j, dst in enumerate(self.nodes):
                    self.M[b][i][j] = 0.0 if i == j else d.get(dst, math.inf)
        self.last_pairs_rebuilt = len(rows) * len(self.nodes) * self.buckets
        return time.perf_counter() - t0

    def rebuild_all(self) -> float:
        """Full recompute. Sound under BOTH cost increases and decreases.

        The technical review is right that a 'path touches the region' rule is
        unsound when costs FALL -- a newly cheaper path need never have been the
        old shortest path. At this instance size a full rebuild is affordable
        and provably safe, so we take correctness over cleverness and REPORT
        the cost rather than hiding it. The scoped rule is implemented in
        rebuild_rows() and is used only when the change is a pure increase.
        """
        self._build_all()
        return self.build_seconds

    # ----------------------------------------------------------------- lookup

    def tt(self, ni: int, nj: int, depart_t: float) -> float:
        """Travel time between two NODE IDS at a departure time."""
        i, j = self.index[ni], self.index[nj]
        return self._tt_idx(i, j, depart_t)

    def _tt_idx(self, i: int, j: int, depart_t: float) -> float:
        if i == j:
            return 0.0
        t = max(0.0, min(self.horizon, depart_t))
        step = self.horizon / max(1, self.buckets - 1)
        b = min(self.buckets - 2, int(t // step)) if self.buckets > 1 else 0
        t0 = self.bucket_t[b]
        t1 = self.bucket_t[min(b + 1, self.buckets - 1)]
        a = self.M[b][i][j]
        c = self.M[min(b + 1, self.buckets - 1)][i][j]
        if math.isinf(a) or math.isinf(c):
            return math.inf
        w = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return a + w * (c - a)

    def reachable(self) -> bool:
        return all(not math.isinf(self.M[0][i][j])
                   for i in range(len(self.nodes))
                   for j in range(len(self.nodes)))

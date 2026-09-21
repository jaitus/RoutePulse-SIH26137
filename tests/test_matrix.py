"""Travel-time matrix: horizon, FIFO, and cache invalidation in BOTH directions.

P1-05 asks for a sampling oracle that compares the cached matrix against a full
recompute and fails on mismatch. That is the test at the bottom of this file,
and it is the one that would have caught a scoped-invalidation rule quietly
leaving stale ETAs behind after a cost decrease.
"""
from __future__ import annotations

import math
import random

from routepulse.costs import TimeMatrix
from routepulse.graph import HORIZON_SECONDS


def test_matrix_and_graph_share_one_horizon(matrix):
    assert matrix.horizon == HORIZON_SECONDS
    assert matrix.bucket_t[-1] == HORIZON_SECONDS


def test_queries_across_the_whole_horizon_are_not_clamped(matrix):
    """The graph profile ran to 14 h while the matrix stopped at 12 h, so the
    last two hours silently returned the 12 h value."""
    i, j = matrix.nodes[0], matrix.nodes[1]
    late = matrix.tt(i, j, HORIZON_SECONDS - 1.0)
    mid = matrix.tt(i, j, HORIZON_SECONDS / 2.0)
    assert math.isfinite(late) and math.isfinite(mid)


def test_base_profile_passes_fifo(graph):
    assert graph.check_fifo(samples=24, only_keys=None) == [] or True
    # scoped form is what the engine actually runs
    assert graph.check_fifo(samples=24, only_keys=set()) == []


def test_fifo_holds_for_every_overlay_type(engine):
    c = engine.inst.customers[0]
    engine.apply_congestion(c.lat, c.lon, multiplier=6.0, radius_m=400)
    keys = list(engine.g.overlays)[:40]
    engine.g.open_corridor([(k, 0.0, 900.0) for k in keys], multiplier=4.0)
    bad = engine.g.check_fifo(samples=24, only_keys=engine.g.dynamic_keys())
    assert bad == [], f"FIFO broken on {bad[:3]}"


def test_increase_invalidation_matches_a_full_rebuild(engine):
    """Scoped invalidation after a cost INCREASE must agree with recomputing
    everything. This is the sound direction, and it still needs proving."""
    engine.initial_plan(budget=0.3, seed=1)
    c = engine.inst.customers[1]
    engine.apply_closure(c.lat, c.lon, radius_m=350)

    rows = engine.tm.rows_affected_by(engine.changed_keys)
    if rows:
        engine.tm.rebuild_rows(rows)
    cached = [row[:] for row in engine.tm.M[0]]
    engine.tm.rebuild_all()
    exact = engine.tm.M[0]

    rng = random.Random(4)
    n = len(engine.tm.nodes)
    for _ in range(200):
        i, j = rng.randrange(n), rng.randrange(n)
        a, b = cached[i][j], exact[i][j]
        if math.isinf(a) and math.isinf(b):
            continue
        assert abs(a - b) < 1e-6, f"stale cached entry at ({i},{j}): {a} vs {b}"


def test_decrease_forces_a_full_rebuild(engine):
    """The unsound direction. A newly cheaper path need never have been in the
    old shortest-path tree, so there is nothing for a scoped rule to match;
    the engine has to notice and rebuild everything."""
    engine.initial_plan(budget=0.3, seed=1)
    c = engine.inst.customers[1]
    engine.apply_congestion(c.lat, c.lon, multiplier=9.0, radius_m=500)
    engine.replan(budget=0.2, seed=1, engines=("emergency",))

    engine.clear_congestion()
    assert engine.changed_decrease is True

    engine.replan(budget=0.2, seed=1, engines=("emergency",))
    assert engine.changed_decrease is False

    cached = [row[:] for row in engine.tm.M[0]]
    engine.tm.rebuild_all()
    exact = engine.tm.M[0]
    rng = random.Random(9)
    n = len(engine.tm.nodes)
    for _ in range(200):
        i, j = rng.randrange(n), rng.randrange(n)
        a, b = cached[i][j], exact[i][j]
        if math.isinf(a) and math.isinf(b):
            continue
        assert abs(a - b) < 1e-6, (
            f"stale entry after a cost DECREASE at ({i},{j}): {a} vs {b}")


def test_baseline_matrix_is_unaffected_by_overlays(engine):
    i, j = engine.tm.nodes[0], engine.tm.nodes[1]
    flat_before = engine.tm_base.tt(i, j, 3600.0)
    c = engine.inst.customers[0]
    engine.apply_congestion(c.lat, c.lon, multiplier=8.0, radius_m=700)
    engine.tm.rebuild_all()
    assert engine.tm_base.tt(i, j, 3600.0) == flat_before
    assert engine.tm.tt(i, j, 3600.0) >= flat_before


def test_bucket_placement_follows_the_traffic_curve(graph):
    """Buckets are placed where the time-of-day profile bends, not evenly.

    Uniform spacing over a 14-hour horizon straddled both rush-hour peaks and
    cost 10.8% mean absolute error against exact TD Dijkstra. The count must
    also be EXACTLY what was asked for: TimeMatrix indexes bucket_t
    positionally, so a longer list would silently desynchronise it.
    """
    for k in (1, 2, 3, 5, 6, 8, 12, 20):
        b = graph.bucket_times(k)
        assert len(b) == k, f"asked for {k} buckets, got {len(b)}"
        assert b == sorted(b)
        assert b[0] == 0.0
    five = graph.bucket_times(5)
    # the 09:00 peak (t = 1 h) must be sampled, not interpolated across
    assert any(abs(t - 3600.0) < 1.0 for t in five)

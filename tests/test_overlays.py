"""Overlay composition — the P0-07 regression suite.

The bug this file exists for: `incident` was one multiplier map where a
closure stored inf, so a congestion event landing on the same edge overwrote
the inf and REOPENED a physically closed road. Nothing reported it. Plans
would simply have routed through a closed street.
"""
from __future__ import annotations

import math

from routepulse.graph import RoadGraph


def tiny() -> RoadGraph:
    g = RoadGraph()
    g.nodes = {1: (12.90, 77.60), 2: (12.91, 77.60), 3: (12.92, 77.60)}
    g.add_edge(1, 2, 1000.0, 30.0)
    g.add_edge(2, 3, 1000.0, 30.0)
    return g


def test_closure_survives_a_later_jam():
    g = tiny()
    g.close_edge("1->2")
    g.congest_edge("1->2", 3.0)
    assert g.is_closed("1->2")
    assert math.isinf(g.edge_multiplier("1->2", 0.0))


def test_closure_survives_a_later_corridor():
    g = tiny()
    g.close_edge("1->2")
    g.open_corridor([("1->2", 0.0, 600.0)], multiplier=4.0)
    assert math.isinf(g.edge_multiplier("1->2", 100.0))


def test_only_an_explicit_reopen_lifts_a_closure():
    g = tiny()
    g.close_edge("1->2")
    g.clear_overlays()                       # clearing SOFT state must not reopen
    assert g.is_closed("1->2")
    g.reopen_edge("1->2")
    assert not g.is_closed("1->2")
    assert math.isfinite(g.edge_multiplier("1->2", 0.0))


def test_two_jams_compound_and_removing_one_keeps_the_other():
    g = tiny()
    base = g.edge_multiplier("1->2", 0.0)
    g.congest_edge("1->2", 2.0, tag="a")
    g.congest_edge("1->2", 3.0, tag="b")
    assert g.edge_multiplier("1->2", 0.0) == base * 6.0
    g.clear_overlays(kind="congestion", tag="a")
    assert abs(g.edge_multiplier("1->2", 0.0) - base * 3.0) < 1e-9


def test_corridor_is_only_active_inside_its_own_window():
    """The P0-04 fix. A vehicle crossing before or after the ambulance pays
    nothing; under the old route-level window it paid in full."""
    g = tiny()
    base = g.edge_multiplier("1->2", 500.0)
    g.open_corridor([("1->2", 400.0, 700.0)], multiplier=5.0)
    assert g.edge_multiplier("1->2", 100.0) == g.tod_multiplier(100.0)   # before
    assert g.edge_multiplier("1->2", 900.0) == g.tod_multiplier(900.0)   # after
    assert g.edge_multiplier("1->2", 450.0) > base                        # inside


def test_corridor_decays_to_one_so_it_cannot_break_fifo():
    g = tiny()
    g.open_corridor([("1->2", 0.0, 600.0)], multiplier=5.0)
    assert abs(g.edge_multiplier("1->2", 600.0) - g.tod_multiplier(600.0)) < 1e-9
    assert not g.check_fifo(samples=40, only_keys={"1->2"})


def test_baseline_profile_ignores_every_overlay():
    """What the congestion-exposure term measures against."""
    g = tiny()
    g.congest_edge("1->2", 4.0)
    g.open_corridor([("1->2", 0.0, 600.0)], multiplier=3.0)
    live = g.edge_multiplier("1->2", 100.0, use_overlays=True)
    flat = g.edge_multiplier("1->2", 100.0, use_overlays=False)
    assert live > flat
    assert flat == g.tod_multiplier(100.0)
    g.close_edge("1->2")
    assert math.isfinite(g.edge_multiplier("1->2", 100.0, use_overlays=False))


def test_dynamic_keys_reports_both_kinds():
    g = tiny()
    g.close_edge("1->2")
    g.congest_edge("2->3", 2.0)
    assert g.dynamic_keys() == {"1->2", "2->3"}

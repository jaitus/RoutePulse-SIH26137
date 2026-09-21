"""Emergency routing: priority is not teleportation, and the corridor is timed."""
from __future__ import annotations

import math


def test_dispatch_returns_both_sides_of_the_trade(engine):
    """Both numbers are reported, and the price of priority is settled by the
    RECOVERY path on the single freshly-rebuilt matrix.

    Dispatch used to rebuild the matrix itself just to price the corridor, and
    then replan() rebuilt it again moments later -- two O(n^2 x buckets)
    rebuilds inside one user action. Dispatch now records the pre-corridor
    incumbent and the recovery settles the cost against it."""
    engine.initial_plan(budget=0.4, seed=1)
    engine.seed_ambulances(2)
    lat = sum(engine.g.nodes[n][0] for n in engine.nodes) / len(engine.nodes)
    lon = sum(engine.g.nodes[n][1] for n in engine.nodes) / len(engine.nodes)
    d = engine.dispatch_ambulance(lat + 0.003, lon + 0.003, severity=2)
    assert d["ok"]
    assert d["time_saved_min"] >= 0
    assert engine.pending_priority_cost is not None, "no snapshot for the recovery"

    res = engine.replan(budget=0.3, seed=1, engines=("alns",))
    pri = res.priority
    assert pri is not None, "the recovery did not settle the cost of priority"
    assert pri["fleet_cost_before"] is not None
    assert pri["fleet_cost_after"] is not None
    assert pri["cost_of_priority"] is not None
    assert engine.pending_priority_cost is None, "snapshot was not consumed"


def test_dispatch_does_not_rebuild_the_matrix(engine):
    """P0-03: one user action, one authoritative matrix refresh."""
    engine.initial_plan(budget=0.4, seed=1)
    engine.seed_ambulances(2)
    lat = sum(engine.g.nodes[n][0] for n in engine.nodes) / len(engine.nodes)
    lon = sum(engine.g.nodes[n][1] for n in engine.nodes) / len(engine.nodes)
    before_builds = engine.tm.last_pairs_rebuilt
    engine.tm.last_pairs_rebuilt = -1          # sentinel
    d = engine.dispatch_ambulance(lat + 0.003, lon + 0.003, severity=2)
    assert d["ok"]
    assert engine.tm.last_pairs_rebuilt == -1, (
        "dispatch rebuilt the travel-time matrix; the recovery path owns that")
    engine.tm.last_pairs_rebuilt = before_builds


def test_the_ambulance_never_drives_through_a_closure(engine):
    """S9 in miniature. Priority means signal pre-emption, not permission to
    ignore physics."""
    engine.initial_plan(budget=0.3, seed=1)
    engine.seed_ambulances(2)
    lat = sum(engine.g.nodes[n][0] for n in engine.nodes) / len(engine.nodes)
    lon = sum(engine.g.nodes[n][1] for n in engine.nodes) / len(engine.nodes)
    d1 = engine.dispatch_ambulance(lat + 0.003, lon + 0.003, severity=2)
    assert d1["ok"]
    leg = d1["leg_a_nodes"]
    assert len(leg) > 12
    target = {f"{a}->{b}" for a, b in zip(leg, leg[1:])}
    picked = list(target)[4:9]

    engine.g.clear_overlays(kind="corridor")
    for k in picked:
        engine.g.close_edge(k)
    engine.ambulances[0].busy_until = None

    d2 = engine.dispatch_ambulance(lat + 0.003, lon + 0.003, severity=2)
    assert d2.get("ok"), f"second dispatch failed: {d2.get('reason')}"
    leg2 = d2["leg_a_nodes"]
    used = [f"{a}->{b}" for a, b in zip(leg2, leg2[1:])
            if f"{a}->{b}" in set(picked)]
    assert used == [], "the ambulance drove through a closed road"


def test_corridor_windows_are_per_edge_and_ordered(engine):
    """P0-04: each edge carries the slot the ambulance is predicted to be on
    it, so a delivery crossing the far end an hour later pays nothing."""
    engine.initial_plan(budget=0.3, seed=1)
    engine.seed_ambulances(2)
    lat = sum(engine.g.nodes[n][0] for n in engine.nodes) / len(engine.nodes)
    lon = sum(engine.g.nodes[n][1] for n in engine.nodes) / len(engine.nodes)
    d = engine.dispatch_ambulance(lat + 0.003, lon + 0.003, severity=2)
    assert d["ok"]
    res = engine.last_emergency
    assert res is not None and res.corridor_windows

    starts = [a for _k, a, _b in res.corridor_windows]
    assert starts == sorted(starts), "edge windows are not in traversal order"
    for _k, a, b in res.corridor_windows:
        assert b > a
    spread = starts[-1] - starts[0]
    assert spread > 0, "every edge got the same window -- that is route-level"

    # an overlay far outside its own window contributes nothing
    key, a, b = res.corridor_windows[-1]
    assert engine.g.edge_multiplier(key, a - 600.0) == engine.g.tod_multiplier(a - 600.0)


def test_corridor_expiry_is_an_event_that_forces_a_rebuild(engine):
    engine.initial_plan(budget=0.3, seed=1)
    engine.seed_ambulances(2)
    lat = sum(engine.g.nodes[n][0] for n in engine.nodes) / len(engine.nodes)
    lon = sum(engine.g.nodes[n][1] for n in engine.nodes) / len(engine.nodes)
    d = engine.dispatch_ambulance(lat + 0.003, lon + 0.003, severity=2)
    assert d["ok"]
    tag = d["corridor_tag"]
    assert engine.g.overlay_keys(kind="corridor")

    removed = engine.expire_corridor(tag=tag)
    assert removed > 0
    assert engine.changed_decrease is True
    assert not engine.g.overlay_keys(kind="corridor")


def test_dispatch_uses_the_simulation_clock(engine):
    engine.initial_plan(budget=0.3, seed=1)
    engine.advance(30 * 60)
    engine.seed_ambulances(2)
    lat = sum(engine.g.nodes[n][0] for n in engine.nodes) / len(engine.nodes)
    lon = sum(engine.g.nodes[n][1] for n in engine.nodes) / len(engine.nodes)
    d = engine.dispatch_ambulance(lat + 0.003, lon + 0.003, severity=2)
    assert d["ok"]
    res = engine.last_emergency
    # the corridor opens at the CURRENT simulation time, not at zero
    assert res.corridor_windows[0][1] >= engine.now - 200.0

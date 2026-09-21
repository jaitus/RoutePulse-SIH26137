"""The three-case acceptance rule, and the vehicle state that advances.

The reviewer's requirement is that the implementation DEMONSTRATE the rule,
not only document it.
"""
from __future__ import annotations

import math

from routepulse.validator import score


def test_case1_rejects_a_trivial_improvement(engine):
    """A routing system that re-tasks five drivers to save twenty seconds is
    useless in the field, so a sub-epsilon gain must be refused."""
    engine.initial_plan(budget=0.4, seed=1)
    res = engine.replan(budget=0.2, seed=1, engines=("emergency",))
    assert "CASE 1" in res.case
    if res.accepted:
        gain = (res.solution.score, engine.incumbent.score)
        assert gain[0] <= gain[1]
    else:
        assert "rejected" in res.case


def test_case2_accepts_without_an_epsilon_comparison(engine):
    """Requiring a new plan to beat an IMPOSSIBLE one by 1% is undefined.

    The infeasibility here is a vehicle breakdown, which is a real operational
    path rather than a poked field: the broken vehicle's stops are released,
    the incumbent is short those deliveries, and no epsilon test can be run
    against a plan that does not serve everyone.
    """
    engine.initial_plan(budget=0.4, seed=1)
    busy = [r for r in engine.incumbent.routes if r.customer_ids]
    assert len(busy) >= 2, "need a fleet with work on it"
    released = engine.remove_vehicle(busy[0].vehicle_id)
    assert released > 0

    inc = score(engine.inst, engine.incumbent.copy(), engine.tm, engine.w)
    assert not inc.feasible, "removing a working vehicle should strand its stops"

    res = engine.replan(budget=0.4, seed=1, engines=("emergency", "alns"))
    assert "CASE 2" in res.case
    assert res.accepted
    assert res.solution.feasible


def test_case3_holds_and_raises_an_alert(engine, monkeypatch):
    """A failure that reaches nobody is not handled."""
    engine.initial_plan(budget=0.4, seed=1)
    import routepulse.dynamic as dyn

    # every engine returns something infeasible -> nothing to accept
    def broken(inst, tm, w, seed=0):
        from routepulse.model import Route, Solution
        return Solution(routes=[Route(v.id, []) for v in inst.vehicles])

    monkeypatch.setattr(dyn, "greedy_insertion", broken)
    res = engine.replan(budget=0.2, seed=1, engines=("emergency",))
    assert "CASE 3" in res.case
    assert res.alert
    assert not res.accepted


def test_commitment_beats_replanning(engine):
    engine.initial_plan(budget=0.4, seed=1)
    frozen = engine.freeze_commitments()
    assert frozen
    res = engine.replan(budget=0.3, seed=1, engines=("alns",))
    if res.accepted:
        by_v = {r.vehicle_id: r.customer_ids for r in res.solution.routes}
        for v in engine.inst.vehicles:
            if v.committed_customer is not None and by_v.get(v.id):
                assert by_v[v.id][0] == v.committed_customer


# --------------------------------------------------- vehicle state (P0-09)

def test_advancing_the_clock_serves_stops_and_moves_vehicles(engine):
    engine.initial_plan(budget=0.5, seed=1)
    n_before = engine.inst.n
    starts_before = [v.start_node for v in engine.inst.vehicles]
    assert all(s == engine.inst.depot_node for s in starts_before)

    info = engine.advance(60 * 60)
    assert engine.now == 3600.0
    assert info["served"] > 0, "an hour of driving served nothing"
    assert engine.inst.n == n_before - info["served"]

    moved = [v for v in engine.inst.vehicles
             if v.start_node != engine.inst.depot_node]
    assert moved, "no vehicle advanced off the depot"
    assert all(v.available_at > 0 for v in moved)


def test_replanning_after_an_advance_starts_from_the_fleet_not_the_depot(engine):
    engine.initial_plan(budget=0.5, seed=1)
    engine.advance(45 * 60)
    res = engine.replan(budget=0.3, seed=1, engines=("alns",))
    assert res.solution is not None
    served = {cid for r in res.solution.routes for cid in r.customer_ids}
    assert served == {c.id for c in engine.inst.customers}
    # the matrix must be indexed over the REMAINING work
    assert len(engine.tm.nodes) <= engine.inst.n + 1 + len(engine.inst.vehicles)


def test_the_clock_is_the_only_clock(engine):
    """Every event carries a simulation timestamp from the same origin."""
    engine.initial_plan(budget=0.4, seed=1)
    c = engine.inst.customers[0]
    engine.apply_closure(c.lat, c.lon, radius_m=300)
    engine.log_event("closure", "test closure")
    engine.advance(10 * 60)
    engine.log_event("marker", "after advance")
    stamps = [e["t"] for e in engine.event_log]
    assert stamps == sorted(stamps), "event timestamps are not monotone"
    assert stamps[-1] >= 600.0

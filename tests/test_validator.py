"""The official scorer and the hard feasibility gate."""
from __future__ import annotations

import math

from routepulse.model import Route, Solution
from routepulse.validator import (churn_components, churn_value,
                                  congestion_exposure, score, validate)


def plan_all(inst):
    """Everything on vehicle 0 — valid shape, probably over capacity."""
    return Solution(routes=[Route(v.id, [c.id for c in inst.customers]
                                  if v.id == inst.vehicles[0].id else [])
                            for v in inst.vehicles])


def test_missing_customer_is_infeasible(instance, matrix, weights):
    sol = Solution(routes=[Route(instance.vehicles[0].id,
                                 [c.id for c in instance.customers[:-1]])]
                   + [Route(v.id, []) for v in instance.vehicles[1:]])
    s = score(instance, sol, matrix, weights)
    assert not s.feasible
    assert any("unserved" in v for v in s.violations)


def test_duplicate_customer_is_infeasible(instance, matrix, weights):
    ids = [c.id for c in instance.customers]
    sol = Solution(routes=[Route(instance.vehicles[0].id, ids + ids[:1])]
                   + [Route(v.id, []) for v in instance.vehicles[1:]])
    s = score(instance, sol, matrix, weights)
    assert not s.feasible
    assert any("more than once" in v for v in s.violations)


def test_over_capacity_is_infeasible(instance, matrix, weights):
    s = score(instance, plan_all(instance), matrix, weights)
    assert not s.feasible
    assert any("over capacity" in v for v in s.violations)


def test_broken_commitment_is_infeasible(instance, matrix, weights):
    inst = instance
    v0 = inst.vehicles[0]
    ids = [c.id for c in inst.customers]
    v0.committed_customer = ids[-1]
    try:
        sol = Solution(routes=[Route(v0.id, ids[:3])]
                       + [Route(v.id, []) for v in inst.vehicles[1:]])
        sol.routes[-1].customer_ids = ids[3:]
        s = score(inst, sol, matrix, weights)
        assert not s.feasible
        assert any("committed" in v for v in s.violations)
    finally:
        v0.committed_customer = None


# ------------------------------------------------- congestion exposure (P0-01)

def test_exposure_is_zero_on_an_undisturbed_network(engine):
    """It must not quietly inflate every score, or benchmark arms built on a
    clean network stop being comparable."""
    engine.initial_plan(budget=0.4, seed=1)
    assert congestion_exposure(engine.inst, engine.incumbent, engine.tm) == 0.0
    assert engine.incumbent.congestion_exposure == 0.0


def test_exposure_becomes_positive_under_a_jam_and_raises_the_score(engine):
    engine.initial_plan(budget=0.4, seed=1)
    before = score(engine.inst, engine.incumbent.copy(), engine.tm, engine.w)
    c = engine.inst.customers[2]
    engine.apply_congestion(c.lat, c.lon, multiplier=8.0, radius_m=600)
    engine.tm.rebuild_all()
    after = score(engine.inst, engine.incumbent.copy(), engine.tm, engine.w)

    assert after.congestion_exposure > 0.0, "the term is still a placeholder"
    assert after.score > before.score
    # and the exposure term is actually IN the objective, not just reported
    gamma = engine.w.gamma_congestion
    assert gamma > 0
    recomputed = (engine.w.alpha_travel * after.travel_time
                  + engine.w.beta_lateness * after.lateness
                  + gamma * after.congestion_exposure
                  + engine.w.delta_churn * after.churn)
    assert abs(recomputed - after.score) < 1e-6


def test_exposure_is_not_just_lateness(engine):
    """A plan can be deep in traffic and still hit every window. The fleet
    paid for that traffic either way."""
    engine.initial_plan(budget=0.4, seed=1)
    c = engine.inst.customers[1]
    engine.apply_congestion(c.lat, c.lon, multiplier=3.0, radius_m=500)
    engine.tm.rebuild_all()
    s = score(engine.inst, engine.incumbent.copy(), engine.tm, engine.w)
    assert s.congestion_exposure > 0.0
    assert s.lateness >= 0.0


# ------------------------------------------------------------------- churn

def test_churn_is_zero_against_an_identical_plan(instance, matrix, weights):
    sol = plan_all(instance)
    assert churn_value(sol, sol.copy()) == 0.0


def test_churn_counts_reassignment(instance):
    ids = [c.id for c in instance.customers]
    a = Solution(routes=[Route(0, ids[:5]), Route(1, ids[5:10])])
    b = Solution(routes=[Route(0, ids[:4]), Route(1, [ids[4]] + ids[5:10])])
    comp = churn_components(b, a)
    assert comp["reassigned"] == 1
    assert 1 in comp["vehicles_changed"]

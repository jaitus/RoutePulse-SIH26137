"""Commitment safety — the P0-02 acceptance criterion, literally.

The reviewer's definition of done: "For 1, 2, and N committed vehicles: every
committed stop remains first on its own vehicle; no committed stop is
duplicated or lost; 10,000 randomized decode tests pass."

That is what this file runs. It is deliberately a property test rather than a
handful of examples, because the old decoder failed only on specific
collisions -- two committed stops landing in the same Split chunk, or a
committed vehicle already consumed by an earlier chunk -- which a fixture
suite would have missed exactly the way review did.
"""
from __future__ import annotations

import random

import pytest

from conftest import build_instance
from routepulse.costs import TimeMatrix
from routepulse.model import ObjectiveWeights
from routepulse.solvers.alns import ALNS
from routepulse.solvers.qpso import keys_to_solution


def _decode_case(inst, tm, w, rng, n_committed):
    for v in inst.vehicles:
        v.committed_customer = None
    ids = [c.id for c in inst.customers]
    picks = rng.sample(ids, min(n_committed, len(ids), len(inst.vehicles)))
    for v, cid in zip(inst.vehicles, picks):
        v.committed_customer = cid
    frozen = {v.committed_customer: v.id for v in inst.vehicles
              if v.committed_customer is not None}
    keys = [rng.random() for _ in ids]
    sol = keys_to_solution(inst, tm, w, keys, ids, frozen)
    return sol, frozen, ids


@pytest.mark.parametrize("n_committed", [0, 1, 2, 3, 4])
def test_decode_is_commitment_safe(instance, matrix, weights, n_committed):
    rng = random.Random(1000 + n_committed)
    # 2,000 randomized decodes per arm x 5 arms = 10,000 total
    for _ in range(2000):
        sol, frozen, ids = _decode_case(instance, matrix, weights, rng, n_committed)
        served = [cid for r in sol.routes for cid in r.customer_ids]

        assert len(served) == len(set(served)), "a customer was duplicated"
        assert set(served) == set(ids), "a customer was lost by the decoder"

        by_vehicle = {r.vehicle_id: r.customer_ids for r in sol.routes}
        for cid, vid in frozen.items():
            assert by_vehicle[vid], f"committed vehicle {vid} got an empty route"
            assert by_vehicle[vid][0] == cid, "committed stop is not first"


def test_alns_destroy_repair_never_moves_a_committed_stop(graph, weights):
    """ALNS is the adopted improvement layer, so its operators have to honour
    commitments too -- a decoder that is safe and an improver that is not
    leaves the same hole."""
    inst = build_instance(graph, n=16, k=4, seed=11)
    nodes = [inst.depot_node] + [c.id for c in inst.customers]
    tm = TimeMatrix(graph, nodes, buckets=3)
    from routepulse.solvers.heuristics import greedy_insertion
    import time

    for v, c in zip(inst.vehicles[:3], inst.customers[:3]):
        v.committed_customer = c.id
    sol = greedy_insertion(inst, tm, weights, seed=2)
    eng = ALNS(inst, tm, weights, seed=5)
    out, _tel = eng.run(sol, time.perf_counter() + 0.4)

    by_vehicle = {r.vehicle_id: r.customer_ids for r in out.routes}
    served = [cid for r in out.routes for cid in r.customer_ids]
    assert len(served) == len(set(served))
    assert set(served) == {c.id for c in inst.customers}
    for v in inst.vehicles:
        if v.committed_customer is not None:
            assert by_vehicle[v.id][0] == v.committed_customer


def test_split_decode_never_drops_a_customer(instance, matrix, weights):
    """The fallback path used to `return out[:K]`, silently discarding the
    overflow. A decode that forgets a delivery is not an infeasible plan the
    validator can flag -- it is a plan that looks fine and is short a parcel."""
    from routepulse.solvers.qpso import split_decode
    rng = random.Random(7)
    ids = [c.id for c in instance.customers]
    for _ in range(200):
        perm = ids[:]
        rng.shuffle(perm)
        chunks = split_decode(instance, matrix, weights, perm)
        flat = [c for ch in chunks for c in ch]
        assert sorted(flat) == sorted(perm)
        assert len(chunks) <= len(instance.vehicles)

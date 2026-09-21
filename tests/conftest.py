"""Shared fixtures.

The suite runs against the REAL Bengaluru graph when the cache is present and
falls back to the synthetic grid otherwise, so it is meaningful on a developer
machine and still runs in a bare checkout. Every fixture is session-scoped
because building a graph and a matrix is the expensive part, not the asserts.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from routepulse.costs import TimeMatrix                        # noqa: E402
from routepulse.dynamic import Engine                          # noqa: E402
from routepulse.graph import RoadGraph, synthetic_grid         # noqa: E402
from routepulse.model import ObjectiveWeights, random_instance  # noqa: E402


@pytest.fixture(scope="session")
def graph() -> RoadGraph:
    cache = os.path.join(ROOT, "data", "bengaluru_simplified.json")
    if os.path.exists(cache):
        return RoadGraph.from_json(cache)
    return synthetic_grid(16, 16)


@pytest.fixture(scope="session")
def weights() -> ObjectiveWeights:
    return ObjectiveWeights()


def build_instance(graph, n=14, k=4, seed=3):
    nodes = [(nd, la, lo) for nd, (la, lo) in graph.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = graph.nearest_node(lat0, lon0)
    return random_instance(depot, nodes, n_customers=n, n_vehicles=k,
                           capacity=110, seed=seed,
                           depot_lat=graph.nodes[depot][0],
                           depot_lon=graph.nodes[depot][1])


@pytest.fixture(scope="session")
def instance(graph):
    return build_instance(graph)


@pytest.fixture()
def engine(graph, weights):
    """A FRESH engine per test: these tests mutate overlays and vehicle state,
    and a shared one would make failures depend on test order."""
    inst = build_instance(graph)
    return Engine(graph, inst, weights, matrix_buckets=3)


@pytest.fixture(scope="session")
def matrix(graph, instance):
    nodes = [instance.depot_node] + [c.id for c in instance.customers]
    return TimeMatrix(graph, nodes, buckets=3)

"""Core data model for RoutePulse.

Deliverable 2 (Mathematical Formulation) is documented in FORMULATION.md and
mirrored here in code: decision variables are the per-vehicle ordered customer
sequences; constraints are capacity, time windows and flow conservation
(every customer served exactly once, every route starts and ends at the depot).
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field, asdict


@dataclass
class Customer:
    id: int
    lat: float
    lon: float
    demand: float
    service_time: float = 120.0      # seconds spent at the stop
    tw_start: float = 0.0            # seconds from horizon start
    tw_end: float = 86400.0
    priority: int = 0                # 1 = protected deadline, 0 = normal

    @property
    def has_tw(self) -> bool:
        return self.tw_start > 0.0 or self.tw_end < 86400.0


@dataclass
class Vehicle:
    id: int
    capacity: float
    start_node: int                  # graph node it is currently at / heading to
    available_at: float = 0.0        # earliest time it can begin new work
    committed_customer: int | None = None   # frozen next leg, cannot be re-planned


@dataclass
class Instance:
    """One dynamic re-optimisation problem: the REMAINING work."""
    depot_node: int
    customers: list[Customer]
    vehicles: list[Vehicle]
    horizon_start: float = 0.0
    name: str = "instance"

    # customer id -> index lookup
    def __post_init__(self) -> None:
        self._by_id = {c.id: i for i, c in enumerate(self.customers)}

    def index_of(self, cid: int) -> int:
        return self._by_id[cid]

    @property
    def n(self) -> int:
        return len(self.customers)

    @property
    def total_demand(self) -> float:
        return sum(c.demand for c in self.customers)

    @property
    def total_capacity(self) -> float:
        return sum(v.capacity for v in self.vehicles)


@dataclass
class Route:
    vehicle_id: int
    customer_ids: list[int] = field(default_factory=list)
    arrival_times: list[float] = field(default_factory=list)
    load: float = 0.0
    travel_time: float = 0.0
    completion_time: float = 0.0


@dataclass
class Solution:
    routes: list[Route] = field(default_factory=list)
    # populated by the scorer, never by a solver
    travel_time: float = 0.0
    lateness: float = 0.0
    churn: float = 0.0
    congestion_exposure: float = 0.0
    score: float = math.inf
    feasible: bool = False
    violations: list[str] = field(default_factory=list)

    def assignment(self) -> dict[int, int]:
        """customer id -> vehicle id"""
        out: dict[int, int] = {}
        for r in self.routes:
            for cid in r.customer_ids:
                out[cid] = r.vehicle_id
        return out

    def sequence_pred(self) -> dict[int, int | None]:
        """customer id -> predecessor customer id (None if first on route)"""
        out: dict[int, int | None] = {}
        for r in self.routes:
            prev = None
            for cid in r.customer_ids:
                out[cid] = prev
                prev = cid
        return out

    def served(self) -> set[int]:
        return {cid for r in self.routes for cid in r.customer_ids}

    @property
    def makespan(self) -> float:
        return max((r.completion_time for r in self.routes), default=0.0)

    @property
    def sum_completion(self) -> float:
        return sum(r.completion_time for r in self.routes)

    def copy(self) -> "Solution":
        return Solution(
            routes=[Route(r.vehicle_id, list(r.customer_ids), list(r.arrival_times),
                          r.load, r.travel_time, r.completion_time) for r in self.routes],
            travel_time=self.travel_time, lateness=self.lateness, churn=self.churn,
            congestion_exposure=self.congestion_exposure, score=self.score,
            feasible=self.feasible, violations=list(self.violations),
        )

    def to_dict(self) -> dict:
        return {
            "routes": [asdict(r) for r in self.routes],
            "travel_time": self.travel_time,
            "lateness": self.lateness,
            "churn": self.churn,
            "score": self.score,
            "feasible": self.feasible,
            "violations": self.violations,
            "makespan": self.makespan,
            "sum_completion": self.sum_completion,
        }


@dataclass
class ObjectiveWeights:
    """Fixed per experiment, recorded in every run log, never tuned per solver."""
    alpha_travel: float = 1.0
    beta_lateness: float = 8.0          # heavy: protects priority deadlines
    gamma_congestion: float = 0.5
    delta_churn: float = 40.0           # seconds-equivalent per churned customer
    # NOTE: infeasibility is NOT a weight. Feasibility is a hard gate in the
    # validator and in acceptance. Lambda-as-penalty only guides search inside
    # solvers, never decides acceptance. See FORMULATION.md.
    lambda_search_penalty: float = 1e6

    def to_dict(self) -> dict:
        return asdict(self)


def random_instance(
    depot_node: int,
    nodes: list[tuple[int, float, float]],
    n_customers: int = 40,
    n_vehicles: int = 6,
    capacity: float = 120.0,
    seed: int = 0,
    tw_fraction: float = 0.35,
    priority_fraction: float = 0.15,
) -> Instance:
    """Build a demo instance by sampling stop locations from real graph nodes."""
    rng = random.Random(seed)
    pool = [nd for nd in nodes if nd[0] != depot_node]
    picked = rng.sample(pool, min(n_customers, len(pool)))

    customers: list[Customer] = []
    for i, (nid, lat, lon) in enumerate(picked, start=1):
        demand = round(rng.uniform(3, 18), 1)
        tw_start, tw_end = 0.0, 86400.0
        if rng.random() < tw_fraction:
            s = rng.uniform(1800, 12600)          # 30 min .. 3.5 h
            tw_start, tw_end = s, s + rng.uniform(3600, 7200)
        prio = 1 if rng.random() < priority_fraction else 0
        customers.append(Customer(
            id=nid, lat=lat, lon=lon, demand=demand,
            service_time=rng.uniform(90, 240),
            tw_start=tw_start, tw_end=tw_end, priority=prio,
        ))

    vehicles = [Vehicle(id=k, capacity=capacity, start_node=depot_node)
                for k in range(n_vehicles)]
    return Instance(depot_node=depot_node, customers=customers,
                    vehicles=vehicles, name=f"demo-n{n_customers}-k{n_vehicles}-s{seed}")


def save_instance(inst: Instance, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "depot_node": inst.depot_node,
            "name": inst.name,
            "customers": [asdict(c) for c in inst.customers],
            "vehicles": [asdict(v) for v in inst.vehicles],
        }, f, indent=1)


def load_instance(path: str) -> Instance:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return Instance(
        depot_node=d["depot_node"],
        customers=[Customer(**c) for c in d["customers"]],
        vehicles=[Vehicle(**v) for v in d["vehicles"]],
        name=d.get("name", "instance"),
    )

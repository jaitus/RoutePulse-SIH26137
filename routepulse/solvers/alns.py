"""Traffic-Aware Adaptive Large Neighbourhood Search — blueprint Appendix A.

WHAT ALNS IS
------------
Ropke & Pisinger (2006, *Transportation Science* 40(4):455-472). Repeatedly
destroy part of an incumbent solution and repair it, choosing WHICH destroy and
WHICH repair operator to use by roulette on weights that adapt to what has been
working. It is the strongest general-purpose heuristic family for rich VRPs, and
it is the reason Appendix A of the blueprint proposes it as a replacement for
the 2-opt/relocate/swap improvement layer.

WHY IT IS BEHIND AN ADOPTION GATE
---------------------------------
The blueprint is explicit that ALNS is EXPERIMENTAL and must not be adopted on
reputation. The whole point of the benchmark harness is that the project has
already caught one layer (the swarm) contributing nothing measurable; adopting a
second layer because the literature likes it would repeat exactly the mistake
the project was built to avoid. So ALNS ships as a selectable improvement
operator with its own arm in `scripts/bench.py`, and the README reports the
paired Wilcoxon result whichever way it falls.

WHAT "TRAFFIC-AWARE" ADDS
-------------------------
Standard ALNS removal operators are geometric: random, worst-cost, and Shaw
relatedness. None of them knows that a road got slower, which is the only thing
that ever changes in this system. So there is a fourth removal operator here
that ranks stops by how far their inbound leg's realised travel time has
diverged from its free-flow equivalent -- i.e. it tears out the part of the plan
the traffic actually broke, rather than a random part. That operator is the
delta over textbook ALNS, and the benchmark measures it separately.

All four destroy operators and both repair operators honour committed legs: a
vehicle already driving to its next stop keeps that stop at position 0 and it is
never removed. Feasibility remains a hard gate in the validator; the SA
acceptance below runs on the same soft search penalty every other engine uses.
"""
from __future__ import annotations

import math
import random
import time

from ..costs import TimeMatrix
from ..model import Instance, ObjectiveWeights, Route, Solution
from .heuristics import _route_penalty

# Ropke & Pisinger's reward schedule. Kept at the published values rather than
# tuned on our own instances, so the arm is a fair test of the published method.
SIGMA_NEW_BEST = 33.0
SIGMA_BETTER = 9.0
SIGMA_ACCEPTED = 13.0
REACTION = 0.8              # weight decay between segments
SEGMENT = 25                # iterations per weight update


def _free_flow_seconds(tm: TimeMatrix, a: int, b: int,
                       kmph: float = 50.0) -> float:
    """Straight-line time between two graph nodes at an unobstructed speed.

    The denominator of the congestion ratio. Deliberately a lower bound -- no
    road is straighter than the straight line -- so the ratio is conservative
    and a stop only looks congested when it really is.
    """
    na, nb = tm.g.nodes.get(a), tm.g.nodes.get(b)
    if na is None or nb is None:
        return 1.0
    dlat = (nb[0] - na[0]) * 111_320.0
    dlon = (nb[1] - na[1]) * 111_320.0 * math.cos(math.radians(na[0]))
    metres = math.hypot(dlat, dlon)
    return max(1.0, metres / (kmph / 3.6))


class ALNS:
    def __init__(self, inst: Instance, tm: TimeMatrix, w: ObjectiveWeights,
                 seed: int = 0) -> None:
        self.inst = inst
        self.tm = tm
        self.w = w
        self.rng = random.Random(seed)
        self.cust = {c.id: c for c in inst.customers}
        self.veh = {v.id: v for v in inst.vehicles}
        self.destroy = [self._random_removal, self._worst_removal,
                        self._shaw_removal, self._traffic_removal]
        self.destroy_names = ["random", "worst", "shaw", "traffic-aware"]
        self.repair = [self._greedy_insert, self._regret2_insert]
        self.repair_names = ["greedy", "regret-2"]
        self.d_weight = [1.0] * len(self.destroy)
        self.r_weight = [1.0] * len(self.repair)
        self.d_score = [0.0] * len(self.destroy)
        self.r_score = [0.0] * len(self.repair)
        self.d_used = [0] * len(self.destroy)
        self.r_used = [0] * len(self.repair)

    # ------------------------------------------------------------- utilities

    def _frozen(self, vid: int) -> int:
        v = self.veh.get(vid)
        return 1 if (v is not None and v.committed_customer is not None) else 0

    def _cost(self, seqs: dict[int, list[int]]) -> float:
        total = 0.0
        for vid, s in seqs.items():
            v = self.veh.get(vid)
            if v is None:
                continue
            c = _route_penalty(self.inst, self.tm, v, s, self.w)
            if math.isinf(c):
                return math.inf
            total += c
        return total

    def _load(self, seq: list[int]) -> float:
        return sum(self.cust[c].demand for c in seq if c in self.cust)

    # ------------------------------------------------------ destroy operators

    def _removable(self, seqs: dict[int, list[int]]) -> list[tuple[int, int]]:
        """(vehicle, customer) pairs that are legal to remove."""
        return [(vid, cid) for vid, s in seqs.items()
                for cid in s[self._frozen(vid):]]

    def _random_removal(self, seqs: dict[int, list[int]], q: int) -> list[int]:
        pool = self._removable(seqs)
        self.rng.shuffle(pool)
        return self._pull(seqs, pool[:q])

    def _worst_removal(self, seqs: dict[int, list[int]], q: int) -> list[int]:
        """Remove the stops whose deletion saves the most — the plan's mistakes."""
        scored: list[tuple[float, int, int]] = []
        for vid, s in seqs.items():
            v = self.veh.get(vid)
            if v is None:
                continue
            base = _route_penalty(self.inst, self.tm, v, s, self.w)
            lo = self._frozen(vid)
            for i in range(lo, len(s)):
                without = s[:i] + s[i + 1:]
                gain = base - _route_penalty(self.inst, self.tm, v, without, self.w)
                if math.isfinite(gain):
                    scored.append((gain, vid, s[i]))
        scored.sort(reverse=True)
        # Randomised selection (y^p over a sorted list) rather than strict
        # top-q: deterministic worst-removal cycles between the same few stops.
        picks: list[tuple[int, int]] = []
        avail = scored[:]
        while avail and len(picks) < q:
            y = self.rng.random() ** 3.0
            k = int(y * len(avail))
            _g, vid, cid = avail.pop(k)
            picks.append((vid, cid))
        return self._pull(seqs, picks)

    def _shaw_removal(self, seqs: dict[int, list[int]], q: int) -> list[int]:
        """Relatedness removal: tear out a cluster that can meaningfully swap.

        Relatedness = normalised geographic distance + time-window overlap +
        demand similarity, which is Shaw's (1998) formulation as adapted by
        Ropke & Pisinger.
        """
        pool = self._removable(seqs)
        if not pool:
            return []
        seed_vid, seed_cid = self.rng.choice(pool)
        s0 = self.cust[seed_cid]

        def relatedness(cid: int) -> float:
            c = self.cust[cid]
            dlat = (c.lat - s0.lat) * 111_320.0
            dlon = (c.lon - s0.lon) * 111_320.0 * math.cos(math.radians(s0.lat))
            dist = math.hypot(dlat, dlon) / 1000.0
            tw = abs(c.tw_start - s0.tw_start) / 3600.0
            dem = abs(c.demand - s0.demand) / 20.0
            return dist + 0.5 * tw + 0.3 * dem

        rest = [(relatedness(cid), vid, cid) for vid, cid in pool
                if cid != seed_cid]
        rest.sort()
        picks = [(seed_vid, seed_cid)] + [(vid, cid) for _r, vid, cid in rest[:q - 1]]
        return self._pull(seqs, picks)

    def _traffic_removal(self, seqs: dict[int, list[int]], q: int) -> list[int]:
        """THE TRAFFIC-AWARE OPERATOR — the delta over textbook ALNS.

        Rank every stop by how badly its INBOUND leg is running compared with
        free flow, and remove the worst. After a closure or a jam this pulls out
        precisely the stops the incident stranded, instead of a random sample of
        a plan that is mostly still fine.
        """
        scored: list[tuple[float, int, int]] = []
        for vid, s in seqs.items():
            v = self.veh.get(vid)
            if v is None:
                continue
            t = max(self.inst.horizon_start, v.available_at)
            prev = v.start_node
            lo = self._frozen(vid)
            for i, cid in enumerate(s):
                leg = self.tm.tt(prev, cid, t)
                if i >= lo:
                    ratio = (math.inf if math.isinf(leg)
                             else leg / _free_flow_seconds(self.tm, prev, cid))
                    scored.append((ratio if math.isfinite(ratio) else 1e9, vid, cid))
                if math.isinf(leg):
                    break
                t += leg + self.cust[cid].service_time
                prev = cid
        scored.sort(reverse=True)
        picks: list[tuple[int, int]] = []
        avail = scored[:]
        while avail and len(picks) < q:
            y = self.rng.random() ** 4.0          # sharper bias than worst-removal
            k = int(y * len(avail))
            _r, vid, cid = avail.pop(k)
            picks.append((vid, cid))
        return self._pull(seqs, picks)

    @staticmethod
    def _pull(seqs: dict[int, list[int]], picks: list[tuple[int, int]]) -> list[int]:
        removed: list[int] = []
        for vid, cid in picks:
            if cid in seqs.get(vid, ()):
                seqs[vid].remove(cid)
                removed.append(cid)
        return removed

    # ------------------------------------------------------- repair operators

    def _insertion_costs(self, seqs: dict[int, list[int]], cid: int
                         ) -> list[tuple[float, int, int]]:
        """Every legal (delta, vehicle, position) for one customer, sorted."""
        out: list[tuple[float, int, int]] = []
        demand = self.cust[cid].demand
        for vid, s in seqs.items():
            v = self.veh.get(vid)
            if v is None or self._load(s) + demand > v.capacity + 1e-9:
                continue
            base = _route_penalty(self.inst, self.tm, v, s, self.w)
            if math.isinf(base):
                base = 0.0
            for pos in range(self._frozen(vid), len(s) + 1):
                trial = s[:pos] + [cid] + s[pos:]
                c = _route_penalty(self.inst, self.tm, v, trial, self.w)
                if math.isfinite(c):
                    out.append((c - base, vid, pos))
        out.sort()
        return out

    def _greedy_insert(self, seqs: dict[int, list[int]], removed: list[int]) -> None:
        for cid in sorted(removed, key=lambda c: (-self.cust[c].priority,
                                                  self.cust[c].tw_end)):
            opts = self._insertion_costs(seqs, cid)
            if opts:
                _d, vid, pos = opts[0]
                seqs[vid].insert(pos, cid)
            else:
                # No capacity-feasible slot. Park it on the emptiest route and
                # let the validator call the result infeasible -- silently
                # dropping a customer would be a far worse failure.
                vid = min(seqs, key=lambda k: self._load(seqs[k]))
                seqs[vid].append(cid)

    def _regret2_insert(self, seqs: dict[int, list[int]], removed: list[int]) -> None:
        """Insert whichever customer will hurt most if we wait — regret-2."""
        pending = list(removed)
        while pending:
            best_cid, best_opt, best_regret = None, None, -math.inf
            for cid in pending:
                opts = self._insertion_costs(seqs, cid)
                if not opts:
                    regret, opt = math.inf, None
                elif len(opts) == 1:
                    regret, opt = math.inf, opts[0]
                else:
                    regret, opt = opts[1][0] - opts[0][0], opts[0]
                # priority customers break ties toward being placed first
                regret += 1e6 * self.cust[cid].priority
                if regret > best_regret:
                    best_cid, best_opt, best_regret = cid, opt, regret
            pending.remove(best_cid)                      # type: ignore[arg-type]
            if best_opt is None:
                vid = min(seqs, key=lambda k: self._load(seqs[k]))
                seqs[vid].append(best_cid)                # type: ignore[arg-type]
            else:
                _d, vid, pos = best_opt
                seqs[vid].insert(pos, best_cid)           # type: ignore[arg-type]

    # ------------------------------------------------------------------ driver

    def _roulette(self, weights: list[float]) -> int:
        total = sum(weights)
        r = self.rng.random() * total
        acc = 0.0
        for i, x in enumerate(weights):
            acc += x
            if r <= acc:
                return i
        return len(weights) - 1

    def run(self, sol: Solution, deadline: float) -> tuple[Solution, dict]:
        seqs = {r.vehicle_id: list(r.customer_ids) for r in sol.routes}
        for v in self.inst.vehicles:
            seqs.setdefault(v.id, [])
        cur = self._cost(seqs)
        best_seqs = {k: list(v) for k, v in seqs.items()}
        best = cur
        if math.isinf(cur):
            cur = 1e15

        n = max(1, self.inst.n)
        # Start temperature: a move that worsens the plan by 5% is accepted
        # with probability 0.5 at iteration 0 (Ropke & Pisinger's rule).
        temp = max(1e-6, 0.05 * abs(best) / math.log(2)) if math.isfinite(best) else 1.0
        cooling = 0.9985

        it = 0
        accepted = 0
        new_bests = 0
        while time.perf_counter() < deadline:
            it += 1
            di = self._roulette(self.d_weight)
            ri = self._roulette(self.r_weight)
            self.d_used[di] += 1
            self.r_used[ri] += 1

            trial = {k: list(v) for k, v in seqs.items()}
            q = self.rng.randint(max(1, int(0.10 * n)), max(2, int(0.35 * n)))
            removed = self.destroy[di](trial, q)
            if removed:
                self.repair[ri](trial, removed)
            cost = self._cost(trial)
            if math.isinf(cost):
                cost = 1e15

            if cost < best - 1e-9:
                best, best_seqs = cost, {k: list(v) for k, v in trial.items()}
                seqs, cur = trial, cost
                self.d_score[di] += SIGMA_NEW_BEST
                self.r_score[ri] += SIGMA_NEW_BEST
                accepted += 1
                new_bests += 1
            elif cost < cur - 1e-9:
                seqs, cur = trial, cost
                self.d_score[di] += SIGMA_BETTER
                self.r_score[ri] += SIGMA_BETTER
                accepted += 1
            elif self.rng.random() < math.exp(-(cost - cur) / max(1e-9, temp)):
                seqs, cur = trial, cost
                self.d_score[di] += SIGMA_ACCEPTED
                self.r_score[ri] += SIGMA_ACCEPTED
                accepted += 1

            temp *= cooling

            if it % SEGMENT == 0:
                for i in range(len(self.d_weight)):
                    if self.d_used[i]:
                        self.d_weight[i] = (REACTION * self.d_weight[i]
                                            + (1 - REACTION) * self.d_score[i]
                                            / self.d_used[i])
                    self.d_score[i], self.d_used[i] = 0.0, 0
                for i in range(len(self.r_weight)):
                    if self.r_used[i]:
                        self.r_weight[i] = (REACTION * self.r_weight[i]
                                            + (1 - REACTION) * self.r_score[i]
                                            / self.r_used[i])
                    self.r_score[i], self.r_used[i] = 0.0, 0

        out = Solution(routes=[Route(vehicle_id=vid, customer_ids=s)
                               for vid, s in best_seqs.items()])
        weights = {name: round(wt, 3)
                   for name, wt in zip(self.destroy_names, self.d_weight)}
        weights.update({name: round(wt, 3)
                        for name, wt in zip(self.repair_names, self.r_weight)})
        return out, {
            "solver": "ALNS",
            "iterations": it,
            "accepted": accepted,
            "new_bests": new_bests,
            "final_weights": weights,
            "best": None if math.isinf(best) else round(best, 2),
        }


def alns(inst: Instance, sol: Solution, tm: TimeMatrix, w: ObjectiveWeights,
         deadline: float, seed: int = 0, **_ignored) -> Solution:
    """Improvement-operator signature matching `local_search`, so the two are
    drop-in interchangeable wherever an improvement layer is selected."""
    engine = ALNS(inst, tm, w, seed=seed)
    out, _tel = engine.run(sol, deadline)
    return out


def alns_with_telemetry(inst: Instance, sol: Solution, tm: TimeMatrix,
                        w: ObjectiveWeights, deadline: float,
                        seed: int = 0) -> tuple[Solution, dict]:
    engine = ALNS(inst, tm, w, seed=seed)
    return engine.run(sol, deadline)

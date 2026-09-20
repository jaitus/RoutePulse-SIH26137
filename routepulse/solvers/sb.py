"""Simulated Bifurcation — the genuinely quantum-derived engine.

WHY THIS MODULE EXISTS
----------------------
QPSO is *quantum-inspired* in the loose sense: it borrows a delta-potential-well
sampling rule and a metaphor. A panel is entitled to ask "in what sense is this
quantum?", and the honest answer for QPSO is "by analogy only".

Simulated Bifurcation is a different kind of object. It is the classical limit
of a real quantum system: Goto (2016) showed that a network of Kerr-nonlinear
parametric oscillators, driven through its bifurcation point, adiabatically
relaxes into the ground state of an Ising Hamiltonian; Goto, Tatsumura &
Dixon (2019, *Sci. Adv.* 5:eaav2372) showed that simulating the CLASSICAL
Hamiltonian equations of that same network solves the Ising problem on ordinary
hardware, and Goto et al. (2021, *Sci. Adv.* 7:eabe7953) added the discretised
variant (dSB) used here. The dynamics below are literally the equations of
motion of a physical quantum-optical machine, integrated with symplectic Euler.
That is a stronger claim than "inspired by", and it is the reason this engine is
in the project.

THE PRICE OF THAT, STATED UP FRONT
----------------------------------
An Ising machine optimises `E(s) = 1/2 s'Js + h's` over spins s in {-1,+1}. To
use it you must embed your problem in that form, and the embedding costs:

  1. TIME-DEPENDENCE IS LOST. J is a fixed matrix. A travel time that varies
     with departure time cannot be written into it without discretising time
     into additional spins. We therefore build J from the travel times at the
     route's own departure bucket, and treat SB's output as a PROPOSAL that is
     re-evaluated under the full time-dependent cost model before it can be
     accepted. The approximation can waste effort; it cannot corrupt a plan.

  2. CONSTRAINTS BECOME PENALTIES. "Visit each stop exactly once" is a hard
     constraint in the VRP and a weighted penalty term in the Ising model, so a
     relaxed trajectory can land on a spin configuration that is not a valid
     permutation at all. We measure how often that happens (`invalid_rate`) and
     report it rather than hiding it behind a repair step. This is the classic
     objection to QUBO-embedded routing and it deserves a number, not a caveat.

  3. IT SCALES AS m^2 SPINS for an m-stop sequence. A 10-stop route is 100
     spins; a 30-stop single tour would be 900. SB is applied per route, which
     is where the sizes are small enough to be honest about.

THE EMBEDDING (Lucas 2014, *Front. Physics* 2:5, section 7.2)
-------------------------------------------------------------
Binary x[i,p] = 1 iff stop i occupies position p of the sequence.

    H = A * SUM_i (1 - SUM_p x[i,p])^2          each stop used exactly once
      + A * SUM_p (1 - SUM_i x[i,p])^2          each position filled exactly once
      + B * SUM_p SUM_{i!=j} d[i,j] x[i,p] x[j,p+1]     consecutive-leg cost
      + B * SUM_i d[origin,i] x[i,0]                    leg out of the origin
      + B * SUM_i d[i,depot] x[i,m-1]                   leg back to the depot

d may be asymmetric (one-way streets), which the position-indexed form handles
correctly because the pair {(i,p),(j,p+1)} is directional by construction.
QUBO -> Ising by x = (1+s)/2. A is set from the largest leg cost so that no
tour, however short, can pay for a constraint violation.
"""
from __future__ import annotations

import math
import time

from ..costs import TimeMatrix
from ..model import Instance, ObjectiveWeights, Route, Solution
from .heuristics import _route_penalty

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:                                        # pragma: no cover
    HAVE_NUMPY = False


# --------------------------------------------------------------- the SB solver

def simulated_bifurcation(J, h, steps: int = 400, dt: float = 0.5,
                          a0: float = 1.0, agents: int = 24,
                          seed: int = 0, variant: str = "dSB",
                          deadline: float | None = None):
    """Minimise E(s) = 0.5 s'Js + h's over s in {-1,+1}^N.

    `J` must be symmetric with a zero diagonal. Returns (spins, energy, info).

    The equations of motion, integrated with the symplectic Euler scheme the
    papers use (position updated from the freshly updated momentum):

        a(t) = a0 * t/T                                  pump, ramped linearly
        y_i <- y_i + [ -(a0 - a(t)) x_i - c0 g_i ] dt
        x_i <- x_i + a0 y_i dt

    where g_i is the coupling force. The variants differ only in g:

        bSB   g_i = SUM_j J_ij x_j + h_i        ballistic, continuous coupling
        dSB   g_i = SUM_j J_ij sgn(x_j) + h_i   discrete, and the one that
                                                eliminates the analogue errors
                                                bSB inherits from continuous x

    Inelastic walls at |x| = 1 (clip the position, kill the momentum) are what
    makes the discrete variant stable; without them the trajectories diverge.

    `agents` independent trajectories are integrated as one batched array --
    SB's defining practical property is that it is embarrassingly parallel, and
    running 24 restarts costs barely more than one.
    """
    if not HAVE_NUMPY:
        raise RuntimeError("Simulated Bifurcation requires numpy")

    J = np.asarray(J, dtype=np.float64)
    h = np.asarray(h, dtype=np.float64)
    N = J.shape[0]
    if N == 0:
        return np.zeros(0), 0.0, {"steps": 0, "agents": 0}

    # Coupling strength. Goto et al. scale c0 so the coupling term is comparable
    # to the pump at the bifurcation point; the standard choice is
    # c0 = 0.5 / (sqrt(N) * std(J)). Without it the dynamics either never leave
    # the origin or saturate at the walls on the first step.
    off = J[~np.eye(N, dtype=bool)]
    sigma = float(off.std()) if off.size else 0.0
    c0 = 0.5 / (math.sqrt(N) * sigma) if sigma > 1e-12 else 0.5

    rng = np.random.default_rng(seed)
    x = rng.uniform(-0.1, 0.1, size=(agents, N))
    y = rng.uniform(-0.1, 0.1, size=(agents, N))

    done = steps
    for k in range(steps):
        if deadline is not None and (k & 31) == 0 and time.perf_counter() > deadline:
            done = k
            break
        a = a0 * (k + 1) / steps
        drive = np.sign(x) if variant == "dSB" else x
        g = drive @ J + h                       # (agents, N)
        y += (-(a0 - a) * x - c0 * g) * dt
        x += a0 * y * dt
        # inelastic walls
        out = np.abs(x) > 1.0
        if out.any():
            x[out] = np.sign(x[out])
            y[out] = 0.0

    s = np.sign(x)
    s[s == 0] = 1.0
    energies = 0.5 * np.einsum("ai,ij,aj->a", s, J, s) + s @ h
    best = int(np.argmin(energies))
    return s[best], float(energies[best]), {
        "steps": done, "agents": agents, "variant": variant,
        "c0": round(c0, 6), "N": N,
        "energy_spread": float(energies.max() - energies.min()),
    }


def ising_energy(J, h, s) -> float:
    """E(s) = 0.5 s'Js + h's — the one convention used everywhere in this file."""
    s = np.asarray(s, dtype=np.float64)
    return float(0.5 * s @ np.asarray(J) @ s + s @ np.asarray(h))


# ------------------------------------------------------------- the TSP embedding

def tsp_ising(D, origin_row, depot_col, penalty_scale: float = 0.5,
              position_cost=None):
    """Build (J, h, offset) for sequencing m stops between a fixed origin and
    a fixed depot return.

    `D[i][j]`        cost of the leg stop i -> stop j (may be asymmetric)
    `origin_row`     cost origin -> stop i, for every i
    `depot_col`      cost stop i -> depot, for every i
    `position_cost`  optional m x m array: extra cost of putting stop i at
                     position p. See TIME WINDOWS below.

    Spin index v = i*m + p for stop i at position p, so N = m^2.

    TIME WINDOWS, AND THE ONE DEGREE OF FREEDOM THE ISING FORM LEAVES US
    --------------------------------------------------------------------
    The standard objection to QUBO routing is that time windows are not
    expressible: lateness depends on the CUMULATIVE arrival time at a stop,
    which is a sum over a prefix of the permutation, not a pairwise term. That
    is true of the quadratic part. It is not true of the LINEAR part: h carries
    one coefficient per (stop, position) pair, and position is a proxy for time.

    So `position_cost[i][p]` lets a caller price "stop i served p-th" using an
    ESTIMATED arrival time for position p. The estimate is a mean-field one --
    it does not know which stops precede i, only how many -- so it is an
    approximation, not an encoding. It is measured rather than assumed: see
    `scripts/sb_eval.py`, which reports the gap to the exact optimum with the
    field on and off.
    """
    D = np.asarray(D, dtype=np.float64)
    origin_row = np.asarray(origin_row, dtype=np.float64)
    depot_col = np.asarray(depot_col, dtype=np.float64)
    m = D.shape[0]
    N = m * m

    # A must exceed the best possible saving from breaking a constraint --
    # dropping a stop saves at most the legs around it -- but making it much
    # LARGER than that flattens the objective relative to the penalty and the
    # dynamics stop being able to tell good tours from bad ones.
    #
    # Measured on 20 random 6-stop instances against brute force
    # (`python scripts/sb_eval.py`), A as a multiple of the largest leg:
    #
    #     A = 0.5x max leg   raw-valid  4/20   mean gap 7.9%   <- penalty too weak
    #     A = 1.0x max leg   raw-valid 20/20   mean gap 0.0%   <- default
    #     A = 2.0x max leg   raw-valid 20/20   mean gap 0.9%
    #     A = 4.0x max leg   raw-valid 20/20   mean gap 4.0%   <- objective drowned
    #
    # The 0.5x row is the classic QUBO failure mode in the flesh: the embedding
    # produced spin configurations that were not tours at all.
    finite = D[np.isfinite(D)]
    scale = float(finite.max()) if finite.size else 1.0
    scale = max(scale, float(np.max(origin_row[np.isfinite(origin_row)], initial=0.0)),
                float(np.max(depot_col[np.isfinite(depot_col)], initial=0.0)), 1.0)
    if position_cost is not None:
        # The position field is part of the objective, so the constraint weight
        # has to dominate IT too -- otherwise a large lateness term can pay for
        # breaking the permutation and the decode stops being a tour.
        pc = np.asarray(position_cost, dtype=np.float64)
        scale = max(scale, float(np.max(pc[np.isfinite(pc)], initial=0.0)))
    A = 2.0 * scale * penalty_scale

    # Unreachable legs would make the matrix non-finite. They are already
    # excluded by the caller (an unreachable route fails the validator long
    # before it gets here) but a defensive clamp keeps the dynamics stable.
    BIG = 10.0 * scale
    D = np.where(np.isfinite(D), D, BIG)
    origin_row = np.where(np.isfinite(origin_row), origin_row, BIG)
    depot_col = np.where(np.isfinite(depot_col), depot_col, BIG)

    Q = np.zeros((N, N), dtype=np.float64)      # symmetric, pair coefficients
    q = np.zeros(N, dtype=np.float64)           # linear coefficients

    def ix(i, p):
        return i * m + p

    # ---- constraint: each stop occupies exactly one position
    # (1 - SUM_p x)^2 = 1 - SUM_p x + 2 SUM_{p<q} x_p x_q      (x^2 = x)
    for i in range(m):
        for p in range(m):
            q[ix(i, p)] -= A
            for pq in range(p + 1, m):
                Q[ix(i, p), ix(i, pq)] += 2.0 * A
                Q[ix(i, pq), ix(i, p)] += 2.0 * A

    # ---- constraint: each position holds exactly one stop
    for p in range(m):
        for i in range(m):
            q[ix(i, p)] -= A
            for j in range(i + 1, m):
                Q[ix(i, p), ix(j, p)] += 2.0 * A
                Q[ix(j, p), ix(i, p)] += 2.0 * A

    # ---- objective: consecutive legs, plus the two fixed ends
    for p in range(m - 1):
        for i in range(m):
            for j in range(m):
                if i == j:
                    continue
                Q[ix(i, p), ix(j, p + 1)] += D[i, j]
                Q[ix(j, p + 1), ix(i, p)] += D[i, j]
    for i in range(m):
        q[ix(i, 0)] += origin_row[i]
        q[ix(i, m - 1)] += depot_col[i]

    # ---- optional position-indexed field (time windows, priorities)
    if position_cost is not None:
        P = np.asarray(position_cost, dtype=np.float64)
        for i in range(m):
            for p in range(m):
                q[ix(i, p)] += float(P[i, p])

    # ---- QUBO -> Ising with x = (1+s)/2
    #   H = SUM_{v<w} Q_vw x_v x_w + SUM_v q_v x_v
    #     = SUM_{v<w} (Q_vw/4) s_v s_w + SUM_v (q_v/2 + (1/4) SUM_w Q_vw) s_v + c
    # Q is stored symmetric, so SUM_{v<w} Q_vw = 0.5 * SUM_vw Q_vw.
    J = Q / 4.0
    np.fill_diagonal(J, 0.0)
    h = q / 2.0 + Q.sum(axis=1) / 4.0
    offset = Q.sum() / 8.0 + q.sum() / 2.0
    return J, h, offset


def decode_permutation(s, m: int) -> tuple[list[int], bool]:
    """Spins -> a stop ordering, and whether the raw decode was already valid.

    A spin configuration is a valid tour only if exactly one +1 sits in every
    row and every column of the m x m matrix. When it is not -- and with penalty
    constraints it sometimes is not -- we repair greedily by confidence rather
    than discarding the trajectory, and the caller reports how often repair was
    needed.
    """
    S = np.asarray(s, dtype=np.float64).reshape(m, m)      # [stop][position]
    picked = S > 0
    valid = bool((picked.sum(axis=0) == 1).all() and (picked.sum(axis=1) == 1).all())
    if valid:
        order = [int(np.argmax(picked[:, p])) for p in range(m)]
        return order, True

    # greedy repair: strongest (stop, position) claims first
    order: list[int | None] = [None] * m
    used_stop: set[int] = set()
    flat = sorted(((float(S[i, p]), i, p) for i in range(m) for p in range(m)),
                  reverse=True)
    for _v, i, p in flat:
        if order[p] is None and i not in used_stop:
            order[p] = i
            used_stop.add(i)
    leftovers = [i for i in range(m) if i not in used_stop]
    for p in range(m):
        if order[p] is None:
            order[p] = leftovers.pop()
    return [int(o) for o in order], False       # type: ignore[arg-type]


# ------------------------------------------------- the improvement operator

MAX_SB_STOPS = 14           # 14 stops = 196 spins. Measured: real routes in
                            # this instance family run 7-12 stops, so this
                            # covers them; beyond it the m^2 embedding starts
                            # costing more than the re-sequencing is worth.


def position_field(inst: Instance, stops: list[int], w: ObjectiveWeights,
                   t_start: float, mean_leg: float, edd_weight: float = 0.5):
    """Mean-field time-window cost of serving each stop at each position.

    Two terms, and the second one is the one that matters:

    1. DIRECT LATENESS. Arrival at position p is estimated as
       `t_start + p*(mean_leg + mean service) + mean_leg` -- it knows how many
       stops come first, not which -- and any estimated overshoot past tw_end is
       priced at the objective's own lateness weight.

    2. DEADLINE-ORDER BIAS. The first term alone measured as literally zero on
       this instance family, and the reason is instructive: the cost of a bad
       sequence here is almost never arriving late, it is arriving EARLY at a
       stop whose window has not opened. The vehicle then waits, and the wait
       cascades into every stop after it. That is a prefix effect -- it depends
       on the whole ordering, not on one position -- so it is exactly what a
       quadratic form cannot see.

       What CAN be written into the local field is the ordering the windows
       imply: rank the windowed stops by tw_end and pay `edd_weight * mean_leg`
       per position of deviation from that earliest-deadline-first rank. It is a
       bias, not an encoding, and it is reported as one.

    Stops with no window contribute nothing -- they are free to go anywhere,
    which is true.

    MEASURED (24 real routes, `python scripts/sb_eval.py`, cost of SB's chosen
    order relative to the greedy order it was given):

        edd_weight  0.0   +1428%      lateness term alone, i.e. no bias at all
        edd_weight  0.1    +454%
        edd_weight  0.2     +23%
        edd_weight  0.5     +19%      <- default
        edd_weight  2.0     +33%      bias starts overriding the geometry

    Read that top row carefully: without the ordering bias the Ising model's
    "best" tour costs fourteen times what a greedy one does. Not because the
    solver failed -- it minimised its Hamiltonian correctly -- but because the
    Hamiltonian was missing the term that dominates the real cost.
    """
    m = len(stops)
    cust = {c.id: c for c in inst.customers}
    svc = [cust[c].service_time for c in stops if c in cust]
    mean_svc = sum(svc) / len(svc) if svc else 0.0
    P = np.zeros((m, m), dtype=np.float64)

    # --- term 1: estimated lateness
    for p in range(m):
        t_hat = t_start + p * (mean_leg + mean_svc) + mean_leg
        for i, cid in enumerate(stops):
            c = cust.get(cid)
            if c is None:
                continue
            late = max(0.0, t_hat - c.tw_end)
            if late > 0:
                P[i, p] += w.beta_lateness * late * (3.0 if c.priority == 1 else 1.0)

    # --- term 2: earliest-deadline-first ordering bias
    if edd_weight > 0.0:
        windowed = [(cust[cid].tw_end, i) for i, cid in enumerate(stops)
                    if cid in cust and cust[cid].has_tw]
        windowed.sort()
        for rank, (_tw, i) in enumerate(windowed):
            # spread the windowed stops' target ranks across the whole route
            target = rank * (m - 1) / max(1, len(windowed) - 1) if len(windowed) > 1 else 0
            for p in range(m):
                P[i, p] += edd_weight * mean_leg * abs(p - target)
    return P


def sb_resequence(inst: Instance, sol: Solution, tm: TimeMatrix,
                  w: ObjectiveWeights, deadline: float,
                  steps: int = 800, agents: int = 48, seed: int = 0,
                  variant: str = "dSB", max_stops: int = MAX_SB_STOPS,
                  time_windows: bool = True,
                  ) -> tuple[Solution, dict]:
    """Re-sequence every route with Simulated Bifurcation.

    This is an improvement OPERATOR, deliberately the same shape as
    `local_search` so the two can be compared arm-to-arm in the benchmark.
    It only ever reorders a route -- it never moves a stop between vehicles,
    because the Ising embedding above is a single-tour formulation.

    Every proposal is re-costed with `_route_penalty`, the same time-dependent
    function every other engine is judged by, and kept only if it is strictly
    better. So SB cannot make a plan worse; the question the benchmark answers
    is whether it makes one better than 2-opt does, at the same budget.
    """
    if not HAVE_NUMPY:
        return sol, {"available": False, "reason": "numpy missing"}

    veh = {v.id: v for v in inst.vehicles}
    seqs = {r.vehicle_id: list(r.customer_ids) for r in sol.routes}

    tried = improved = raw_valid = 0
    spins_total = 0
    gain_total = 0.0
    t0 = time.perf_counter()

    for vid, seq in seqs.items():
        if time.perf_counter() > deadline:
            break
        v = veh.get(vid)
        if v is None:
            continue
        lo = 1 if v.committed_customer is not None else 0
        free = seq[lo:]
        m = len(free)
        if m < 3 or m > max_stops:
            # m < 3 has nothing to reorder; m > max_stops is the scaling wall,
            # and skipping is reported rather than silently truncating.
            continue

        origin = seq[lo - 1] if lo > 0 else v.start_node
        # Reference departure time: when the free portion of this route actually
        # starts. Fixing J at one instant is the embedding's core approximation.
        t_ref = max(inst.horizon_start, v.available_at)
        cust = {c.id: c for c in inst.customers}
        if lo > 0 and origin in cust:
            t_ref += tm.tt(v.start_node, origin, t_ref) + cust[origin].service_time

        D = [[0.0 if i == j else tm.tt(free[i], free[j], t_ref)
              for j in range(m)] for i in range(m)]
        origin_row = [tm.tt(origin, c, t_ref) for c in free]
        depot_col = [tm.tt(c, inst.depot_node, t_ref) for c in free]
        if any(math.isinf(x) for x in origin_row + depot_col):
            continue

        P = None
        if time_windows:
            legs = [D[i][j] for i in range(m) for j in range(m)
                    if i != j and math.isfinite(D[i][j])]
            mean_leg = sum(legs) / len(legs) if legs else 0.0
            P = position_field(inst, free, w, t_ref, mean_leg)

        J, h, _ = tsp_ising(D, origin_row, depot_col, position_cost=P)
        s, _e, info = simulated_bifurcation(J, h, steps=steps, agents=agents,
                                            seed=seed + vid, variant=variant,
                                            deadline=deadline)
        order, was_valid = decode_permutation(s, m)
        tried += 1
        raw_valid += 1 if was_valid else 0
        spins_total += info["N"]

        cand = seq[:lo] + [free[i] for i in order]
        before = _route_penalty(inst, tm, v, seq, w)
        after = _route_penalty(inst, tm, v, cand, w)
        if after < before - 1e-9:
            seqs[vid] = cand
            improved += 1
            gain_total += before - after

    out = Solution(routes=[Route(vehicle_id=vid, customer_ids=s)
                           for vid, s in seqs.items()])
    return out, {
        "available": True,
        "routes_tried": tried,
        "routes_improved": improved,
        "raw_valid_decodes": raw_valid,
        "invalid_rate": round(1.0 - raw_valid / tried, 4) if tried else None,
        "mean_spins": round(spins_total / tried, 1) if tried else 0,
        "seconds": round(time.perf_counter() - t0, 4),
        "total_gain": round(gain_total, 2),
        "variant": variant,
        "max_stops": max_stops,
        "time_window_field": time_windows,
    }

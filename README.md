# RoutePulse — SIH26137

**Quantum-Inspired Intelligent Traffic Route Optimization in Transportation Systems Using Metaheuristic Optimization**
Egreen Quanta · AICTE Smart India Hackathon 2026 · Quantum Technology Vertical

A live fleet re-optimisation control tower. Plan routes on a real road network,
inject a road closure or congestion, and watch the fleet recover under a hard
time budget — with end-to-end latency, feasibility and solver comparison all
reported rather than asserted.

---

## Quick start

```bash
cd C:\PROJECTS\SIH26137
pip install fastapi uvicorn ortools requests           # networkx/numpy usually present
python -m uvicorn server.app:app --port 8000
```

Open <http://127.0.0.1:8000>.

1. **Plan routes** — builds the initial plan.
2. **Click the map** — injects a closure (or congestion, via the toggle) there.
3. **Re-plan** — watch the acceptance decision, latency breakdown and solver race.

No internet required. No CDN, no map tiles, no external JS. The road network is
drawn on a canvas from our own API, and a synthetic grid is used if the cached
Bengaluru graph is absent — the demo cannot be killed by venue Wi-Fi.

Benchmark + ablation:

```bash
python scripts/bench.py --seeds 10 --budget 0.35
```

---

## How the five deliverables are met

| # | Deliverable | Where |
|---|---|---|
| 1 | Graph-based Network Model | `routepulse/graph.py` — weighted directed road graph from OpenStreetMap (Overpass), with the **dynamic weight update mechanism**: piecewise-linear time-of-day profiles plus incident and corridor overlays |
| 2 | Mathematical Formulation | `FORMULATION.md` — objective, decision variables, capacity / time-window / **flow conservation** constraints, FIFO condition, acceptance rule |
| 3 | Quantum-Inspired Algorithm Module | `routepulse/solvers/qpso.py` — QPSO with the **update rule written out in full** (§ below), random-key encoding, Prins Split decode |
| 4 | Software Platform / Prototype | `server/app.py` + `server/static/index.html` — API and UI, network + traffic input, optimised route output, **map visualisation** |
| 5 | Demonstration | Bengaluru zone, 30 stops, 5 vehicles, live incidents under **varying traffic conditions**; `scripts/bench.py` for experimental results |

Constraint handling, convergence analysis and systematic performance
benchmarking — all three named in the sponsor's Expected Solution — are in
`routepulse/validator.py`, the convergence panel in the UI, and
`scripts/bench.py` respectively.

---

## The QPSO update rule (Deliverable 3 asks for this explicitly)

A particle has **no velocity**. Its position is sampled directly from a
delta-potential well (Sun, Feng & Xu, 2004):

```
1. mean best        mbest_d = (1/M) * SUM_i pbest_id
2. local attractor  phi ~ U(0,1)
                    p_id = phi * pbest_id + (1 - phi) * gbest_d
3. position update  u ~ U(0,1),  L_id = beta * |mbest_d - x_id|
                    x_id = p_id ± L_id * ln(1/u)      (sign chosen 50/50)
4. contraction      beta(t) = beta_hi - (beta_hi - beta_lo) * t/T
```

`ln(1/u)` gives the heavy tail that lets a particle appear far from the swarm —
the tunnelling behaviour the method is named for. `beta` sets the well width:
large explores, small exploits.

**Decoding:** continuous keys → `argsort` → permutation → capacity-aware
**Split** (Prins 2004, *Computers & OR* 31(12):1985–2002) → routes.

---

## Measured results — including the ones that do not flatter us

6 seeds, 30 customers, 5 vehicles, 350 ms budget, identical instance and matrix
per seed, every plan re-scored by one official evaluation function.
Lower is better.

| Arm | Mean | sd | Best | vs greedy+LS |
|---|---:|---:|---:|---:|
| Greedy construction only | 5,290 | 421 | 4,805 | −27.9% |
| Greedy + local search (no swarm) | 4,135 | 415 | 3,743 | — |
| **QPSO + LS, warm start** | 4,102 | 465 | 3,532 | +0.8% |
| **QPSO + LS, cold start** | **3,681** | 371 | 2,978 | **+11.0%** |
| Random-restart + LS (no swarm pull) | 4,082 | 488 | 3,450 | +1.3% |
| QPSO, local search OFF | 5,222 | 469 | 4,631 | −26.3% |
| OR-Tools (same matrix, same budget) | 3,351 | 239 | 2,942 | +19.0% |

**Attribution**

| Comparison | Contribution |
|---|---:|
| Improvement layer (D → A) | **+21.5%** |
| Warm start (A0 → A) | **−11.4%** |
| Swarm update rule (B → A) | **−0.5%** |
| vs OR-Tools | −22.4% |

### What these numbers actually say

Three findings we report rather than hide:

1. **The local search does most of the work (+21.5%).** The QPSO update rule
   itself contributes about **nothing** (−0.5% against a random-restart control
   using the identical improvement layer). This is precisely the critique
   Sörensen (2015) makes of metaphor-named metaheuristics, and we designed the
   ablation specifically to detect it. Most submissions in this space never run
   the control arm and so cannot know.

2. **Warm-starting the whole swarm is harmful (−11.4%).** Seeding every particle
   from the incumbent makes the local attractor, `gbest` and `mbest` coincide,
   the well width `L = beta*|mbest − x|` collapses, and the swarm cannot leave.
   Fixed by seeding only a small elite; initial planning now uses a cold start.
   Re-planning still warm-starts on purpose — there the objective is **churn**,
   not raw score, and a cold solve re-tasks every driver. The benchmark does not
   measure churn, which is exactly why the two code paths differ.

3. **OR-Tools still beats us on solution quality.** It is given the same matrix,
   the same budget, the same capacity and time-window constraints, and the same
   commitment constraints. We do not claim to beat the state of the art on
   static quality. Our claim is the dynamic, commitment-aware recovery path with
   end-to-end latency accounting.

A 2025 systematic review of this field (Liu, Parkinson & Best, *Smart Cities*
8:206) found that of fifteen peer-reviewed studies, **none** reported end-to-end
timing and **none** applied hypothesis tests or confidence intervals. We report
both.

---

## End-to-end latency

Reported as **event → accepted plan**, decomposed. The travel-time matrix
rebuild is *inside* this number, not excluded from it — the review above found
that data-translation overhead, not the optimiser, dominates total turnaround in
published work.

Typical, 30 stops / 5 vehicles / 3 buckets, three engines racing:

```
freeze_commitments      0.0 ms
fifo_assert            33.1 ms
matrix_rebuild        285.3 ms     <- the real cost, reported not hidden
evaluate_incumbent      0.2 ms
solve                 948.7 ms     <- three engines sequentially
acceptance              0.0 ms
                    ----------
TOTAL                1267.4 ms
```

**Honest status:** the blueprint targeted p95 < 500 ms. The operational path
(matrix + QPSO only, scoped rebuild) lands around **600 ms**; the 1,267 ms above
includes the three-engine solver race, which is a demo and benchmarking feature,
not the production path. **The 500 ms target has not been met yet** and is not
claimed as met.

---

## Acceptance rule

```
CASE 1  incumbent feasible   -> accept only if > 1% better (anti-churn)
CASE 2  incumbent INFEASIBLE -> accept best feasible, no improvement test
CASE 3  nothing feasible     -> hold, list violations, RAISE DISPATCHER ALERT
```

Case 2 exists because after a closure the incumbent may be physically
impossible, and requiring a new plan to beat an impossible one by ε is
undefined. Case 3 exists because a failure that reaches nobody is not handled.

Feasibility is decided by an **independent validator**, never by the solver that
produced the plan, and never by a finite penalty weight.

---

## Layout

```
routepulse/
  graph.py        road network, time-dependent costs, FIFO check, overlays
  costs.py        bucketed travel-time matrix, scoped + full rebuild
  model.py        Instance / Vehicle / Customer / Solution / weights
  validator.py    independent feasibility gate + THE official scorer + churn
  dynamic.py      commitment freeze, events, 3-case acceptance, latency
  solvers/
    heuristics.py greedy insertion (emergency mode) + 2-opt/relocate/swap
    qpso.py       QPSO, random keys, Prins Split, memetic loop
    ortools_baseline.py  fair comparator (same constraints, same budget)
server/           FastAPI + zero-dependency canvas control tower
scripts/bench.py  ablation + benchmark harness
FORMULATION.md    Deliverable 2
```

---

## Known limitations

- Traffic is **simulated**, not live. Free city-scale real-time traffic for an
  Indian city is not obtainable; the time-of-day profile is a plausible model,
  not a measurement.
- p95 < 500 ms is **not yet met** on the racing path (see above).
- The swarm update rule's measured contribution is ~0 at these budgets. QPSO is
  retained as the required quantum-inspired module and reported honestly.
- Emergency-vehicle priority, Simulated Bifurcation and ALNS are designed in the
  blueprint but **not implemented** in this prototype.
- "Quantum-inspired" means classical. No quantum hardware, no quantum speedup.

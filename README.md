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
python scripts/bench.py --seeds 30 --budget 0.35     # the recommended protocol
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

**Real OpenStreetMap Bengaluru network** (6,420 junctions), zone-constrained
instances, **30 seeds** — the full protocol the literature review recommends —
30 customers, 5 vehicles, 350 ms budget, identical instance and matrix per seed,
every plan re-scored by one official evaluation function. Lower is better.
Raw data: `out/bench_30seed.json`.

| Arm | Mean | sd | Best | vs greedy+LS | Wilcoxon *p* |
|---|---:|---:|---:|---:|---:|
| Greedy construction only | 3,786 | 377 | 3,044 | −28.8% | 0.0000 ✓ |
| Greedy + local search (no swarm) | 2,939 | 286 | 2,177 | — | reference |
| **QPSO + LS, warm start** | **2,885** | 274 | 2,177 | **+1.9%** | **0.334 ✗** |
| QPSO + LS, cold start | 2,893 | 232 | 2,459 | +1.6% | 0.339 ✗ |
| Random-restart + LS (no swarm pull) | 2,912 | 278 | 2,177 | +0.9% | 0.689 ✗ |
| QPSO, local search OFF | 3,778 | 370 | 3,044 | −28.5% | 0.0000 ✓ |
| OR-Tools (same matrix, same budget) | **2,570** | 221 | 2,121 | +12.6% | 0.0000 ✓ |

✓ significant at α=0.05 · ✗ not significant · paired Wilcoxon signed-rank against
the greedy+LS reference (arms share instances, so paired is the correct test).

**Attribution**

| Comparison | Contribution | Significant? |
|---|---:|---|
| Improvement layer (D → A) | **+23.6%** | **yes** (p < 0.0001) |
| Swarm layer overall (E → A) | +1.9% | no (p = 0.33) |
| **Swarm update rule (B → A)** | **+0.9%** | **no** (p = 0.69) |
| Warm start (A0 → A) | +0.3% | no (p = 0.34) |
| vs OR-Tools | −12.6% | yes (p < 0.0001) |

### What these numbers actually say

**1. The local search does the work; the swarm does not clear significance.**
Removing local search costs **23.6%** (p < 0.0001). The swarm layer as a whole
is worth **+1.9%** and does not reach significance at 30 seeds (p = 0.33); the
quantum update rule specifically is **+0.9%** against a random-restart control
using the identical improvement layer (p = 0.69).

This is exactly the critique Sörensen (2015) makes of metaphor-named
metaheuristics, and the control arm was built specifically to detect it.

**2. We diagnosed *why*, fixed the structure, and it improved — but not enough.**
The first measurements had the swarm at −0.5%. Tracing the convergence showed the
cause, and it is a property of the *encoding*, not the metaheuristic:

> `gbest` was maintained as a local-search output while particles were scored
> **raw**. A raw random-key decode measures ~1.7× worse than its own refined form
> (mean particle ≈ 4,400 vs gbest 2,655), so **no particle could ever displace
> the incumbent** — the swarm was structurally unable to contribute regardless of
> its update rule or diversity.

Confirmed directly: a diversity-triggered restart fires 2–3 times per run,
measurably re-diversifies the swarm (0.12 → 0.20), and changed the final
objective by **exactly zero**.

The fix was to score like with like — a **Lamarckian step** that improves one
particle in place per generation and rewrites its genotype, so particles and
incumbent live in the same space. That moved the swarm contribution from −0.5%
to **+1.9%**. Real, reproducible, and still not significant.

**3. OR-Tools beats us on solution quality by 12.6% (p < 0.0001)** — given the
same matrix, budget, capacity, time windows and commitment constraints. We do
not claim to beat the state of the art on static quality. The claim is the
dynamic, commitment-aware recovery path with end-to-end latency accounting.

> **What we would tell the panel:** the honest headline is not "our quantum-inspired
> solver wins". It is *"we built the experiment that could prove it didn't, ran it
> at the recommended protocol, found the structural reason, fixed it, and the
> effect is still inside the noise."* That is a result. The alternative — a
> confident 3% claim from 5 unpaired runs — is not.

### Convergence analysis

`python scripts/convergence.py --budget 4.0 --seeds 3 --cold`
→ `out/convergence.png`, `out/convergence.json`

Cold start, real network, 3 seeds, 4 s budget:

| Measure | Value |
|---|---|
| gbest improvement | **+43.6%** (11,693 → 6,600) |
| Swarm diversity | 0.2205 → 0.0505 (**77% collapse**) |
| β (contraction–expansion) | 0.988 → 0.659 |
| Stagnation onset | **iteration 8** of 41 |

Convergence analysis is not the same thing as an anytime curve, and the sponsor
asks for it by name. The useful finding here is the stagnation point: **the
swarm has effectively converged by iteration 8**, so the remaining ~33
iterations of the budget buy almost nothing. That argues for either a shorter
budget or a restart/diversification mechanism after stagnation — a concrete next
step the curve earned.

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

Real Bengaluru network, 6,420 junctions, 30 stops / 5 vehicles / 3 buckets,
10 injected closures. `python scripts/latency.py` · raw: `out/latency.json`.

**Operational path** (single engine — what a dispatcher actually waits for):

| Stage | p50 | p95 |
|---|---:|---:|
| freeze commitments | 0.0 | 0.0 |
| FIFO assert | 2.0 | 4.5 |
| **travel-time matrix rebuild** | 78.4 | 117.9 |
| evaluate incumbent | 0.2 | 0.3 |
| solve | 354.5 | 358.2 |
| acceptance | 0.0 | 0.0 |
| **TOTAL (event → accepted plan)** | **434.4** | **480.9** |

✅ **p95 = 481 ms — meets the 500 ms target.**

**Demo path** (three-engine solver race) — p95 **963 ms**. Reported separately
rather than averaged in, because they are different things and conflating them
would flatter whichever number we chose.

### How it got there — 5,287 ms → 481 ms

The first honest measurement was **p95 = 5,287 ms**, over 10× the target, with a
228-second outlier. Four fixes, in order of payoff:

| Fix | Effect |
|---|---|
| **scipy `csgraph` Dijkstra** instead of pure-Python | matrix 4,456 → 118 ms |
| **Zone-constrained instances** (1.4 km, as the PS describes) + service-area subgraph | 5,287 → 2,520 ms |
| **Scoped FIFO check** — only edges carrying an overlay, not all 16,413 | 400 → 3 ms |
| Shortest-path-tree invalidation | sound scoping for cost increases |

The scipy swap is sound rather than a shortcut: **within one bucket the edge
weights are constant by construction** — a bucket *is* a fixed departure time —
so each bucket is an ordinary static shortest-path problem. The time-dependence
still lives in the bucketing and interpolation. Nothing about the model changed;
only the inner loop.

> **A bug this nearly introduced:** the scipy path does not build predecessor
> trees, so the tree-based "which rows are stale?" check returned *nothing* and
> the matrix silently stopped rebuilding after incidents — fast, and wrong.
> `rows_affected_by()` now returns *all* rows when trees are unavailable.
> Slow-but-right beats fast-but-stale, and with scipy a full rebuild is ~40 ms
> anyway.

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

- Road network is REAL (OpenStreetMap, 6,420 junctions). Delivery stops and traffic are **simulated**, not live. Free city-scale real-time traffic for an
  Indian city is not obtainable; the time-of-day profile is a plausible model,
  not a measurement.
- p95 < 500 ms IS met on the operational path (481 ms). The 3-engine demo race is 963 ms and is reported separately.
- The swarm layer's measured contribution is +1.9% and does NOT reach
  significance at 30 seeds (p = 0.33). QPSO is retained as the required
  quantum-inspired module and its contribution is reported, not assumed.
- Emergency-vehicle priority, Simulated Bifurcation and ALNS are designed in the
  blueprint but **not implemented** in this prototype.
- "Quantum-inspired" means classical. No quantum hardware, no quantum speedup.

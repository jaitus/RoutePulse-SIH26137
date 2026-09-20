# RoutePulse — SIH26137

**Quantum-Inspired Intelligent Traffic Route Optimization in Transportation Systems Using Metaheuristic Optimization**
Egreen Quanta · AICTE Smart India Hackathon 2026 · Quantum Technology Vertical

A live fleet re-optimisation control tower. Plan routes on a real road network,
inject a road closure, a jam or an ambulance, and watch the fleet recover under
a hard time budget — with end-to-end latency, feasibility, energy and solver
comparison all **measured and shown**, including the measurements that do not
flatter us.

---

## Quick start

```bash
cd C:\PROJECTS\SIH26137
pip install -r requirements.txt
python -m uvicorn server.app:app --port 8000
```

Open <http://127.0.0.1:8000>.

The console has two modes:

- **Operations** — the live map. *Plan routes*, pick an incident type, click on a
  coloured route line, then *Re-plan*. Left rail is the fleet, right drawer is
  the acceptance decision, latency waterfall and solver race, bottom strip is the
  event timeline. Scroll to zoom, drag to pan, hover a stop for its ETA.
- **Evidence** — the measured record: the 30-seed ablation with significance
  tests, both adoption gates, convergence, latency percentiles, the Simulated
  Bifurcation study, the S1–S9 scenario suite, energy accounting, and a blunt
  list of what the system is not. Keyboard: `1` and `2` switch modes, `p` plans,
  `r` re-plans.

**No internet required.** No CDN, no map tiles, no external JS, no web fonts.
The road network is drawn on a canvas from our own API and a synthetic grid is
used if the cached Bengaluru graph is absent — the demo cannot be killed by
venue Wi-Fi.

### Reproducing every number on the Evidence page

```bash
python scripts/bench.py --seeds 30 --budget 0.35   # ablation + adoption gates
python scripts/latency.py --trials 20              # end-to-end percentiles
python scripts/convergence.py --budget 4.0 --seeds 3 --cold
python scripts/scenarios.py                        # S1-S9 operational suite
python scripts/sb_eval.py                          # Simulated Bifurcation study
python scripts/energy.py --trials 12               # energy per re-plan
python scripts/security_check.py                   # hardening, against a live server
```

Everything lands in `out/` and is committed. The Evidence view renders those
files and **never computes anything** — if an experiment has not been run, the
panel says MISSING rather than showing a plausible default.

---

## How the five deliverables are met

| # | Deliverable | Where |
|---|---|---|
| 1 | Graph-based Network Model | `routepulse/graph.py` — weighted directed road graph from OpenStreetMap (Overpass), with the **dynamic weight update mechanism**: piecewise-linear time-of-day profiles plus incident and corridor overlays |
| 2 | Mathematical Formulation | `FORMULATION.md` — objective, decision variables, capacity / time-window / **flow conservation** constraints, FIFO condition, acceptance rule, and (§11) the Ising/QUBO reduction |
| 3 | Quantum-Inspired Algorithm Module | `routepulse/solvers/qpso.py` — QPSO with the update rule written out in full; `routepulse/solvers/sb.py` — **Simulated Bifurcation**, the classical limit of a real Kerr-nonlinear parametric oscillator network |
| 4 | Software Platform / Prototype | `server/` — API and UI, network + traffic input, optimised route output, **map visualisation**, offline by construction, CSP-hardened |
| 5 | Demonstration | Bengaluru zone, 30 stops, 5 vehicles, live incidents under **varying traffic conditions**; `scripts/` for experimental results |

Constraint handling, convergence analysis and systematic performance
benchmarking — all three named by the sponsor's Expected Solution paragraph —
are in `routepulse/validator.py`, `scripts/convergence.py` and
`scripts/bench.py` respectively, and all three are on the Evidence page.

---

## The engines

Four solve; one decides.

| Engine | What it is | Status |
|---|---|---|
| **Emergency heuristic** | Greedy insertion + a short local search. Something feasible *immediately*. | always in the race |
| **QPSO + local search** | Deliverable 3's quantum-inspired module. Delta-potential-well sampling over random keys, Prins Split decode, memetic + Lamarckian steps. | always in the race; contribution reported, not assumed |
| **Traffic-Aware ALNS** | Destroy/repair with adaptive operator weights, plus a removal operator that tears out the stops the traffic actually broke. | **adopted** — passed its gate at 7.2%, p < 0.0001 |
| **Simulated Bifurcation** | Genuinely quantum-derived: the equations of motion of a physical Ising machine. | **not adopted** — see §5 |
| **OR-Tools** | Fair external comparator: same matrix, same budget, same constraints. | baseline |

Every candidate is re-scored by **one** evaluation function in
`routepulse/validator.py`. Solvers never report their own numbers, and
feasibility is a hard gate decided by an independent validator — never a finite
penalty weight.

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
instances, **30 seeds** — the protocol the literature review recommends — 30
customers, 5 vehicles, 350 ms budget, identical instance and matrix per seed,
every plan re-scored by one official evaluation function. Lower is better.
Raw data: `out/bench_30seed.json`.

| Arm | Mean | sd | Best | vs greedy+LS | Wilcoxon *p* |
|---|---:|---:|---:|---:|---:|
| Greedy construction only | 3,786 | 377 | 3,044 | −28.8% | <0.0001 ✓ |
| Greedy + local search (no swarm) | 2,939 | 286 | 2,177 | — | reference |
| **Greedy + Traffic-Aware ALNS** | **2,726** | 279 | 2,255 | **+7.2%** | **<0.0001 ✓** |
| Greedy + LS + Simulated Bifurcation | 2,939 | 286 | 2,177 | +0.0% | identical |
| ↳ re-sequencing only: 2-opt | 3,670 | 366 | 2,938 | −24.9% | <0.0001 ✓ |
| ↳ re-sequencing only: Simulated Bifurcation | 3,779 | 379 | 3,044 | −28.6% | <0.0001 ✓ |
| QPSO + LS, warm start | 2,929 | 293 | 2,177 | +0.3% | **0.98 ✗** |
| QPSO + LS, cold start | 2,903 | 277 | 2,459 | +1.2% | 0.48 ✗ |
| Random-restart + LS (no swarm pull) | 2,910 | 274 | 2,177 | +1.0% | 0.64 ✗ |
| QPSO, local search OFF | 3,778 | 370 | 3,044 | −28.5% | <0.0001 ✓ |
| OR-Tools (same matrix, same budget) | **2,572** | 218 | 2,121 | +12.5% | <0.0001 ✓ |

✓ significant at α=0.05 · ✗ not significant · paired Wilcoxon signed-rank against
the greedy+LS reference (arms share instances, so paired is the correct test).

### What these numbers actually say

**1. The improvement layer does the work; the swarm does not clear
significance.** Removing local search costs **22.5%** (p < 0.0001). The swarm
layer as a whole is worth **+0.3%** and does not reach significance at 30 seeds
(p = 0.98); the quantum update rule specifically loses to a random-restart
control using the identical improvement layer. This is exactly the critique
Sörensen (2015) makes of metaphor-named metaheuristics, and the control arm was
built specifically to detect it.

**2. We diagnosed *why*, fixed the structure, and it improved — but not enough.**
The first measurements had the swarm at −0.5%. Tracing the convergence showed the
cause, and it is a property of the *encoding*, not the metaheuristic:

> `gbest` was maintained as a local-search output while particles were scored
> **raw**. A raw random-key decode measures ~1.7× worse than its own refined form,
> so **no particle could ever displace the incumbent** — the swarm was
> structurally unable to contribute regardless of its update rule or diversity.

Confirmed directly: a diversity-triggered restart fires 2–3 times per run,
measurably re-diversifies the swarm (0.12 → 0.20), and changed the final
objective by **exactly zero**. The fix — a **Lamarckian step** that improves one
particle in place per generation and rewrites its genotype — moved the swarm
contribution from −0.5% into the noise band it still occupies. Real, reproducible,
and still not significant.

**3. OR-Tools beats us on solution quality by 12.5% (p < 0.0001)** — given the
same matrix, budget, capacity, time windows and commitment constraints. We do
not claim to beat the state of the art on static quality. The claim is the
dynamic, commitment-aware recovery path with end-to-end latency accounting.

> **What we would tell the panel:** the honest headline is not "our
> quantum-inspired solver wins". It is *"we built the experiment that could
> prove it didn't, ran it at the recommended protocol, found the structural
> reason, fixed what could be fixed, and reported the rest."* Two engines were
> then put through the same gate — one passed and shipped, one failed and was
> kept as a documented negative result.

---

## Adoption gate 1 — Traffic-Aware ALNS: **ADOPTED**

Appendix A of the blueprint proposed replacing the 2-opt/relocate/swap
improvement layer with Adaptive Large Neighbourhood Search (Ropke & Pisinger
2006), and required an explicit gate before adoption. It cleared it:
**7.2% better, p < 0.0001**, same greedy start, same wall-clock budget, 30
paired seeds. It is now the default improvement layer for initial planning and
runs in every re-plan race.

`routepulse/solvers/alns.py`. Four destroy operators — random, worst-removal,
Shaw relatedness, and a **traffic-aware** one that ranks stops by how far their
inbound leg's realised travel time has diverged from free flow — two repair
operators (greedy, regret-2), Ropke & Pisinger's published reward schedule, and
simulated-annealing acceptance. The traffic-aware operator is the delta over
textbook ALNS: after an incident it tears out the part of the plan the incident
broke, not a random part.

The published reward constants are used unchanged rather than tuned on our own
instances, so the arm is a fair test of the published method.

---

## Adoption gate 2 — Simulated Bifurcation: **NOT ADOPTED, and that is the finding**

QPSO is quantum-inspired *by analogy*. A panel is entitled to ask "in what sense
is any of this quantum?", and the honest answer for QPSO is "by metaphor".

Simulated Bifurcation is a different kind of object. Goto (2016) showed that a
network of Kerr-nonlinear parametric oscillators driven through its bifurcation
point relaxes into the ground state of an Ising Hamiltonian; Goto, Tatsumura &
Dixon (2019, *Sci. Adv.* 5:eaav2372) showed that simulating the **classical**
Hamiltonian equations of that same network solves the Ising problem on ordinary
hardware. `routepulse/solvers/sb.py` integrates those equations of motion
(discrete variant, Goto et al. 2021) with symplectic Euler and inelastic walls.

Three questions, asked in order (`python scripts/sb_eval.py` → `out/sb.json`):

**1. Is the solver correct?** Against exhaustive enumeration on random Ising
instances: **20/20 exact ground states at 10, 12 and 14 spins**, mean gap 0.000%.
Nothing below is a bug in the solver.

**2. Is the embedding sound?** The tour is embedded as $m^2$ position-indexed
spins (Lucas 2014 §7.2). The constraint penalty $A$, in units of the largest leg:

| A | valid tours | gap vs optimal |
|---|---|---:|
| 0.5× max leg | **4/20** | +7.9% |
| 1.0× max leg | 20/20 | +1.2% |
| 2.0× max leg | 20/20 | +6.0% |
| 4.0× max leg | 20/20 | +10.9% |

The classic QUBO objection, measured: too weak a penalty and the spin
configuration is not a tour at all; too strong and it drowns the objective it
exists to protect.

**3. Is it worth anything here?** On 24 real routes (mean 9.75 stops, ~97 spins),
given the same routes as 2-opt and asked only to reorder them:

| | 2-opt | Simulated Bifurcation |
|---|---:|---:|
| wins, head to head | **22** | 1 (1 tie) |
| mean time per route | **1.75 ms** | 140 ms |
| gap vs exact optimum, **travel only** | +2.2% | +6.0% |
| gap vs exact optimum, **full objective** | **0.0%** | +257% → **+10.8%** with the time-window field |

That decomposition is the result. **SB is competitive on the objective its
Hamiltonian actually encodes and catastrophic on the objective the fleet is
actually judged by**, because lateness depends on cumulative arrival time — a
prefix sum over the permutation — and no quadratic form in $x_{i,p}$ equals a
prefix sum. Pricing deadline order into the *local field* (the one degree of
freedom the Ising form leaves, `position_field()` in `sb.py`) recovers most of
the damage: measured over the deadline-order weight λ,

| λ | cost vs the greedy order it replaced |
|---|---:|
| 0 | **+1428%** |
| 0.1 | +454% |
| 0.2 | +23% |
| **0.5** | **+19%** ← default |
| 2.0 | +33% |

Read the first row carefully. Without the bias the Ising model's "best" tour
costs fourteen times what a greedy one does — not because the solver failed, but
because the Hamiltonian was missing the term that dominates the real cost.

**Verdict:** a correctly implemented, independently validated Ising machine is
beaten by 2-opt on this problem by 3.0% at 80× the time (p < 0.0001). It stays in
the repository as a selectable engine and as the honest answer to whether
quantum-derived optimisation is ready for time-windowed fleet routing today. It
is not, and the reason is the embedding, not the hardware.

---

## End-to-end latency

Reported as **event → accepted plan**, decomposed. The travel-time matrix
rebuild is *inside* this number — the review below found that data-translation
overhead, not the optimiser, dominates total turnaround in published work.

Real Bengaluru network, 6,420 junctions, 30 stops / 5 vehicles / 3 buckets,
20 injected closures. `python scripts/latency.py` · raw: `out/latency.json`.

| Path | p50 | p95 | worst | 500 ms target |
|---|---:|---:|---:|---|
| **operational — ALNS only** | 447 | **462** | 486 | ✅ met |
| operational — QPSO only | 451 | 484 | 498 | ✅ met |
| demo — 4-engine race | 1,249 | 1,466 | 3,045 | reported separately |

Operational p95 by stage (ALNS): freeze 0.0 · FIFO assert 10.9 · **matrix rebuild
100.0** · evaluate incumbent 0.3 · solve 357.0 · acceptance 0.0.

A dispatcher waits on **one** engine. The race exists so a judge can watch four
engines compete on identical inputs, and it is never averaged in with the
operational number — conflating them would flatter whichever number we chose.

### How it got there — 5,287 ms → 462 ms

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
so each bucket is an ordinary static shortest-path problem. Nothing about the
model changed; only the inner loop.

> **A bug this nearly introduced:** the scipy path does not build predecessor
> trees, so the tree-based "which rows are stale?" check returned *nothing* and
> the matrix silently stopped rebuilding after incidents — fast, and wrong.
> `rows_affected_by()` now returns *all* rows when trees are unavailable.

---

## Energy per re-plan

The 2025 systematic review names energy reporting as a gap in this field. It
matters here because a recovery engine is not run once — it runs on every
incident, all day, at every depot.

`python scripts/energy.py` → `out/energy.json`. Measured: CPU seconds and wall
time. Energy: from the **ACPI battery discharge sensor** when the machine
exposes one, otherwise **modelled** as `cpu_seconds × 15 W per busy core` with
the coefficient printed next to every figure it produced. The run below was on
mains power, so it is the modelled path and says so.

| Configuration | wall ms | CPU ms | mWh / re-plan | kWh / year¹ | kg CO₂e / year² |
|---|---:|---:|---:|---:|---:|
| Emergency heuristic only | 153 | 151 | 0.629 | 0.092 | 0.066 |
| **Operational — ALNS** | 448 | 445 | 1.855 | 0.271 | 0.194 |
| Operational — QPSO | 450 | 443 | 1.845 | 0.269 | 0.193 |
| Demo race — 4 engines | 1,226 | 1,216 | 5.067 | 0.740 | 0.530 |

¹ at 400 re-plans/day — one every ~2 minutes across a 14-hour delivery window.
² CEA all-India grid average, 0.716 kg CO₂e/kWh, 2023-24.

**For scale:** the operational path uses **0.27 kWh a year**. A single 100 W depot
floodlight burns four times that in one ten-hour shift. The honest reading is
that the optimiser's own energy is negligible beside the diesel it saves — and
that is only a credible statement because it was measured rather than assumed
away.

---

## Emergency-vehicle priority

An ambulance is **not a vehicle in the VRP**. Delivery is a capacitated,
time-windowed, multi-vehicle routing problem; the ambulance is a single-origin,
single-destination time-dependent shortest path under a different cost model and
a tighter deadline. They are solved separately and coupled through the cost
layer — which needs no new machinery, because a **green corridor is structurally
identical to a congestion incident** and the recovery engine already handles one.

Measured on the real network (`scripts/scenarios.py --only S8 S9`):

| | |
|---|---|
| Ambulance, under priority | **7.1 min** |
| Ambulance, without priority | 11.9 min |
| **Time saved** | **4.8 min** |
| **Cost of priority to the delivery fleet** | **+345.7** (3,030.2 → 3,375.9) |
| Green corridor | 112 edges, 25-minute window |
| Emergency path latency | **66.2 ms** (budget 200 ms) |

**Priority is not teleportation.** One-ways and physical closures are still
respected — S9 closes six edges on the ambulance's *own* route and verifies it
reroutes around them (41 → 58 nodes, **0 closed edges used**, +0.5 min detour).

**Priority is not free, and we report both sides.** The corridor that speeds the
ambulance up slows the fleet down. Most systems show only the first number.

Two further details that matter:

- The corridor is an **exogenous forecast**, not a measurement — it applies only
  to the forward window `t > now`. Summing it with observed slowdown for the
  same instant would double-count one physical effect.
- **Commitment beats corridor**: a vehicle already en route to a stop inside the
  corridor completes that leg first.

---

## Scenario suite — S1 to S9

`python scripts/scenarios.py` → `out/scenarios.json`

**9/9 pass with their condition actually exercised.** That qualifier is the
point: the harness reports a **VACUOUS** verdict when a scenario passes without
triggering what it claims to test, and it caught three of those during
development — S1 and S5 were closing *zero* edges (a connectivity guard was
reopening everything) and S9's first version closed roads that missed the
ambulance's path entirely. All three would have reported green having tested
nothing.

---

## Convergence analysis

`python scripts/convergence.py --budget 4.0 --seeds 3 --cold`
→ `out/convergence.png`, `out/convergence.json`

| Measure | Value |
|---|---|
| gbest improvement | **+43.6%** (11,693 → 6,600) |
| Swarm diversity | 0.2205 → 0.0505 (**77% collapse**) |
| β (contraction–expansion) | 0.988 → 0.659 |
| Stagnation onset | **iteration 8** of 41 |

Convergence analysis is not the same thing as an anytime curve, and the sponsor
asks for it by name. The useful finding is the stagnation point: the swarm has
effectively converged by iteration 8, so the remaining ~33 iterations of the
budget buy almost nothing. That argues for a shorter budget or a better
diversification mechanism — and it is part of why ALNS, which keeps finding new
bests throughout its budget, won its gate.

A 2025 systematic review of this field (Liu, Parkinson & Best, *Smart Cities*
8:206) found that of fifteen peer-reviewed studies, **none** reported end-to-end
timing and **none** applied hypothesis tests or confidence intervals. We report
both.

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

## Security posture

`python scripts/security_check.py` exercises every claim below against a running
server and exits non-zero if any fails. **31/31 pass** with a key configured,
28/28 in open mode.

- **Every numeric input is bounded at the schema.** An unbounded `n` is a denial
  of service in one request: instance size drives an O(n²) matrix build.
- **Coordinates are bounded to the served extract.** A lat/lon anywhere on Earth
  would snap to the nearest Bengaluru node and inject an incident nobody asked
  for.
- **API key on mutating endpoints** when `ROUTEPULSE_API_KEY` is set. Unset, the
  server is open and `/api/health` *says so* — silent "security" is worse than
  none, because it is believed.
- **Per-client token bucket** on the expensive endpoints. A solve is hundreds of
  milliseconds of CPU; unmetered, a loop of them is the whole box. Verified to
  fire at 40 solves/minute.
- **One solve at a time, behind a lock.** The engine holds mutable state; two
  concurrent re-plans would interleave writes and produce a plan that is a
  mixture of two events.
- **Errors return a generic message.** Tracebacks name paths, versions and
  internal structure.
- **CSP of `'self'` with no `'unsafe-inline'`**, plus nosniff, DENY framing,
  no-referrer and a locked-down permissions policy. This is why the UI ships as
  separate `.css`/`.js` files with no inline script and no CDN.
- **The road graph is loaded with `json`, never `pickle`.** A pickle load is
  arbitrary code execution, and a cache file is exactly the sort of thing that
  gets copied between machines.
- Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are not served.

---

## Layout

```
routepulse/
  graph.py        road network, time-dependent costs, FIFO check, overlays
  costs.py        bucketed travel-time matrix, scoped + full rebuild
  model.py        Instance / Vehicle / Customer / Solution / weights
  validator.py    independent feasibility gate + THE official scorer + churn
  dynamic.py      commitment freeze, events, 3-case acceptance, latency, energy
  emergency.py    ambulance dispatch, hospital selection, green corridor
  energy.py       CPU + battery-sensor energy accounting
  solvers/
    heuristics.py greedy insertion (emergency mode) + 2-opt/relocate/swap
    qpso.py       QPSO, random keys, Prins Split, memetic loop
    alns.py       Traffic-Aware ALNS  [adopted]
    sb.py         Simulated Bifurcation over an Ising embedding  [not adopted]
    ortools_baseline.py  fair comparator (same constraints, same budget)
server/           FastAPI + zero-dependency canvas control tower
  static/         index.html + app.css + app.js, no external assets
scripts/          bench, latency, convergence, scenarios, sb_eval, energy,
                  security_check
FORMULATION.md    Deliverable 2, including the Ising reduction
DEMO.md           the five-minute walkthrough
```

---

## Known limitations

- **The road network is real; the demand is not.** OpenStreetMap Bengaluru,
  6,420 junctions. Delivery stops are synthetic and the traffic profile is a
  plausible hand-authored time-of-day model, **not measured data**. Free
  city-scale real-time traffic for an Indian city is not obtainable.
- **OR-Tools beats us on static solution quality by 12.5%** (p < 0.0001). We do
  not claim otherwise. The claim is the dynamic, commitment-aware recovery path
  with end-to-end latency accounting.
- **The swarm layer's measured contribution is +0.3% and not significant**
  (p = 0.98). QPSO is retained as the required quantum-inspired module and its
  contribution is reported, not assumed.
- **"Quantum-inspired" means classical.** No quantum hardware, no quantum
  speedup. Simulated Bifurcation is the classical limit of a quantum system —
  a stronger claim than metaphor — and it still lost.
- **The emergency layer simulates traffic interaction, not EMS dispatch.** Crew
  availability, clinical triage and hospital diversion are out of scope.
  Hospital locations are synthetic and labelled as such.
- **The server is single-tenant.** One engine in module state, so two browsers
  share one fleet. Correct for a control tower demo, wrong for a product, and
  written down rather than discovered later.
- **Not built:** SUMO microsimulation. The time-of-day curve stands in for it.

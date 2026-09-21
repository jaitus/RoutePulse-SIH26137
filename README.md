# RoutePulse — SIH26137

**Quantum-Inspired Intelligent Traffic Route Optimization in Transportation Systems Using Metaheuristic Optimization**
Egreen Quanta · AICTE Smart India Hackathon 2026 · Quantum Technology Vertical

A live fleet re-optimisation control sheet. Plan routes on a real road network,
inject a road closure, a jam or an ambulance, let the clock run, and watch the
fleet recover under a hard deadline — with end-to-end latency, feasibility,
congestion exposure, energy and solver comparison all **measured and shown**,
including the measurements that do not flatter us.

---

## Quick start

```bash
cd C:\PROJECTS\SIH26137
pip install -r requirements.txt
python -m uvicorn server.app:app --port 8000
```

Open <http://127.0.0.1:8000>.

The console has two modes:

- **Operations** — the live map, drawn as a transport authority's plan sheet.
  *Plan routes* → pick an incident type → **click on a coloured route line** →
  *Re-plan*. **Advance clock** lets the fleet actually drive: stops whose ETA
  has passed are delivered and leave the problem, vehicles move to where they
  are, and the next re-plan starts from there. Dispatching an ambulance is
  **one action** — it computes the priority route, publishes a per-edge green
  corridor and recovers the fleet in a single request.
- **Evidence** — the measured record, rendered straight from `out/`: the
  30-seed ablation with significance tests, both adoption gates, convergence,
  latency percentiles *and their scaling*, the Simulated Bifurcation study, the
  S1–S9 scenario suite, energy accounting, and a blunt list of what the system
  is not. Keyboard: `1`/`2` switch modes, `p` plans, `r` re-plans.

Both views carry a print stylesheet; Ctrl+P produces a usable hard copy.

**No internet required.** No CDN, no map tiles, no external JS, no web fonts.

### Reproducing every number

```bash
python scripts/run_tests.py                        # unit suite -> out/test_summary.json
python scripts/bench.py --seeds 30 --budget 0.35   # ablation + adoption gates
python scripts/latency.py --trials 20 --scale 30,60,100
python scripts/convergence.py --budget 4.0 --seeds 3 --cold
python scripts/scenarios.py                        # S1-S9
python scripts/oracles.py                          # exact VRP + exact TD path
python scripts/sb_eval.py                          # Simulated Bifurcation study
python scripts/energy.py --trials 12
python scripts/dynamic_arm.py                      # QPSO vs PSO on recovery
python scripts/security_check.py --rate            # needs a live server
python scripts/freeze_env.py --final               # hash the evidence package
python scripts/report.py                           # regenerate the status report
```

Everything lands in `out/` and is committed. `--final` copies the evidence set
into `out/final/` with a SHA-256 per file and the environment it was produced
in. The Evidence view renders those files and **never computes anything** — a
missing experiment shows MISSING rather than a plausible default.

---

## How the five deliverables are met

| # | Deliverable | Where |
|---|---|---|
| 1 | Graph-based Network Model | `routepulse/graph.py` — weighted directed road graph from OpenStreetMap, with the **dynamic weight update mechanism**: piecewise-linear time-of-day profile, a hard closure set, and timed soft overlays that compose deterministically |
| 2 | Mathematical Formulation | `FORMULATION.md` — objective, decision variables, capacity / time-window / **flow conservation** constraints, FIFO condition, acceptance rule, §11 the Ising reduction, §12 the congestion-exposure definition |
| 3 | Quantum-Inspired Algorithm Module | `routepulse/solvers/qpso.py` — QPSO with the update rule written out in full **and a classical PSO control sharing the identical decoder**; `routepulse/solvers/sb.py` — Simulated Bifurcation, the classical limit of a real Kerr-nonlinear oscillator network |
| 4 | Software Platform / Prototype | `server/` — API and UI, network + traffic input, optimised route output, **map visualisation**, offline by construction, CSP-hardened |
| 5 | Demonstration | Bengaluru zone under varying traffic; `scripts/` for experimental results; `tests/` for correctness |

Constraint handling, convergence analysis and systematic performance
benchmarking — all three named by the sponsor's Expected Solution paragraph —
are in `routepulse/validator.py`, `scripts/convergence.py` and
`scripts/bench.py`, and all three are on the Evidence page.

---

## Architecture — two vehicle classes, one cost layer

```
            delivery fleet                    ambulance
        dynamic CVRP / VRPTW          single-origin TD shortest path
                  \                            /
                   \                          /
                    ----- shared cost layer -----
                 time-of-day profile · closures · overlays
                                 |
                      ETA invalidation + re-plan
                                 |
              four engines race under ONE global deadline
          emergency heuristic · QPSO · Traffic-Aware ALNS · OR-Tools
                                 |
                      independent feasibility gate
                                 |
                   three-case acceptance → accepted plan
```

**The ambulance is not a vehicle in the VRP.** It is a different problem —
single-origin, single-destination, time-dependent shortest path under an
emergency cost model — solved separately and coupled through the cost layer.
Its predicted road occupancy becomes a temporary cost increase on normal-vehicle
routing, and the delivery optimiser reacts to that, exactly as it reacts to any
other incident.

**One dispatch, one matrix rebuild.** Dispatch used to refresh the travel-time
matrix so it could price the corridor, and then the recovery refreshed it again
moments later — two O(n² × buckets) rebuilds inside one user action. Dispatch now
records the pre-corridor incumbent and the recovery path performs the single
authoritative refresh, then settles the price of priority against that snapshot.

**The interaction is measured, not assumed.** A corridor through streets nobody
was using, at a time nobody was there, costs the fleet nothing — and the system
reports that rather than printing `+0.0` as though it were a finding. Every
dispatch reports how many delivery legs actually crossed the corridor inside
its per-edge window, and which vehicles. S8 chooses its scene deterministically
so the interaction is real, and fails if the recorded cost of priority is zero
while the scenario claims an interaction.

**A short-lived corridor needs its own matrix sample.** A green corridor is warm
for ten or fifteen minutes; the production matrix samples at 0, 1 h, 4 h, 9 h and
14 h. Per-edge windows made the corridor physically accurate and simultaneously
invisible to the planner's cost model. The matrix therefore samples the
corridor's own window while one is live — one extra bucket, inserted
incrementally, during an emergency only.

**QPSO and ALNS are two separate engines, not a hybrid chain.** A chained
QPSO → ALNS arm was measured against both parents on the same budget and does
not beat ALNS alone, so nothing in this project describes it as a hybrid. That
is a measurement, not a preference — see the Evidence page.

### The engines

| Engine | What it is | Status |
|---|---|---|
| **Emergency heuristic** | Greedy insertion + short local search. Something feasible *immediately*. | always in the race |
| **QPSO + local search** | Deliverable 3's quantum-inspired module. Delta-potential-well sampling over random keys, Prins Split decode, memetic + Lamarckian steps. | in the race; contribution reported, not assumed |
| **Classical PSO** | Same encoding, decoder, improvement layer, restarts and budget. Only the position-update line differs. | control arm |
| **Traffic-Aware ALNS** | Six destroy operators — random, worst, Shaw, traffic-aware, **event-biased**, **string** — two repair operators, published reward schedule, weights persisted per event type. | **adopted** — passed its gate |
| **Simulated Bifurcation** | Genuinely quantum-derived: the equations of motion of a physical Ising machine. | **not adopted** — kept as a documented negative result |
| **OR-Tools** | Fair external comparator: same matrix, same budget, same constraints. | baseline |

Every candidate is re-scored by **one** evaluation function in
`routepulse/validator.py`. Solvers never report their own numbers, and
feasibility is a hard gate decided by an independent validator — never a finite
penalty weight.

---

## The cost layer

### Closures and overlays are different things

A physical closure and a soft slowdown do not share a slot. `closed` is a set —
hard, no magnitude, cleared only by an explicit reopen. `overlays` is a list of
timed multipliers per edge whose composition is the product of the active ones.
A congestion event landing on a closed road leaves the road closed.

*This was a real bug.* Both lived in one multiplier map where a closure stored
infinity, so a later congestion write overwrote the infinity and **reopened a
physically closed road**, with nothing reporting it.

### The green corridor is per-edge

Each corridor edge carries the window the ambulance is actually predicted to be
on it, derived by walking its path at the emergency cost model and padding for
traffic clearing ahead and re-forming behind. A delivery vehicle crossing the
far end of a corridor twenty minutes after the ambulance has passed pays
nothing; under the old route-level window it paid in full.

### One clock

`Engine.now` is the single origin, in seconds from the start of the declared
horizon (**08:00–22:00**, `graph.HORIZON_SECONDS`). Every event timestamp,
corridor window, vehicle availability and ETA uses it. Previously there were
three: dispatch defaulted to `now=0.0`, the UI stamped wall-clock `time.time()`,
and the planner horizon was relative simulation time.

### The travel-time matrix is an approximation, and here is its error

The matrix samples edge weights at *k* departure times and interpolates;
`graph.dijkstra_tt` evaluates every edge at its real accumulated departure time
and is kept as the oracle. Measured against it, with buckets placed **where the
traffic curve bends** rather than uniformly:

| buckets | mean abs error | rebuild | |
|---|---:|---:|---|
| 3, uniform | 10.8% | ~70 ms | the old default |
| 5, profile-aligned | **4.5%** | ~130 ms | **production** |
| 6, profile-aligned | 1.7% | ~200 ms | breaks the 500 ms deadline |

Uniform spacing over a 14-hour horizon put buckets at 08:00, 15:00 and 22:00 —
straddling both rush-hour peaks. That error was larger than any solver
improvement this benchmark has ever measured, sitting underneath every number in
it. Six buckets is the most accurate and pushes the operational p95 past target;
five cuts the old error by 2.4× and holds the deadline, so the solver budget
went from 350 ms to 250 ms to pay for it. An error in the cost model is worse
than slightly less search, because every downstream number inherits it.

### Cache invalidation works in both directions

After a cost **increase** a matrix row is stale only if a changed edge is in its
shortest-path tree, and the scipy build now keeps predecessor arrays so that is
answerable. After a cost **decrease** — a reopened road, a lifted jam, an
expired corridor — a newly cheaper path need never have been in the old tree, so
the engine forces a full rebuild. A sampling oracle in `tests/test_matrix.py`
compares the cached matrix against a full recompute in both directions and fails
on any mismatch.

---

## The simulation contract — stated once, not fudged

RoutePulse does **not** detect ambulances and does **not** integrate a live 112
feed. It **consumes** an external emergency feed. In this prototype that feed is
a mock API a demo operator or script drives:

| Endpoint | Purpose |
|---|---|
| `GET /api/mock/ambulances` | unit states |
| `POST /api/mock/ambulance/telemetry` | publish a position |
| `POST /api/mock/ambulance/complete` | end the call, expire the corridor |

In a deployment the same endpoints would be fed by an authorised GPS/AVL or
dispatch-system integration.

**SUMO / TraCI is not part of this build and is not claimed anywhere.** The
blueprint named it as the simulation layer; it was never implemented. Shipping a
claim nothing backs is worse than shipping a smaller honest one, so it is
removed rather than left ambiguous. `scripts/freeze_env.py` records whether SUMO
is on PATH; it is not, and nothing depends on it.

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
All three are exercised by `tests/test_acceptance.py`, and **S1 now drives a
deterministic closure that genuinely makes the incumbent infeasible** — it used
to pass on Case 1 while claiming to test Case 2.

---

## Vehicle state advances

`POST /api/advance` moves the simulation clock and lets the fleet drive: stops
whose planned arrival has passed are **served** and leave the instance, each
vehicle's position becomes its last completed stop, its earliest availability
becomes when it finished there, and the matrix is rebuilt over the remaining
node set. Re-planning then starts from where the fleet is.

Until this existed, "dynamic re-planning" always restarted from the depot with
the full customer list: the cost layer was dynamic and the vehicles were not.

---

## Measured results

The full record is on the **Evidence** tab and in `out/`. The headline figures,
with the file each comes from:

| | | source |
|---|---|---|
| Traffic-Aware ALNS vs greedy + local search | **+9.7%**, 95% CI [+7.0%, +12.5%], p < 0.0001 → **adopted** | `out/bench_30seed.json` |
| **QPSO vs classical PSO** | **−0.9%**, 95% CI [−2.9%, +1.1%], p = 0.95 — **indistinguishable**, and the interval says so as well as the test | `out/bench_30seed.json` |
| QPSO + LS vs greedy + local search | +1.2%, p = 0.033 by the rank test — but the 95% CI [−0.5%, +3.0%] **includes zero** | `out/bench_30seed.json` |
| QPSO → ALNS chained hybrid | −2.5% against ALNS alone, p = 0.045 — significantly **worse**; two engines, not a hybrid | `out/bench_30seed.json` |
| ALNS vs OR-Tools | OR-Tools still ahead by 4.1%, p < 0.0001 (it was ~13% before ALNS) | `out/bench_30seed.json` |
| **QPSO vs classical PSO on the DYNAMIC recovery problem** | same conclusion as the static benchmark | `out/dynamic_arm.json` |
| Gap to the **true optimum** on exhaustively enumerable instances | measured by full enumeration, per solver | `out/oracles.json` |
| Travel-time matrix error vs exact TD Dijkstra | 4.5% mean absolute at the production setting | `out/oracles.json` |
| Operational latency | meets 500 ms p95 at 30 and 60 stops, **does not at 100** | `out/latency.json` |
| Scenarios S1–S9 | 9/9 with every condition exercised; S1 on Case 2; S5 and S8 report functional and performance verdicts separately | `out/scenarios.json` |
| Ambulance interaction | S8 picks its scene deterministically and **fails** if the corridor does not measurably touch the fleet | `out/scenarios.json` |
| Simulated Bifurcation | exact Ising ground states, and still beaten by 2-opt | `out/sb.json` |
| Energy per re-plan | milliwatt-hours; negligible beside the diesel saved | `out/energy.json` |
| Security suite | passes open and API-key mode, rate limiter exercised to 429 | `out/security.json` |
| Unit suite | count measured, never typed | `out/test_summary.json` |

`python scripts/report.py` regenerates a standalone status report from these
files. Every figure in it is read at generation time and carries its source;
none is typed.

### What these numbers say

**1. The improvement layer does the work, and the *quantum* part does nothing
distinguishable.** Removing local search costs ~23%. The decisive comparison is
**QPSO against a classical PSO control: −0.9%, 95% CI [−2.9%, +1.1%],
p = 0.95.** Identical encoding, decoder, improvement layer, restart logic and
budget; only the line that moves a particle differs. A random-restart arm can
tell you whether having a population helps at all; only the PSO control can tell
you whether the *quantum-inspired* rule does, and it does not. This is the
critique Sörensen (2015) makes of metaphor-named metaheuristics, and the control
arm was built to detect it.

The confidence intervals earn their place on a second row. QPSO + LS beats
greedy + local search by +1.2% at p = 0.033 — significant by the rank test —
while the 95% interval on the mean paired difference runs from −0.5% to +3.0%
and **includes zero**. Both statements are true: the ranks move consistently,
the magnitude does not exclude no-effect. Reporting only the p-value would have
turned a small, fragile effect into a clean claim.

**2. ALNS earned its place and SB did not.** Both were built from the blueprint
and put through the same gate: beat the existing improvement layer from the same
start, on the same budget, over 30 paired seeds. One passed and shipped. One
failed and was kept, because *why* it failed is the most interesting result in
the project.

**3. OR-Tools is still ahead on static quality — by 4.1%, down from ~13%.**
Adopting ALNS closed more than half the gap and we still do not claim to have
closed it. The claim is the dynamic, commitment-aware recovery path with
end-to-end latency accounting — plus an absolute one: on instances small enough
to enumerate exhaustively, ALNS, QPSO and PSO all reach the true optimum while
greedy + local search does not.

---

## Adoption gate 1 — Traffic-Aware ALNS: **ADOPTED**

`routepulse/solvers/alns.py`. Ropke & Pisinger (2006) with the published reward
schedule left untuned, plus two operators the textbook does not have:

- **Event-biased removal** — takes the edges the *current* incident actually
  touched and removes the stops whose own path crosses them. After an incident
  that is the part of the plan that is wrong; everything else is still fine.
- **String removal** — tears out a contiguous run so the repair can re-thread a
  leg, which the point-wise operators cannot do.
- **Traffic-aware removal** — ranks stops by how far their inbound leg has
  diverged from free flow.

Operator weights **persist across re-plans, keyed by event type** (closure /
congestion / ambulance / initial), so a depot stops relearning the same lesson
on every incident. A closure and an ambulance corridor are different problems
and do not share a prior.

---

## Adoption gate 2 — Simulated Bifurcation: **NOT ADOPTED, and that is the finding**

QPSO is quantum-inspired *by analogy*. Simulated Bifurcation is the classical
limit of a real system: Goto (2016) showed a network of Kerr-nonlinear
parametric oscillators driven through its bifurcation point relaxes into the
ground state of an Ising Hamiltonian, and Goto, Tatsumura & Dixon (2019,
*Sci. Adv.* 5:eaav2372) showed simulating the **classical** equations of that
network solves the Ising problem on ordinary hardware.

Three questions, asked in order (`python scripts/sb_eval.py`):

1. **Is the solver correct?** Exact ground states against exhaustive
   enumeration at 10, 12 and 14 spins. Nothing below is a bug in the solver.
2. **Is the embedding sound?** The constraint penalty is calibrated and the
   point where decodes stop being valid tours is measured, not assumed.
3. **Is it worth anything here?** It is competitive on the objective its
   Hamiltonian encodes — travel time — and catastrophic on the objective the
   fleet is judged by, because lateness depends on cumulative arrival time and
   no quadratic form in the position-indexed variables equals a prefix sum.
   Pricing deadline order into the *local field* — the one degree of freedom the
   Ising form leaves — recovers most of the damage. It still loses to 2-opt.

**Verdict:** a correctly implemented, independently validated Ising machine is
beaten by 2-opt on this problem. That is a result about the *embedding*, not the
hardware, and it is the honest answer to whether quantum-derived optimisation is
ready for time-windowed fleet routing today.

---

## Security posture

`python scripts/security_check.py` exercises every claim below against a
running server, writes the result to `out/security.json`, and exits non-zero if
any check fails.

- **Every numeric input is bounded at the schema**, and request validation now
  precedes state validation — a malformed request is 422 whether or not the
  server holds a plan.
- **`GET /api/boot` is read-only.** It used to take n/k/seed and rebuild the
  entire simulation on an unauthenticated GET. Rebuilding is `POST /api/reset`,
  behind the key.
- **Coordinates are bounded to the served extract.**
- **API key on mutating endpoints** when `ROUTEPULSE_API_KEY` is set. Unset, the
  server is open and `/api/health` *says so*.
- **Per-client token bucket** on the expensive endpoints, verified to fire.
- **One solve at a time, behind a lock.**
- **Errors return a generic message**; tracebacks name paths and versions.
- **CSP of `'self'` with no `'unsafe-inline'`**, plus nosniff, DENY framing and
  a locked-down permissions policy.
- **The road graph is loaded with `json`, never `pickle`.**
- Interactive API docs are not served.

---

## Tests

`python scripts/run_tests.py` runs the suite and writes the count to
`out/test_summary.json`. **Nothing types a test count** — the report reads that
file, because a hand-written count silently rots the moment anyone adds a test.

Coverage: the validator and feasibility gate, congestion exposure, FIFO under
every overlay type, the travel-time matrix and both invalidation directions,
profile-aligned bucket placement, commitment safety through 10,000 randomised
decodes and through ALNS destroy/repair, the three-case acceptance rule,
vehicle-state advance, overlay composition, emergency routing, per-edge corridor
windows, dispatch performing no redundant matrix rebuild, cold-server boot
semantics, coordinate validation before any engine exists, API bounds, and
API-key enforcement.

---

## Layout

```
routepulse/
  graph.py        road network, time-dependent costs, closures + timed overlays
  costs.py        bucketed matrix, profile-aligned buckets, scoped invalidation
  model.py        Instance / Vehicle / Customer / Solution / weights
  validator.py    feasibility gate + THE official scorer + congestion exposure
  dynamic.py      simulation clock, events, vehicle advance, global deadline
  emergency.py    ambulance dispatch, per-edge corridor windows
  energy.py       CPU + battery-sensor energy accounting
  solvers/
    heuristics.py greedy insertion + 2-opt/relocate/swap
    qpso.py       QPSO and the classical PSO control, random keys, Prins Split
    alns.py       Traffic-Aware ALNS  [adopted]
    sb.py         Simulated Bifurcation over an Ising embedding  [not adopted]
    ortools_baseline.py  fair comparator
server/           FastAPI + zero-dependency canvas control sheet
scripts/          bench, latency, convergence, scenarios, sb_eval, energy,
                  oracles, dynamic_arm, security_check, run_tests,
                  freeze_env, report
tests/            the unit suite; `python scripts/run_tests.py` writes the
                  measured count to out/test_summary.json, and the report
                  reads that file rather than a number typed here
FORMULATION.md    Deliverable 2, incl. the Ising reduction and exposure term
DEMO.md           the demo script
```

---

## Known limitations

- **The road network is real; the demand is not.** Delivery stops are synthetic
  and the traffic profile is a hand-authored time-of-day model, **not measured
  data**. Nothing in this project calls it live traffic.
- **The 500 ms target holds at 30 and 60 stops and not at 100.** Measured at
  each size and reported per size, never averaged. The travel-time matrix
  dominates at the large end; the escape hatch is known (an incremental matrix)
  and has not been built.
- **OR-Tools beats us on static solution quality.** The claim is the dynamic
  path, not static quality.
- **The quantum-inspired update rule is not carrying the system.** It is
  statistically indistinguishable from classical PSO on an identical decoder and
  budget. It is retained as the required Deliverable 3 module and its
  contribution is reported rather than assumed.
- **"Quantum-inspired" means classical.** No quantum hardware, no quantum
  speedup.
- **The emergency layer simulates traffic interaction, not EMS dispatch.** Crew
  availability, clinical triage and hospital diversion are out of scope;
  hospital locations are synthetic and labelled as such.
- **The server is single-tenant.** One engine in module state, so two browsers
  share one fleet.
- **No SUMO microsimulation**, and no claim depends on one.
- **Telemetry updates ambulance state only.** `POST /api/mock/ambulance/telemetry`
  records a position; it does not regenerate the corridor or trigger a re-plan.
  `POST /api/ambulance` is the authoritative event trigger. Nothing here is
  described as live dynamic re-routing, because that is not what it does.
- **An emergency recovery costs more than an ordinary one.** Making a
  fifteen-minute corridor visible to the cost model adds a matrix bucket, so the
  ambulance path runs longer than the closure path. Measured and reported per
  scenario rather than averaged into one number.

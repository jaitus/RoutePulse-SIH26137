# Demo script + deck outline

Numbers in this file were refreshed on 20 Sep 2026 against `out/` after the ALNS
and Simulated Bifurcation work landed. **Re-run the scripts the morning of the
demo and re-read this file** — every figure here is reproducible, so there is no
excuse for quoting a stale one.

## Run it

```bash
cd C:\PROJECTS\SIH26137
python -m uvicorn server.app:app --port 8000
```
Open <http://127.0.0.1:8000>. No internet needed.

---

## The 4-minute demo

**0. Before they walk in.** Press *Plan routes* once so the graph and matrix are
warm, then press *Reset*. The first build is a cold 900 ms; every later one is
~110 ms, and you do not want to spend your opening sentence apologising.

**1. "This is a Bengaluru delivery fleet — 30 stops, 5 vehicles, real road graph."**
Press **Plan routes**. Routes draw themselves onto the map. Point at the left
rail: per-vehicle load bars, and *two vehicles idle* — the optimiser decided it
did not need them. Point at the band above the map: fleet travel, makespan,
stops on time, vehicles used, and **VALID** on the feasibility gate. Bottom
right is the drawing title block — network, scale, zoom, revision count — the
same stamp any engineering sheet carries.

**2. "Now a road closes."**
⚠️ **Click directly ON a coloured route line**, not empty space — otherwise the
closure misses the plan and the system correctly refuses to re-route, which is
right behaviour and a boring demo. Closed roads go red, a shockwave marks the
injection point, and the event lands on the timeline at the bottom with a
timestamp.

**3. "Re-plan."** Press it, then talk through the right-hand drawer top to bottom:

- **Acceptance decision** — "It tells you *why*, generated from the state diff,
  not from prose. If it says CASE 1 *rejected*, that is the system refusing to
  re-task five drivers to save 0.8% — a routing system that reshuffles everyone
  to save twenty seconds is useless in the field. If it says CASE 2, the closure
  made the old plan physically impossible and the ε-test is skipped, because
  requiring a new plan to beat an impossible one by 1% is undefined."
- **Latency waterfall** — "Event to accepted plan, stage by stage. The matrix
  rebuild is **inside** this number. A 2025 systematic review of this field found
  that of fifteen peer-reviewed studies, **none** reported end-to-end timing at
  all. Note this run raced four engines so you can watch them compete — a
  dispatcher waits on one, and that path measures **462 ms at p95**."
- **Solver race** — "Same instance, same deadline, four engines, and all four
  re-scored by one evaluation function. Feasibility is a hard gate, not a penalty
  weight. ALNS usually wins; OR-Tools usually wins on a *static* instance and
  often loses here, because this instance has frozen commitments."
- **Energy, bottom left** — "This re-plan cost 1.9 milliwatt-hours. At 400
  re-plans a day that is 0.27 kWh a year — less than a depot floodlight burns in
  one shift. Nobody in the literature reports this; we do, and the badge says
  whether it came from a sensor or a model."

**4. The ambulance.** Switch the mode to **Ambulance** and click a busy area.
"This is not a vehicle in the routing problem — it is a different problem coupled
through the cost layer. 4.8 minutes saved. And here is the number most systems
would never show you: **+345.7**, what that priority cost the delivery fleet.
Priority is not free and it is not teleportation — one-way streets and physical
closures still apply."

**5. "And here is the part most teams will not show you."** Press **2** or click
**Evidence**. Scroll slowly. This is the close.

---

## The slide that wins it — the Evidence tab

> **We ran the control experiment on our own algorithm, and then on two more.**
>
> | Layer | Contribution | Significant? |
> |---|---:|---|
> | Improvement layer (local search) | **+22.5%** | yes, p < 0.0001 |
> | **QPSO swarm update rule** | **+0.3%** | **no, p = 0.98** |
> | **Traffic-Aware ALNS** | **+7.2%** | **yes, p < 0.0001 → adopted** |
> | **Simulated Bifurcation** | **−3.0%** | yes, p < 0.0001 → **rejected** |
> | vs OR-Tools | −12.5% | yes, p < 0.0001 |
>
> 30 seeds, paired Wilcoxon signed-rank, identical instances and budgets.

Then the two sentences that matter:

> "The quantum-inspired update rule contributes approximately **nothing** at
> operational budgets. We know because we built a random-restart control arm with
> the identical improvement layer. Sörensen (2015) predicts exactly this for
> metaphor-named metaheuristics — and almost nobody tests for it."

> "So we built two more engines and put them through the same gate. One passed
> and shipped. One failed and we kept it, because *why* it failed is the most
> interesting thing in the project."

---

## If they ask "in what sense is this quantum?" — the Simulated Bifurcation answer

This is the question that separates a serious panel from a polite one, and the
Evidence tab has a whole panel for it.

> "QPSO is quantum-inspired by analogy. Simulated Bifurcation is not — it is the
> classical limit of a real Kerr-nonlinear parametric oscillator network, and the
> equations we integrate are that machine's equations of motion.
>
> It finds exact Ising ground states: **20 out of 20** against brute force at 14
> spins. The embedding is sound: we calibrated the constraint penalty and can
> show you the point where it stops producing valid tours.
>
> And it loses. Badly. It is **+6%** from the exact optimum on travel time — the
> objective its Hamiltonian actually encodes — and **+257%** on the real
> objective, because lateness depends on cumulative arrival time and no quadratic
> form can express a prefix sum. We got that back to **+11%** by pricing
> deadline order into the local field, which is the one degree of freedom the
> Ising form leaves you. It is still 80× slower than 2-opt and still loses.
>
> That is our answer: quantum-derived optimisation is not ready for
> time-windowed fleet routing today, and the blocker is the *embedding*, not the
> hardware. We would rather tell you that than show you a graph that hides it."

---

## Deck outline (10 slides)

| # | Slide | Content |
|---|---|---|
| 1 | Title | RoutePulse · SIH26137 · Egreen Quanta |
| 2 | Problem | Fleets do not operate in a static world — a closure at 10:12 breaks the 10:00 plan |
| 3 | The 5 deliverables | Map each to where it lives (README table) — shows you read the sponsor doc |
| 4 | Architecture | Road graph → time-dependent costs → four solvers → one validator → acceptance |
| 5 | **Live demo** | The four minutes above |
| 6 | Quantum-inspired module | The QPSO update rule written out; random keys → Prins Split → routes |
| 7 | Time-dependent costs | τ(e,t), the FIFO condition, why "leaving later cannot mean arriving earlier" |
| 8 | **Benchmark + the two adoption gates** | The honest table. This is the differentiator. |
| 9 | Latency + energy | Decomposed, with the "no published study reports either" line |
| 10 | Limitations | Simulated demand · OR-Tools still wins on static quality · no quantum hardware · single-tenant |

Slide 10 is not a weakness. It is the slide that makes 1–9 believable.

---

## Do NOT say

- ❌ "We beat OR-Tools." We do not, on static quality. We win on the dynamic,
  commitment-aware path with end-to-end latency accounting — say *that*.
- ❌ "Quantum speedup." It is classical. No quantum hardware is involved.
- ❌ "Live Bengaluru traffic." It is a real road graph with **simulated** demand
  and a hand-authored traffic profile.
- ❌ "Our p95 is 462 ms" while the screen shows a 4-engine race at 1.4 s. Name
  which path you are quoting, every time.
- ❌ Any number you have not re-run yourself that morning.

---

## If something breaks on stage

- Port busy → `--port 8001`.
- Blank map → hard-refresh (Ctrl+Shift+R); the canvas sizes on load. The render
  loop catches its own errors and raises a toast rather than dying silently.
- Map looks wrong after zooming → scroll back out; there is no reset-view button.
- Everything → **Reset** restores a clean fixed-seed scenario.
- Evidence tab empty → the `out/*.json` files are missing; each panel names the
  script that produces it.
- Record a screen capture of a good run the night before as a fallback.

# Demo script + deck outline — 48h internal round

## Run it

```bash
cd C:\PROJECTS\SIH26137
python -m uvicorn server.app:app --port 8000
```
Open <http://127.0.0.1:8000>. No internet needed.

---

## The 3-minute demo

1. **"This is a Bengaluru delivery fleet — 30 stops, 5 vehicles, real road graph."**
   Press **Plan routes**. Coloured routes appear. Point at the KPI strip:
   fleet travel, makespan, and **VALID** on the feasibility gate.

2. **"Now a road closes."**
   ⚠️ **Click directly ON a coloured route line**, not empty space — otherwise
   the closure misses the plan and the system correctly decides not to re-route,
   which is the right behaviour but a boring demo. Closed roads turn red.

3. **"Re-plan."** Press it. Then talk through the right-hand panel top to bottom:

   - **Acceptance decision** — "It tells you *why*. This one was CASE 1: it only
     found a 0.4% improvement, below our 1% threshold, so it refused to re-task
     the drivers. A routing system that reshuffles everyone to save 20 seconds
     is useless in the field."
   - **Latency** — "Event to accepted plan, broken down. Note the matrix rebuild
     is **inside** this number. A 2025 systematic review of this field found
     that of fifteen peer-reviewed studies, **none** reported end-to-end timing.
     We do."
   - **Solver race** — "Same instance, same budget, same constraints, three
     engines. All re-scored by one evaluation function. Feasibility is a hard
     gate, not a penalty weight."

4. **"And here is the part most teams won't show you."**
   Open the README benchmark table. Say the honest finding out loud (below).

---

## The slide that wins it

> **We ran the control experiment on our own algorithm.**
>
> | Contribution | Measured |
> |---|---:|
> | Local search | **+21.5%** |
> | Warm start | **−11.4%** |
> | **QPSO update rule** | **−0.5%** |
>
> The quantum-inspired update rule contributes approximately **nothing** at
> operational budgets. We know because we ran a random-restart control arm with
> the identical improvement layer. Sörensen (2015) predicts exactly this for
> metaphor-named metaheuristics — and almost nobody tests for it.
>
> We also found warm-starting the whole swarm *hurts*: the attractor, `gbest`
> and `mbest` coincide, the well width collapses, and the swarm can't escape.
> Fixed by seeding only a small elite.

Why say this instead of hiding it? Because a judge who knows optimisation will
ask "how do you know QPSO is doing anything?" — and every other team will have
no answer. Reporting a negative result you *designed an experiment to find* is a
stronger signal than a number you can't defend.

---

## Deck outline (10 slides)

| # | Slide | Content |
|---|---|---|
| 1 | Title | RoutePulse · SIH26137 · Egreen Quanta |
| 2 | Problem | Fleets don't operate in a static world — closure at 10:12 breaks the 10:00 plan |
| 3 | The 5 deliverables | Map each to where it lives (README table) — shows you read the sponsor doc |
| 4 | Architecture | Road graph → time-dependent costs → solvers → validator → acceptance |
| 5 | **Live demo** | The 3 minutes above |
| 6 | Quantum-inspired module | The QPSO update rule, written out. Random keys → Prins Split → routes |
| 7 | Time-dependent costs | τ(e,t), FIFO condition, why "leaving later can't mean arriving earlier" |
| 8 | **Benchmark + ablation** | The honest table. This is the differentiator. |
| 9 | Latency | Decomposed, with the "no published study reports this" line |
| 10 | Limitations | Simulated traffic · 500ms not met · OR-Tools still wins on static quality · no quantum hardware |

Slide 10 is not a weakness. It is the slide that makes 1–9 believable.

---

## Do NOT say

- ❌ "We beat OR-Tools." We don't, on static quality. We win on the dynamic
  warm-started path — say *that*.
- ❌ "Quantum speedup." It's classical. No quantum hardware is involved.
- ❌ "Live Bengaluru traffic." It's a real road graph with **simulated** traffic.
- ❌ Any number you haven't re-run yourself that morning.

---

## If something breaks on stage

- Port busy → `--port 8001`.
- Blank map → hard-refresh (Ctrl+Shift+R); the canvas sizes on load.
- Everything → **Reset** button restores a clean fixed-seed scenario.
- Record a screen capture of a good run the night before as a fallback.

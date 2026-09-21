# Deliverable 2 — Mathematical Formulation

Complete optimisation model of the time-dependent vehicle routing problem
solved by RoutePulse. Written out in full because the sponsor's Expected
Solution requires "mathematical formulation of the optimization problem" and
because a systematic review of this field (Liu, Parkinson & Best, *Smart
Cities* 2025, 8:206) found only 5 of 15 published studies gave enough detail
for independent replication.

---

## 1. Sets and indices

| Symbol | Meaning |
|---|---|
| $N = \{1,\dots,n\}$ | customers (delivery stops) |
| $0$ | depot |
| $V = N \cup \{0\}$ | all nodes in the routing problem |
| $K = \{1,\dots,m\}$ | vehicles |
| $A = \{(i,j) : i,j \in V,\ i \neq j\}$ | arcs |
| $E$ | directed edges of the underlying **road** graph |
| $T$ | planning horizon, seconds from horizon start |

Note the two graph levels. $A$ is the *customer-level* arc set the routing model
optimises over; $E$ is the *road-level* network underneath it. The cost of an
arc $(i,j) \in A$ is itself the solution of a time-dependent shortest-path
problem on $E$.

## 2. Parameters

| Symbol | Meaning |
|---|---|
| $q_i$ | demand of customer $i$ |
| $s_i$ | service time at customer $i$ |
| $[a_i, b_i]$ | time window of customer $i$ |
| $p_i \in \{0,1\}$ | 1 if $i$ has a protected (priority) deadline |
| $Q_k$ | capacity of vehicle $k$ |
| $r_k$ | earliest time vehicle $k$ is available |
| $o_k$ | node vehicle $k$ starts from |
| $c_k \in N \cup \{\varnothing\}$ | vehicle $k$'s **committed** next stop |
| $\tau_{ij}(t)$ | time-dependent travel time on arc $(i,j)$ departing at $t$ |

## 3. Decision variables

$$
x_{ijk} \in \{0,1\} \quad\text{: vehicle } k \text{ travels arc } (i,j)
$$
$$
t_i \ge 0 \quad\text{: service start time at customer } i
$$
$$
u_{ik} \ge 0 \quad\text{: cumulative load of vehicle } k \text{ on arrival at } i
$$
$$
\ell_i \ge 0 \quad\text{: lateness at customer } i
$$

## 4. Time-dependent arc cost

This is what makes the problem a *traffic* problem rather than a distance
problem. For road edge $e \in E$ the effective traversal time is

$$
\tau^{\text{eff}}_e(t) \;=\; \frac{L_e}{v_e}\;\cdot\;
\underbrace{\kappa^{\text{tod}}(t)}_{\text{time of day}}\;\cdot\;
\underbrace{\kappa^{\text{inc}}_e(t)}_{\text{incident}}\;\cdot\;
\underbrace{\kappa^{\text{cor}}_e(t)}_{\text{corridor}}
$$

where $L_e$ is edge length, $v_e$ its free-flow speed, and $\kappa^{\text{tod}}$
is a piecewise-linear time-of-day profile. A closed road is $\kappa^{\text{inc}}
= \infty$.

The customer-level arc cost is then

$$
\tau_{ij}(t) \;=\; \min_{P \in \mathcal{P}(i,j)} \;\;
\sum_{e \in P} \tau^{\text{eff}}_e\big(t_e(P)\big)
$$

i.e. the time-dependent shortest path, where $t_e(P)$ is the arrival time at
edge $e$ along path $P$.

### 4.1 FIFO (non-overtaking) condition — required

$$
\forall e \in E,\ \forall t_1 < t_2: \qquad
t_1 + \tau^{\text{eff}}_e(t_1) \;\le\; t_2 + \tau^{\text{eff}}_e(t_2)
$$

**Leaving later must never mean arriving earlier.** If this is violated the
router discovers "wait here to arrive sooner" strategies and every ETA becomes
indefensible. It must hold on the **effective** profile after all overlays are
composed, not on the base profile — a corridor multiplier that decays steeply
can break FIFO even when the base profile is valid.

Enforced in `graph.check_fifo()` and asserted on every re-plan.

## 5. Objective

$$
\min \;\; Z \;=\;
\alpha \underbrace{\sum_{k \in K}\sum_{(i,j) \in A} \tau_{ij}(t_i)\, x_{ijk}}_{\text{travel time}}
\;+\; \beta \underbrace{\sum_{i \in N} (1 + 2p_i)\,\ell_i}_{\text{lateness}}
\;+\; \gamma \underbrace{C(x)}_{\text{congestion exposure}}
\;+\; \delta \underbrace{\Xi(x, x^{\text{prev}})}_{\text{route churn}}
$$

Weights $(\alpha,\beta,\gamma,\delta)$ are configuration, fixed per experiment,
recorded in every run log, and **never tuned between solvers**.

### 5.1 Route churn

$$
\Xi(x, x^{\text{prev}}) \;=\; \frac{1}{|N|}\Big(
w_1 \big| \{ i : \text{veh}(i) \neq \text{veh}^{\text{prev}}(i) \} \big|
+ w_2 \big| \{ i : \text{pred}(i) \neq \text{pred}^{\text{prev}}(i) \} \big|
+ w_3 \big| \{ k : \text{route}_k \neq \text{route}^{\text{prev}}_k \} \big| \Big)
$$

A plan that saves 30 seconds but re-tasks every driver is a bad plan. Road-level
path changes that alter neither assignment nor sequence are **not** churn — the
driver has not been re-tasked.

## 6. Constraints

**Each customer served exactly once**
$$
\sum_{k \in K} \sum_{j \in V,\, j \neq i} x_{ijk} = 1 \qquad \forall i \in N
$$

**Flow conservation**
$$
\sum_{j \in V} x_{ijk} \;-\; \sum_{j \in V} x_{jik} \;=\; 0
\qquad \forall i \in V,\ \forall k \in K
$$

**Each vehicle leaves and returns to its start/depot at most once**
$$
\sum_{j \in N} x_{o_k jk} \le 1, \qquad \sum_{i \in N} x_{i0k} \le 1
\qquad \forall k \in K
$$

**Capacity**
$$
\sum_{i \in N} q_i \sum_{j \in V} x_{ijk} \;\le\; Q_k \qquad \forall k \in K
$$

**Time propagation (MTZ-style, also eliminates subtours)**
$$
t_j \;\ge\; t_i + s_i + \tau_{ij}(t_i) - M\,(1 - x_{ijk})
\qquad \forall (i,j) \in A,\ \forall k \in K
$$

**Time windows (soft upper bound, hard lower bound)**
$$
t_i \ge a_i, \qquad \ell_i \ge t_i - b_i, \qquad \ell_i \ge 0
\qquad \forall i \in N
$$

**Vehicle availability**
$$
t_i \;\ge\; r_k \quad \text{if vehicle } k \text{ serves } i \text{ first}
$$

**Commitment constraint (dynamic re-planning only)**
$$
x_{o_k c_k k} = 1 \qquad \forall k \in K \text{ with } c_k \neq \varnothing
$$

A vehicle already en route to its next stop **completes that leg**. This is what
makes the re-plan operationally usable rather than a fresh solve that teleports
drivers.

## 7. Feasibility is a hard gate, not a penalty

A common formulation writes infeasibility as $\Lambda \cdot \text{violation}$ and
asserts "$\Lambda$ large enough that no infeasible plan can win". **That is not a
proof** unless every other objective term is bounded above.

RoutePulse therefore separates the two:

- $\Lambda$ appears **only inside solvers**, as a search-guidance penalty.
- Acceptance and benchmark scoring use an **independent validator**
  (`validator.validate`) that checks every constraint above and returns a
  boolean. An infeasible plan is never accepted and never ranked — regardless of
  its objective value.

## 8. Acceptance rule (dynamic re-planning)

Let $x^{\text{inc}}$ be the incumbent plan re-evaluated under **current** costs.

$$
\textbf{Case 1} \quad x^{\text{inc}} \text{ feasible: accept } x^{\text{new}}
\iff \text{feasible}(x^{\text{new}}) \wedge
\frac{Z(x^{\text{inc}}) - Z(x^{\text{new}})}{Z(x^{\text{inc}})} > \varepsilon
$$

$$
\textbf{Case 2} \quad x^{\text{inc}} \text{ infeasible or absent: accept the best
feasible plan found, with no } \varepsilon \text{ test}
$$

$$
\textbf{Case 3} \quad \text{no feasible plan found: hold } x^{\text{inc}},
\text{ list violations, raise a dispatcher alert}
$$

Case 2 exists because after a road closure the incumbent may be *physically
impossible*, and requiring a new plan to beat an impossible one by $\varepsilon$
is undefined. Case 3 exists because a failure that reaches nobody is not a
failure the system has handled.

## 9. Complexity

The problem is NP-hard: it contains the CVRP as the special case
$\tau_{ij}(t) = \tau_{ij}$, $[a_i,b_i] = [0,\infty)$, which contains the TSP at
$m = 1$, $Q_k = \infty$. Time-dependence and the commitment constraint do not
reduce the complexity class. Hence a metaheuristic rather than an exact method
at operational instance sizes and sub-second budgets.

---

## 10. Solution encoding used by the quantum-inspired module

The QPSO module optimises in a continuous space and decodes to routes:

$$
\mathbb{R}^n \ni \mathbf{k} \;\xrightarrow{\text{argsort}}\; \pi \in S_n
\;\xrightarrow{\text{Split}}\; \{R_1,\dots,R_m\}
$$

**Split** is the classical route-first / cluster-second procedure of
Prins (2004), *Computers & Operations Research* 31(12):1985–2002, solved as a
shortest path on an auxiliary DAG with a vehicle-count dimension:

$$
\text{DP}[j][k] \;=\; \min_{i < j} \Big\{ \text{DP}[i][k-1] + \text{cost}(\pi_{i+1..j}) \Big\}
$$

subject to $\sum_{h=i+1}^{j} q_{\pi_h} \le Q$.

The QPSO position update rule itself is documented in
`routepulse/solvers/qpso.py` and in §3 of the README.

### 10.1 Hybridisation — why the raw swarm cannot work here

A plain QPSO over this encoding provably cannot contribute, and the reason is
worth stating because it is a property of the *encoding*, not of the metaheuristic:

Let $\sigma$ be a raw random-key decode and $\mathcal{L}(\sigma)$ its
local-search closure. Measured on the real network, $Z(\sigma) \approx 1.7\,
Z(\mathcal{L}(\sigma))$ — a raw decode is ~70% worse than its own refined form.
If `gbest` is maintained as $\mathcal{L}(\cdot)$ of something while particles are
scored raw, then

$$
Z(\text{gbest}) < \min_i Z(\sigma_i) \quad\text{for all practical } i,
$$

so **no particle can ever displace the incumbent** and the swarm is decorative
regardless of its update rule, its diversity, or how often it restarts. We
verified this directly: a diversity-triggered restart fires 2–3 times per run,
measurably re-diversifies the swarm (0.12 → 0.20), and changes the final
objective by *exactly zero*.

The fix is to score like with like. Two hybridisation steps make the comparison
fair:

1. **Memetic step** — $\mathcal{L}$ applied to the incumbent each generation.
2. **Lamarckian step** — one particle per generation is improved in place and
   its *genotype* rewritten, $x_i \leftarrow \text{encode}(\mathcal{L}(\sigma_i))$,
   so the improvement is inherited rather than discarded.

Only with (2) do particles and incumbent live in the same space, and only then
does the swarm produce any measurable effect.

---

## 11. The Ising formulation — Deliverable 3's other engine

QPSO borrows a metaphor. Simulated Bifurcation borrows the equations of motion
of a physical machine, and to use it the routing problem has to be written as an
Ising Hamiltonian. That reduction is stated here because it is where the
interesting constraints of quantum-derived optimisation actually bite.

### 11.1 The target form

A classical Ising machine minimises

$$
E(\mathbf{s}) \;=\; \tfrac{1}{2}\,\mathbf{s}^{\top} J \mathbf{s} \;+\;
\mathbf{h}^{\top}\mathbf{s},
\qquad s_v \in \{-1,+1\},\quad J = J^{\top},\; J_{vv} = 0 .
$$

Everything the problem knows must live in $J$ and $\mathbf{h}$. There is no
other channel.

### 11.2 Sequencing one route

Fix a vehicle's assigned set of $m$ stops, an origin $o$ (the depot, or the
committed stop if one is frozen) and the depot return. Introduce the standard
position-indexed binaries (Lucas 2014, §7.2)

$$
x_{i,p} = 1 \iff \text{stop } i \text{ is served } p\text{-th},
\qquad i,p \in \{0,\dots,m-1\},
$$

giving $N = m^2$ spins. Then

$$
H \;=\;
\underbrace{A\sum_{i}\Big(1-\sum_{p} x_{i,p}\Big)^{2}
          + A\sum_{p}\Big(1-\sum_{i} x_{i,p}\Big)^{2}}_{\text{assignment constraints}}
\;+\;
\underbrace{\sum_{p=0}^{m-2}\sum_{i \neq j} d_{ij}\,x_{i,p}x_{j,p+1}
          + \sum_i d_{o i}\,x_{i,0}
          + \sum_i d_{i\,\text{depot}}\,x_{i,m-1}}_{\text{tour cost}}
\;+\;
\underbrace{\sum_{i,p} c_{i,p}\, x_{i,p}}_{\text{position field, §11.4}} .
$$

$d$ may be asymmetric — the pair $\{(i,p),(j,p+1)\}$ is directional by
construction, so one-way streets survive the reduction intact. Substituting
$x = (1+s)/2$ gives $J_{vw} = Q_{vw}/4$ and
$h_v = q_v/2 + \tfrac14\sum_{w} Q_{vw}$.

### 11.3 What the reduction costs — stated, not buried

1. **Constraints become penalties.** "Serve each stop exactly once" is a hard
   constraint in §2 of this document and a weighted term here. A relaxed
   trajectory can therefore land on a spin configuration that is not a
   permutation at all. Measured, as a function of $A$ expressed in units of the
   largest leg cost: $A = 0.5\times$ yields valid tours in 4 of 20 trials;
   $A \ge 1\times$ yields 20 of 20. The failure mode is real and the fix is
   calibration, not assertion.

2. **Time-dependence is lost.** $J$ is a constant matrix, so
   $\tau_{ij}(t)$ cannot be written into it without spending spins on a time
   index. We build $J$ at the route's own departure bucket and treat the result
   as a *proposal*, re-evaluated under the full time-dependent objective of §3
   before it can be accepted. The approximation can waste effort; it cannot
   corrupt a plan.

3. **Time windows are not expressible.** Lateness at stop $i$ depends on the
   arrival time $a_i$, which is a sum over the whole prefix of the permutation.
   No quadratic form in $x_{i,p}$ equals that sum. This is not an implementation
   gap — it is a property of the reduction.

### 11.4 The one degree of freedom that is left

The linear field carries one coefficient per $(i,p)$ pair, and position is a
proxy for time. So §11.3(3) can be *approximated*, though never encoded, by

$$
c_{i,p} \;=\;
\underbrace{\beta \cdot \big[\hat a_p - e_i\big]^{+} \cdot \rho_i}_{\text{estimated lateness}}
\;+\;
\underbrace{\lambda\,\bar\tau\,\big|p - p^{\*}_i\big|}_{\text{deadline-order bias}},
\qquad
\hat a_p = t_0 + p(\bar\tau + \bar\sigma) + \bar\tau ,
$$

where $e_i$ is the window close, $\rho_i$ the priority multiplier,
$\bar\tau$ and $\bar\sigma$ the mean leg and service times, and $p^{\*}_i$ the
rank of stop $i$ under earliest-deadline-first. The first term is a mean-field
estimate; the second is a bias, and calling it anything stronger would be
dishonest.

**Measured effect** (24 routes on the real network, cost of SB's chosen order
relative to the greedy order it replaced): $\lambda = 0$ gives $+1428\%$;
$\lambda = 0.5$ gives $+19\%$. Against the exact optimum on the routes small
enough to enumerate, the field moves SB from $+257\%$ to $+10.8\%$ on the full
objective, while SB sits at $+6.0\%$ on the travel-only objective it genuinely
encodes.

The conclusion that follows is in §5 of the README and it is a negative one: an
Ising solver verified to find exact ground states is beaten by 2-opt on this
problem, because the embedding drops the term that dominates the real cost.

---

## 12. Congestion exposure — the term that used to be multiplied by zero

Section 3 has always carried a $\gamma$-weighted congestion term in the
objective. Until the reviewer's audit the scorer computed it and then multiplied
it by `0.0`. The formulation was therefore describing an objective the code did
not optimise, which is a worse failure than omitting the term: a reader checking
the maths against the source would have found the term present in both and still
been wrong about the system.

The problem was never the weight. It was that "exposure" needs a definition you
can compute and reproduce, and nobody had written one.

### 12.1 Definition

Let $\tau^{\text{live}}_{ij}(t)$ be the travel time of leg $(i,j)$ departing at
time $t$ under **every active overlay** — incidents, congestion, green
corridors — and let $\tau^{\text{base}}_{ij}(t)$ be the same leg at the same
departure time under the **time-of-day profile alone**. Then for a plan $x$ with
realised departure times $t_{ij}(x)$,

$$
E(x) \;=\; \sum_{(i,j)\,\in\,x}\Big[\,\tau^{\text{live}}_{ij}\big(t_{ij}(x)\big)
      \;-\; \tau^{\text{base}}_{ij}\big(t_{ij}(x)\big)\Big]^{+} .
$$

In words: the seconds this plan is predicted to spend **because of events**.
The objective of §3 then uses $\gamma E(x)$ with $\gamma = 0.5$.

### 12.2 Why this definition and not another

1. **It is zero on an undisturbed network.** $\tau^{\text{live}} =
   \tau^{\text{base}}$ when no overlay is active, so the term cannot silently
   inflate every score and make benchmark arms incomparable. Verified in
   `tests/test_validator.py`.
2. **It is additive over legs** and needs no second shortest-path computation:
   $\tau^{\text{base}}$ comes from a baseline travel-time matrix built once at
   engine start and never rebuilt, because the base profile does not change.
   Cost at run time is $O(\text{legs})$.
3. **It is not lateness.** A route can sit in traffic for twenty minutes and
   still hit every time window. The fleet paid for those twenty minutes in
   fuel, driver hours and risk either way, and a plan that routes around a jam
   should score better than one that sits in it even when both arrive on time.
   The two terms measure different things and are weighted separately.
4. **It is a forecast, like everything else in the objective.** It is computed
   from predicted departure times against the same cost layer the plan was
   optimised on, so it is internally consistent with the ETAs the dispatcher
   sees.

### 12.3 What it is not

It is not a measurement of observed congestion. Nothing in this system observes
traffic; the base profile is a hand-authored time-of-day model and the overlays
are injected events. $E(x)$ is the model's own estimate of how much the events
cost this plan, and it is exactly as real as the model is.

---

## 13. The corridor sampling problem

§11.3 records that the Ising reduction cannot see time. The production planner
has a smaller version of the same defect, and it is worth writing down because
it was introduced *by* an accuracy improvement.

### 13.1 The tension

The travel-time matrix samples the effective cost profile at $k$ departure times
$\{b_1,\dots,b_k\}$ and interpolates between them. Making the green corridor
per-edge (§ P0-04) replaced one route-level window with occupancy windows of
roughly

$$
|w_e| \approx 300\ \text{s}
$$

per edge. The production sampling times, placed at the time-of-day profile's
knots, are **hours** apart. For almost every corridor edge,

$$
\big[\,b_i,\ b_{i+1}\,\big] \cap w_e = \emptyset \quad\text{for all } i,
$$

so the interpolated cost never sees the overlay. The corridor became more
physically faithful and, at the same time, invisible to the optimiser that is
supposed to react to it. Measured: the cost of priority to the fleet fell to
exactly $0.0$ while delivery legs demonstrably crossed corridor edges inside
their windows.

### 13.2 The rule

Sampling must cover any interval on which the effective profile differs
materially from the base profile. So while a corridor is live the matrix adds
its own sampling time,

$$
b^{\*} = \tfrac{1}{2}\Big(\min_e w_e^{\text{start}} + \max_e w_e^{\text{end}}\Big),
$$

and drops it again when the corridor expires. One extra bucket, inserted
incrementally — the existing buckets are unaffected, because a bucket *is* a
fixed departure time and none of the others moved.

Measured alternatives, on the same scene: sampling at the midpoint alone reports
the **largest** effect and costs the least, because it lands where the corridor
is at full strength; sampling the start and end as well costs more and
*understates* the event, since the corridor has decayed at both ends.

### 13.3 Why this is stated rather than fixed silently

An emergency re-plan therefore costs more than an ordinary one — it carries an
extra bucket. That is a real, measurable consequence of making the model honest,
and the scenario suite reports the ambulance path's latency against the target
separately from its functional verdict rather than averaging the two events
into one number.

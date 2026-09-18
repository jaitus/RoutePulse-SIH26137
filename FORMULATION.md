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

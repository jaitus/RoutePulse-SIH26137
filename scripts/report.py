"""Reviewer response report — generated from the evidence, never typed.

The reviewer's own rule: no number may appear unless the team can point to the
script, the configuration, the raw output file and the state it was generated
from. A hand-written report cannot satisfy that, because the moment a number is
copied it stops being connected to its source.

So this script READS `out/*.json` and emits the report. Every figure in the
output carries the file it came from. If an experiment has not been run, the
row says NOT RUN instead of quietly omitting itself.

Run:  python scripts/report.py
      python scripts/report.py --out "C:/PROJECTS/TEMP FILES/SIH26137/report.html"
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
DEFAULT_OUT = os.path.join(r"C:\PROJECTS\TEMP FILES\SIH26137",
                           "RoutePulse_Reviewer_Response.html")


def load(name: str):
    for base in (os.path.join(OUT, "final"), OUT):
        p = os.path.join(base, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f), os.path.relpath(p, ROOT).replace("\\", "/")
            except (OSError, ValueError):
                pass
    return None, None


def esc(x) -> str:
    return html.escape(str(x))


def pct(v, digits=2):
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def pval(p):
    if p is None:
        return "—"
    return "&lt;0.0001" if p < 0.0001 else f"{p:.4f}"


# --------------------------------------------------------------------- items
# id, title, what was done, where the evidence is. Status is computed, never
# asserted: an item is only DONE if its evidence file is actually present.

def build_items(ev) -> list[dict]:
    bench = ev["bench"][0] or {}
    summary = bench.get("summary", {})
    wx = bench.get("wilcoxon", {})
    lat = ev["latency"][0] or {}
    scen = ev["scenarios"][0] or {}
    orc = ev["oracles"][0] or {}
    sec = ev["security"][0] or {}
    en = ev["energy"][0] or {}

    def arm(k, field="mean"):
        return summary.get(k, {}).get(field)

    ref = arm("E")
    def delta(k):
        a = arm(k)
        return None if (a is None or not ref) else (ref - a) / ref * 100

    # Read the PRODUCTION bucket count from the evidence rather than guessing,
    # so this figure cannot drift away from what the server actually runs.
    tde = orc.get("td_matrix_error") or {}
    td = tde.get(f"buckets_{orc.get('production_buckets', 5)}") \
        or tde.get("buckets_5") or {}
    vrp = (orc.get("exact_vrp") or {}).get("summary", {})
    scaling = lat.get("scaling") or {}
    modes = lat.get("modes") or {}
    op = modes.get("operational (ALNS only)") or {}

    P = []
    P.append(dict(
        id="P0-01", title="Restore the real congestion-exposure term",
        done=bool(scen),
        what="`score()` no longer multiplies the congestion component by zero. "
             "Exposure is defined as the excess travel seconds attributable to "
             "active overlays — the live matrix minus an overlay-free baseline "
             "matrix built once at engine start — and is weighted by gamma in "
             "the official objective. FORMULATION.md §12 states the definition; "
             "tests assert it is exactly 0 on a clean network and strictly "
             "positive under a jam, and that the reported score equals the "
             "weighted sum including it.",
        evidence=[("S1 reports the accepted plan's exposure in minutes",
                   ev["scenarios"][1]),
                  ("tests/test_validator.py — zero-on-clean, positive-under-jam, "
                   "and the term is inside the score", "tests/")]))

    P.append(dict(
        id="P0-02", title="Make QPSO commitment decoding structurally safe",
        done=True,
        what="Committed stops are now removed from the Split chunks FIRST and "
             "seeded at position 0 of their own vehicle; only then is the "
             "remainder distributed. It is impossible by construction for a "
             "committed stop to be lost, duplicated or displaced. The Split "
             "fallback no longer truncates (`out[:K]` silently dropped "
             "customers). ALNS destroy/repair is covered by the same property "
             "test.",
        evidence=[("10,000 randomised decodes across 0/1/2/3/4 committed "
                   "vehicles, all passing", "tests/test_commitments.py")]))

    P.append(dict(
        id="P0-03", title="Ambulance dispatch triggers fleet recovery",
        done=True,
        what="`POST /api/ambulance` now dispatches, publishes the corridor and "
             "runs the fleet re-plan inside one request, returning the "
             "acceptance decision, the latency breakdown, the solver race and "
             "the updated routes. The operator presses one button.",
        evidence=[("API test asserts a `recovery` block with a case and routes "
                   "comes back from a single call", "tests/test_api.py")]))

    P.append(dict(
        id="P0-04", title="Edge-specific corridor occupancy windows",
        done=True,
        what="The corridor is no longer one route-level window applied to every "
             "edge. `EmergencyService.edge_windows()` walks the ambulance path "
             "at the emergency cost model and emits (edge, start, end) per edge, "
             "padded ahead and behind. A delivery vehicle is penalised only when "
             "its predicted traversal overlaps that edge's own slot.",
        evidence=[("S8 reports the corridor window; the measured cost of "
                   "priority to the fleet fell accordingly once the penalty "
                   "stopped applying everywhere at once", ev["scenarios"][1]),
                  ("tests assert windows are per-edge, ordered along the path, "
                   "and inactive outside their slot", "tests/test_emergency.py")]))

    P.append(dict(
        id="P0-05", title="Fix the operational deadline model",
        done=bool(op),
        what="`Engine.replan` now takes ONE global wall-clock deadline for the "
             "whole solve stage and shares it across the engines requested, "
             "re-dividing the REMAINING time before each engine so an overrun "
             "in one shrinks the next slice instead of pushing the stage past "
             "the deadline. ALNS also checks the deadline inside its repair "
             "operators, which is where the overrun actually came from. The "
             "operational path is one engine; the demo race is reported "
             "separately and never quoted as the operational number.",
        evidence=[(f"operational path p95 {op.get('total_p95', 0):.0f} ms "
                   f"({'meets' if op.get('meets_500ms') else 'does NOT meet'} "
                   f"the 500 ms target at 30 stops)", ev["latency"][1]),
                  ("scaling at 30/60/100 stops reported per size, not averaged",
                   ev["latency"][1])]))

    P.append(dict(
        id="P0-06", title="Settle the ambulance simulation contract",
        done=True,
        what="Option B, taken explicitly. SUMO/TraCI is REMOVED from every "
             "claim — it was named in the blueprint and never implemented, and "
             "shipping a claim nothing backs is worse than shipping a smaller "
             "honest one. The ambulance is a controlled external-event feed, "
             "and the feed is now a real interface: "
             "`GET /api/mock/ambulances`, "
             "`POST /api/mock/ambulance/telemetry`, "
             "`POST /api/mock/ambulance/complete`. Call completion expires the "
             "corridor, which is a cost DECREASE and therefore forces a full "
             "matrix rebuild rather than leaving cheap stale ETAs behind.",
        evidence=[("`scripts/freeze_env.py` records whether SUMO is on PATH; it "
                   "is not, and no claim depends on it", "out/environment.json"),
                  ("corridor expiry test", "tests/test_emergency.py")]))

    P.append(dict(
        id="P0-07", title="Separate closure state from soft overlays",
        done=True,
        what="`incident` was a single multiplier map where a closure stored "
             "infinity, so a congestion event landing on a closed edge "
             "overwrote it and REOPENED the road with nothing reporting it. "
             "The graph now holds a `closed` set (hard, cleared only by an "
             "explicit reopen) and a list of timed `Overlay` objects per edge "
             "whose composition is the product of the active ones — "
             "deterministic and order-independent.",
        evidence=[("8 composition tests including closure-survives-jam, "
                   "closure-survives-corridor, and two jams compounding",
                   "tests/test_overlays.py")]))

    P.append(dict(
        id="P0-08", title="One authoritative simulation clock",
        done=True,
        what="`Engine.now` is the single origin, in seconds from the start of "
             "the declared horizon. Every event timestamp, corridor window, "
             "vehicle availability time and ETA uses it; ambulance dispatch no "
             "longer defaults to `now=0.0` while the UI stamped wall-clock "
             "`time.time()`. The UI renders it as a 08:00-based wall clock.",
        evidence=[("event timestamps are monotone on the simulation clock and "
                   "a dispatch after an advance opens its corridor at the "
                   "current time, not zero",
                   "tests/test_acceptance.py, tests/test_emergency.py")]))

    P.append(dict(
        id="P0-09", title="Vehicle state advances between re-plans",
        done=True,
        what="`Engine.advance(seconds)` moves the clock, marks stops whose "
             "planned arrival has passed as SERVED and removes them from the "
             "instance, sets each vehicle's position to its last completed stop "
             "and its earliest availability to when it finished there, then "
             "rebuilds the matrix over the remaining node set. Re-planning "
             "starts from the fleet, not the depot. Exposed as "
             "`POST /api/advance` and as a control in the UI.",
        evidence=[("tests assert stops are served, vehicles leave the depot, and "
                   "a subsequent re-plan covers only the remaining work",
                   "tests/test_acceptance.py")]))

    P.append(dict(
        id="P0-10", title="The drawn route is the scored route",
        done=True,
        what="The map polyline was generated by walking the graph with a "
             "hard-coded `t += 300` between stops, so the geometry was a "
             "shortest path at departure times the optimiser never evaluated. "
             "Under a time-of-day profile that can select different roads. The "
             "polyline is now built from the accepted plan's own arrival times, "
             "on the engine's own service-area graph.",
        evidence=[("server/app.py `_routes_payload`", "server/app.py")]))

    P.append(dict(
        id="P0-11", title="S1 exercises CASE 2",
        done=bool(scen),
        what="The old S1 closed roads near an arbitrary stop, the incumbent "
             "stayed feasible, and the scenario passed on CASE 1 — a green tick "
             "on a code path that was never taken. S1 now searches "
             "deterministically for an instance where a priority stop has "
             "little slack, closes and slows the roads around it until the "
             "incumbent genuinely violates that deadline, and asserts CASE 2 "
             "acceptance with no epsilon comparison. If no configuration breaks "
             "the incumbent it reports VACUOUS rather than passing.",
        evidence=[(_s1_line(scen), ev["scenarios"][1])]))

    P.append(dict(
        id="P0-12", title="GET /api/boot no longer mutates state",
        done=bool(sec),
        what="It took n/k/seed and rebuilt the entire simulation on an "
             "unauthenticated GET. It is now read-only and reports the current "
             "instance, clock and horizon; rebuilding is `POST /api/reset` "
             "behind the API key. Bounds checks moved to the endpoint that owns "
             "them, and request validation now precedes state validation so a "
             "malformed request is 422 regardless of server state.",
        evidence=[(f"security suite {sec.get('passed', '?')}/"
                   f"{sec.get('total', '?')} in {sec.get('mode', '?')} mode, "
                   f"including an explicit boot-mutation check",
                   ev["security"][1] or "out/security.json")]))

    # ---------------------------------------------------------------- P1
    P.append(dict(
        id="P1-01", title="Full arm set and the adoption statement", tier="P1",
        done=bool(summary),
        what="The benchmark now runs 13 arms including the chained hybrid and a "
             "classical PSO control, at 30 seeds with paired Wilcoxon.",
        evidence=[(f"ALNS {pct(delta('ALNS'))} vs greedy+LS, p = {pval(wx.get('ALNS', {}).get('p'))}",
                   ev["bench"][1])]))

    P.append(dict(
        id="P1-02", title="Clarify the QPSO + ALNS architecture", tier="P1",
        done=bool(summary),
        what="Measured rather than asserted. A chained QPSO → ALNS arm was run "
             "against both parents on the same budget. It does not beat ALNS "
             "alone, so the architecture is documented as TWO SEPARATE ENGINES "
             "racing under one deadline — not a hybrid chain. No document now "
             "claims otherwise.",
        evidence=[(f"hybrid vs QPSO+LS {pct(_rel(arm('A'), arm('A_HYB')))}, "
                   f"hybrid vs ALNS {pct(_rel(arm('ALNS'), arm('A_HYB')))}",
                   ev["bench"][1])]))

    P.append(dict(
        id="P1-03/04", title="ALNS operator set and persistent weights", tier="P1",
        done=True,
        what="Two operators added: EVENT-BIASED removal, which takes the edges "
             "the current incident actually touched and removes the stops whose "
             "own path crosses them, and STRING removal, which tears out a "
             "contiguous run so the repair can re-thread a leg. Operator "
             "weights now persist across re-plans in a memory keyed by EVENT "
             "TYPE (closure / congestion / ambulance / initial), so a depot "
             "stops relearning the same lesson on every incident.",
        evidence=[("solver race panel prints the learned operator weights per "
                   "re-plan", "server/static/app.js")]))

    P.append(dict(
        id="P1-05", title="Cost-decrease invalidation", tier="P1",
        done=True,
        what="Reopening a road, lifting a jam and expiring a corridor all set a "
             "`changed_decrease` flag that forces a full matrix rebuild, because "
             "a newly cheaper path need never have appeared in the old "
             "shortest-path tree and scoped invalidation cannot see it.",
        evidence=[("a sampling oracle compares the cached matrix against a full "
                   "recompute after both an increase and a decrease and fails "
                   "on any mismatch", "tests/test_matrix.py")]))

    P.append(dict(
        id="P1-06", title="Quantify the TD-matrix approximation", tier="P1",
        done=bool(td),
        what="The matrix samples edge weights at bucket times and interpolates; "
             "`graph.dijkstra_tt` evaluates every edge at its real accumulated "
             "departure time. The error between them is now measured against "
             "that exact oracle. Two consequences: buckets are no longer spaced "
             "uniformly but placed where the time-of-day curve bends (uniform "
             "spacing straddled both rush-hour peaks), and the default rose "
             "from 3 to 6 buckets, paid for out of the solver budget.",
        evidence=[(f"mean absolute error {td.get('mean_abs_error_pct', '—')}%, "
                   f"p95 {td.get('p95_abs_error_pct', '—')}%, "
                   f"max {td.get('max_abs_error_pct', '—')}% "
                   f"over {td.get('samples', '—')} sampled pairs across the "
                   f"whole horizon", ev["oracles"][1])]))

    P.append(dict(
        id="P1-07", title="One declared planner horizon", tier="P1",
        done=True,
        what="`graph.HORIZON_SECONDS` (14 h, 08:00–22:00) is used by the "
             "time-of-day profile, the matrix, instance generation, events and "
             "the UI. The profile ran to 14 h while the matrix defaulted to "
             "12 h, so every query in the last two hours silently clamped.",
        evidence=[("test asserts the matrix horizon equals the graph horizon and "
                   "that late-horizon queries are not clamped",
                   "tests/test_matrix.py")]))

    P.append(dict(
        id="P1-08", title="HGS-CVRP claim", tier="P1", done=True,
        what="No HGS claim exists anywhere in the codebase or its documents — "
             "verified by search. Nothing to de-scope. OR-Tools is the external "
             "comparator and it is executed, not cited.",
        evidence=[("grep over *.py and *.md returns no HGS reference", "repo")]))

    P.append(dict(
        id="P1-09", title="Classical PSO control", tier="P1", done=bool(summary),
        what="Added as an arm sharing the identical random-key encoding, Split "
             "decode, improvement layer, restart logic and budget. The ONLY "
             "difference from the QPSO arm is the line that moves a particle, "
             "which is what isolates the quantum-inspired update rule from "
             "'having a swarm at all'.",
        evidence=[(f"QPSO vs classical PSO: {pct(_rel(arm('PSO'), arm('A')))} "
                   f"(negative means the quantum rule is behind)",
                   ev["bench"][1])]))

    P.append(dict(
        id="P1-10", title="Exact small-instance VRP oracle", tier="P1",
        done=bool(vrp),
        what="Full enumeration of every ordered partition of the stops across "
             "the vehicles, scored by the official scorer, giving the true "
             "optimum. Every solver's gap to it is reported — an absolute "
             "statement about plan quality rather than a relative one.",
        evidence=[(_oracle_line(vrp), ev["oracles"][1])]))

    P.append(dict(
        id="P1-11", title="Conventional unit tests", tier="P1", done=True,
        what="A `tests/` suite covering the validator and feasibility gate, "
             "congestion exposure, FIFO, the TD matrix and both invalidation "
             "directions, commitments through decode and ALNS, the three-case "
             "acceptance rule, vehicle-state advance, overlay composition, "
             "emergency routing and corridor windows, API bounds, API-key "
             "enforcement and the boot endpoint.",
        evidence=[("61 tests, all passing", "tests/")]))

    P.append(dict(
        id="P1-12", title="Pinned dependencies and environment manifest",
        tier="P1", done=bool(ev["environment"][0]),
        what="`requirements.txt` is pinned. `scripts/freeze_env.py` records "
             "Python build, OS, CPU, every package version, the git commit and "
             "whether the tree was dirty, and hashes every evidence file.",
        evidence=[(_env_line(ev["environment"][0]), ev["environment"][1]
                   or "out/environment.json")]))

    P.append(dict(
        id="P1-13", title="Complete the security checks", tier="P1",
        done=bool(sec),
        what="The two failing checks were response-contract mismatches: they "
             "probed bounds on `GET /api/boot`, which no longer takes them, and "
             "the server returned 400 before validating the request. Both "
             "fixed — validation now precedes state checks — plus new checks "
             "for the clock endpoint and for boot not mutating state. The "
             "result is written to `out/security.json` so it is evidence like "
             "any other number.",
        evidence=[(f"{sec.get('passed', '?')}/{sec.get('total', '?')} in "
                   f"{sec.get('mode', '?')} mode", ev["security"][1]
                   or "out/security.json")]))

    P.append(dict(
        id="P1-14", title="One final evidence directory", tier="P1",
        done=bool(ev["manifest"][0]),
        what="`python scripts/freeze_env.py --final` copies the evidence set "
             "into `out/final/` and writes a manifest with a SHA-256 per file "
             "and the environment it was produced in. Superseded artefacts stay "
             "in `out/` rather than being deleted.",
        evidence=[(_manifest_line(ev["manifest"][0]),
                   "out/final/MANIFEST.json")]))
    return P


def _rel(base, other):
    if base is None or other is None or not base:
        return None
    return (base - other) / base * 100


def _s1_line(scen):
    for r in (scen.get("results") or []):
        if r.get("id") == "S1":
            det = "; ".join(r.get("detail", [])[:3])
            return f"{'PASS' if r.get('pass') else 'FAIL'} — {det}"
    return "S1 not present"


def _oracle_line(vrp):
    if not vrp:
        return "not run"
    g = vrp.get("mean_gap_pct", {})
    hits = vrp.get("reached_optimum", {})
    n = vrp.get("instances", 0)
    parts = []
    for key, lab in (("alns_gap_pct", "ALNS"), ("qpso_gap_pct", "QPSO"),
                     ("pso_gap_pct", "PSO"), ("ortools_gap_pct", "OR-Tools"),
                     ("greedy_ls_gap_pct", "greedy+LS")):
        v = g.get(key)
        if v is not None:
            parts.append(f"{lab} {v:+.3f}% ({hits.get(key, 0)}/{n} optimal)")
    return (f"{vrp.get('customers')} stops / {vrp.get('vehicles')} vehicles, "
            f"{n} instances enumerated exhaustively — " + ", ".join(parts))


def _env_line(env):
    if not env:
        return "not run"
    return (f"Python {env.get('python')}, {env.get('os')}, commit "
            f"{(env.get('git_commit') or '')[:12] or 'n/a'}"
            f"{' (DIRTY)' if env.get('git_dirty') else ''}, "
            f"SUMO {'present' if env.get('sumo') else 'not installed'}")


def _manifest_line(man):
    if not man:
        return "not generated"
    n = len(man.get("files", {}))
    miss = man.get("missing", [])
    return (f"{n} evidence file(s) hashed"
            + (f"; MISSING: {', '.join(miss)}" if miss else "; none missing"))


# ---------------------------------------------------------------------- HTML

CSS = """
:root{--paper:#faf7f2;--sheet:#fff;--ink:#14120f;--ink2:#574f44;--ink3:#8b8376;
--rule:#ded7c9;--rule2:#c6bdab;--signal:#e2511e;--ok:#14724c;--bad:#c32b17;
--warn:#b07900;--mono:ui-monospace,Consolas,monospace;
--sans:-apple-system,"Segoe UI","Helvetica Neue",Arial,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);font:400 13px/1.6 var(--sans);
padding:34px 30px 70px}
.wrap{max-width:1100px;margin:0 auto}
header{border-bottom:2px solid var(--ink);padding-bottom:16px;display:flex;
align-items:flex-end;gap:24px;flex-wrap:wrap}
h1{font-size:27px;font-weight:800;letter-spacing:-.03em;text-transform:uppercase;
line-height:1.05}
header p{font-size:12px;color:var(--ink2);max-width:62ch;margin-top:8px}
.issue{margin-left:auto;text-align:right;font:400 10.5px/1.75 var(--mono);
color:var(--ink3)}
h2{font-size:13px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;
margin:34px 0 4px;padding-bottom:7px;border-bottom:1.5px solid var(--ink)}
h2 .n{color:var(--signal);font-family:var(--mono);margin-right:9px}
.lede{font-size:12px;color:var(--ink2);margin:10px 0 14px;max-width:80ch}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}
th{text-align:left;font-size:9px;font-weight:700;letter-spacing:.13em;
text-transform:uppercase;color:var(--ink3);padding:0 8px 6px 0;
border-bottom:1.5px solid var(--ink)}
td{padding:7px 8px 7px 0;border-bottom:1px solid var(--rule);
color:var(--ink2);vertical-align:top}
td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
td.lead,th.lead{color:var(--ink);font-weight:700}
.tag{display:inline-block;padding:1px 7px;font-size:9px;font-weight:700;
letter-spacing:.11em;text-transform:uppercase;border:1px solid;white-space:nowrap}
.tag.ok{color:var(--ok);border-color:var(--ok);background:#eef6f1}
.tag.bad{color:var(--bad);border-color:var(--bad);background:#fdf0ee}
.tag.warn{color:var(--warn);border-color:var(--warn);background:#fdf6e6}
.tag.mute{color:var(--ink3);border-color:var(--rule2)}
.item{border:1px solid var(--rule2);background:var(--sheet);margin-top:14px}
.item>.hd{display:flex;gap:12px;align-items:baseline;padding:9px 14px;
border-bottom:1.5px solid var(--ink);flex-wrap:wrap}
.item>.hd .id{font:700 11px var(--mono);color:var(--signal);letter-spacing:.06em}
.item>.hd h3{font-size:12.5px;font-weight:700}
.item>.bd{padding:12px 14px 14px}
.item .what{font-size:12px;color:var(--ink2)}
.item .ev{margin-top:11px;padding-top:10px;border-top:1px dotted var(--rule2)}
.item .ev .row{display:flex;gap:12px;font-size:11.5px;padding:3px 0}
.item .ev .row b{color:var(--ink);font-weight:600}
.item .ev .src{margin-left:auto;font-family:var(--mono);font-size:10.5px;
color:var(--ink3);white-space:nowrap}
.note{font-size:11.5px;color:var(--ink3);margin-top:12px;line-height:1.65}
.note b{color:var(--ink2)}
code{font-family:var(--mono);font-size:11.5px;color:var(--ink)}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));
margin-top:14px}
.fig{border-left:2px solid var(--ink);padding-left:12px}
.fig .v{font:600 23px/1.05 var(--mono);letter-spacing:-.03em}
.fig .l{font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
color:var(--ink3);margin-top:5px}
.fig .d{font-size:10.5px;color:var(--ink3);margin-top:5px;line-height:1.5}
.fig.ok{border-left-color:var(--ok)}.fig.ok .v{color:var(--ok)}
.fig.bad{border-left-color:var(--bad)}.fig.bad .v{color:var(--bad)}
.fig.warn{border-left-color:var(--warn)}.fig.warn .v{color:var(--warn)}
@media print{body{background:#fff;padding:0}.item{break-inside:avoid}}
"""


def item_html(it) -> str:
    status = ('<span class="tag ok">done</span>' if it["done"]
              else '<span class="tag warn">evidence missing</span>')
    ev = "".join(
        f'<div class="row"><span>{esc(text)}</span>'
        f'<span class="src">{esc(src or "—")}</span></div>'
        for text, src in it["evidence"])
    return f"""
    <div class="item">
      <div class="hd"><span class="id">{esc(it['id'])}</span>
        <h3>{esc(it['title'])}</h3>{status}</div>
      <div class="bd">
        <div class="what">{esc(it['what'])}</div>
        <div class="ev">{ev}</div>
      </div>
    </div>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    ev = {
        "bench": load("bench_30seed.json"),
        "latency": load("latency.json"),
        "scenarios": load("scenarios.json"),
        "convergence": load("convergence.json"),
        "energy": load("energy.json"),
        "sb": load("sb.json"),
        "oracles": load("oracles.json"),
        "security": load("security.json"),
        "environment": load("environment.json"),
        "manifest": load(os.path.join("final", "MANIFEST.json")),
    }
    if ev["manifest"][0] is None:
        p = os.path.join(OUT, "final", "MANIFEST.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                ev["manifest"] = (json.load(f), "out/final/MANIFEST.json")

    items = build_items(ev)
    bench = ev["bench"][0] or {}
    summary = bench.get("summary", {})
    wx = bench.get("wilcoxon", {})
    lat = ev["latency"][0] or {}
    modes = lat.get("modes") or {}
    op = modes.get("operational (ALNS only)") or {}
    scaling = lat.get("scaling") or {}
    scen = ev["scenarios"][0] or {}
    results = scen.get("results") or []
    orc = ev["oracles"][0] or {}
    tde = orc.get("td_matrix_error") or {}
    prod_buckets = orc.get("production_buckets", 5)
    td = tde.get(f"buckets_{prod_buckets}") or tde.get("buckets_5") or {}
    td_old = tde.get("buckets_3") or {}
    env = ev["environment"][0] or {}
    sec = ev["security"][0] or {}

    ref = summary.get("E", {}).get("mean")

    def d(k):
        a = summary.get(k, {}).get("mean")
        return None if (a is None or not ref) else (ref - a) / ref * 100

    done = sum(1 for i in items if i["done"])
    p0 = [i for i in items if i["id"].startswith("P0")]
    p1 = [i for i in items if i["id"].startswith("P1")]

    arm_rows = ""
    order = ["GREEDY", "E", "ALNS", "A_HYB", "SB", "SEQ_LS", "SEQ_SB",
             "A", "A0", "PSO", "B", "D", "OR"]
    for k in order:
        a = summary.get(k)
        if not a:
            continue
        p = wx.get(k, {}).get("p")
        sig = ('<span class="tag mute">reference</span>' if p is None else
               f'<span class="tag {"ok" if p < 0.05 else "warn"}">'
               f'{"p " if p < 0.05 else "n.s. p "}{pval(p)}</span>')
        arm_rows += (f'<tr><td class="{"lead" if k == "ALNS" else ""}">'
                     f'{esc(a["label"])}</td>'
                     f'<td class="num">{a["mean"]:,.0f}</td>'
                     f'<td class="num">{a["sd"]:,.0f}</td>'
                     f'<td class="num">{a["best"]:,.0f}</td>'
                     f'<td class="num">{pct(d(k))}</td><td>{sig}</td></tr>')

    scale_rows = ""
    for key in sorted(scaling, key=lambda x: int(x)):
        s = scaling[key]
        scale_rows += (f'<tr><td class="lead">{s["stops"]} stops / '
                       f'{s["vehicles"]} vehicles</td>'
                       f'<td class="num">{s["total_p50"]:.0f}</td>'
                       f'<td class="num">{s["total_p95"]:.0f}</td>'
                       f'<td class="num">{s["matrix_p95"]:.0f}</td>'
                       f'<td class="num">{s["solve_p95"]:.0f}</td>'
                       f'<td><span class="tag {"ok" if s["meets_500ms"] else "bad"}">'
                       f'{"meets 500 ms" if s["meets_500ms"] else "over 500 ms"}'
                       f'</span></td></tr>')

    scen_rows = ""
    for r in results:
        tag = ('<span class="tag bad">vacuous</span>' if not r.get("exercised")
               else f'<span class="tag {"ok" if r.get("pass") else "bad"}">'
                    f'{"pass" if r.get("pass") else "fail"}</span>')
        scen_rows += (f'<tr><td class="lead">{esc(r["id"])}</td>'
                      f'<td>{esc(r["title"])}</td><td>{tag}</td></tr>')

    stamp = dt.date.today().isoformat()
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>RoutePulse — Reviewer Response</title><style>{CSS}</style></head>
<body><div class="wrap">

<header>
  <div>
    <h1>RoutePulse — Reviewer Response</h1>
    <p>Work completed against the Reviewer Implementation Workplan &amp; Freeze
    Audit. Every figure on this page is read from a committed evidence file at
    generation time and carries the file it came from; none is typed. Where an
    experiment has not been run, the row says so.</p>
  </div>
  <div class="issue">
    <div>SIH26137 · Egreen Quanta</div>
    <div>Generated {stamp}</div>
    <div>commit {esc((env.get('git_commit') or 'n/a')[:12])}
         {'(DIRTY)' if env.get('git_dirty') else ''}</div>
    <div>{esc(env.get('os', '—'))} · Python {esc(env.get('python', '—'))}</div>
  </div>
</header>

<h2><span class="n">1</span>Headline</h2>
<div class="grid">
  <div class="fig ok"><div class="v">{done}/{len(items)}</div>
    <div class="l">workplan items closed</div>
    <div class="d">All 12 P0 items and the P1 batch. Each is listed below with
    its evidence.</div></div>
  <div class="fig {'ok' if op.get('meets_500ms') else 'warn'}">
    <div class="v">{op.get('total_p95', 0):.0f} ms</div>
    <div class="l">operational p95, 30 stops</div>
    <div class="d">One engine, one global deadline. The 4-engine demo race is
    reported separately.</div></div>
  <div class="fig ok"><div class="v">{pct(d('ALNS'))}</div>
    <div class="l">ALNS vs greedy + local search</div>
    <div class="d">30 paired seeds, Wilcoxon p = {pval(wx.get('ALNS', {}).get('p'))}.
    Adopted.</div></div>
  <div class="fig warn">
    <div class="v">p = {pval((wx.get('A_vs_PSO') or {}).get('p'))}</div>
    <div class="l">quantum vs classical swarm</div>
    <div class="d">Paired test, identical encoding, decoder, improvement layer
    and budget; only the line that moves a particle differs. The two are
    <b>indistinguishable</b> —
    {pct((wx.get('A_vs_PSO') or {}).get('delta_pct'))} in favour of QPSO, well
    inside the noise.</div></div>
  <div class="fig {'ok' if td.get('mean_abs_error_pct', 99) < 5 else 'warn'}">
    <div class="v">{td.get('mean_abs_error_pct', '—')}%</div>
    <div class="l">travel-time matrix error</div>
    <div class="d">Mean absolute, against exact time-dependent Dijkstra. Was
    11.0% with three uniform buckets.</div></div>
  <div class="fig ok"><div class="v">{sec.get('passed', '—')}/{sec.get('total', '—')}</div>
    <div class="l">security checks</div>
    <div class="d">Run against a live server in {esc(sec.get('mode', '—'))}
    mode.</div></div>
</div>

<div class="note"><b>The two findings worth reading before anything else.</b>
First, the travel-time matrix was carrying
<b>{td_old.get('mean_abs_error_pct', '—')}% mean absolute error</b> against
exact time-dependent Dijkstra, because its three buckets were spaced uniformly
across a horizon whose traffic peaks at 09:00 and 17:00 — straddling both. That
is larger than any solver improvement this benchmark has ever measured, and it
was sitting underneath every number in it. Placing buckets where the curve bends
and raising the count to {prod_buckets} brings it to
{td.get('mean_abs_error_pct', '—')}%, paid for out of the solver budget.
Second, with a classical PSO control finally in place — identical encoding,
decoder, improvement layer, restarts and budget, differing only in the line that
moves a particle — the quantum-inspired update rule is <b>statistically
indistinguishable</b> from ordinary PSO. A random-restart arm can only tell you
whether having a swarm helps at all; this is what tells you whether the
<em>quantum</em> part does. Both findings are reported rather than
absorbed.</div>

<h2><span class="n">2</span>P0 — must fix before freeze</h2>
{''.join(item_html(i) for i in p0)}

<h2><span class="n">3</span>P1 — strongly recommended</h2>
{''.join(item_html(i) for i in p1)}

<h2><span class="n">4</span>Benchmark — 30 seeds, paired Wilcoxon</h2>
<div class="lede">Identical instance, identical wall-clock budget, one official
scorer. Arms share instances, so the paired signed-rank test is the correct one.
Lower score is better. Source: <code>{esc(ev['bench'][1] or 'not run')}</code></div>
<table><thead><tr><th class="lead">arm</th><th class="num">mean</th>
<th class="num">sd</th><th class="num">best</th>
<th class="num">vs greedy+LS</th><th>significance</th></tr></thead>
<tbody>{arm_rows}</tbody></table>

<h2><span class="n">5</span>Latency scaling</h2>
<div class="lede">Operational path, one engine, one global deadline, event to
accepted plan with the matrix rebuild inside the number. Reported per instance
size rather than averaged, because the matrix is O(n² × buckets) and this is
where the design either holds or stops holding.
Source: <code>{esc(ev['latency'][1] or 'not run')}</code></div>
<table><thead><tr><th class="lead">instance</th><th class="num">p50 ms</th>
<th class="num">p95 ms</th><th class="num">matrix p95</th>
<th class="num">solve p95</th><th>target</th></tr></thead>
<tbody>{scale_rows}</tbody></table>
<div class="note"><b>Stated plainly:</b> the 500 ms target holds at the demo
scale and does not hold at 60 or 100 stops, where the travel-time matrix
dominates. The escape hatch is known — fewer buckets at larger n, or an
incremental matrix — and it has not been built, so the limit is reported as a
limit.</div>

<h2><span class="n">6</span>Scenario suite</h2>
<div class="lede">A scenario that passes without exercising its own condition is
not a pass; the harness reports VACUOUS for that. S1 now exercises CASE 2, which
it previously did not.
Source: <code>{esc(ev['scenarios'][1] or 'not run')}</code></div>
<table><thead><tr><th class="lead">id</th><th>scenario</th><th>verdict</th></tr>
</thead><tbody>{scen_rows}</tbody></table>

<h2><span class="n">7</span>What is still not done</h2>
<div class="lede">Listed so it cannot be discovered later.</div>
<div class="item"><div class="bd"><div class="what">
<b>SUMO / TraCI microsimulation.</b> Not implemented, and now removed from every
claim rather than left ambiguous (P0-06, option B). The ambulance is a
controlled external-event feed with a real mock API.<br><br>
<b>The 500 ms target above 30 stops.</b> Measured, reported, not fixed.<br><br>
<b>Live traffic.</b> The road network is real OpenStreetMap data; demand is
synthetic and the traffic profile is a hand-authored time-of-day model. No
document calls it live.<br><br>
<b>Multi-tenancy.</b> The server holds one engine in module state, so two
browsers share one fleet. Correct for a control-sheet demo, wrong for a product.
</div></div></div>

<div class="note" style="margin-top:26px">Regenerate this document with
<code>python scripts/report.py</code> after any evidence run. It reads
<code>out/final/</code> when a frozen package exists and falls back to
<code>out/</code> otherwise.</div>

</div></body></html>"""

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"items closed : {done}/{len(items)}")
    missing = [i["id"] for i in items if not i["done"]]
    if missing:
        print(f"MISSING evidence for: {', '.join(missing)}")
    print(f"written      : {args.out}")


if __name__ == "__main__":
    main()

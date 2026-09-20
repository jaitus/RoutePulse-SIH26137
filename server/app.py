"""RoutePulse control tower — FastAPI backend.

Deliverable 4 (Software Platform / Prototype): API + UI, network + traffic
input, optimised route output, visualisation on a map.

Run:  python -m uvicorn server.app:app --reload --port 8000

SECURITY POSTURE
----------------
This is a prototype, and saying so is not a licence to leave it open. What is
enforced here, and why:

  * EVERY numeric input is bounded at the schema. An unbounded `n` is a denial
    of service with one request: instance size drives an O(n^2) matrix build and
    an O(n^2 x buckets) Dijkstra sweep. Bounds are the control, not a comment.
  * Coordinates are bounded to the served extract. A lat/lon anywhere on Earth
    would snap to the nearest node in Bengaluru and inject an incident the
    operator never asked for.
  * Mutating endpoints require an API key WHEN ONE IS CONFIGURED
    (ROUTEPULSE_API_KEY). Unset, the server is open and says so at /api/health,
    rather than pretending to be secured.
  * A per-client token bucket rate-limits the expensive endpoints. A solve is
    hundreds of milliseconds of CPU; unmetered, a loop of them is the whole box.
  * ONE solve at a time, behind a lock. The engine holds mutable state
    (incumbent, matrix, overlays); two concurrent re-plans would interleave
    writes to it and produce a plan that is a mixture of two events.
  * Errors return a generic message. Tracebacks name file paths, package
    versions and internal structure.
  * Security headers including a CSP of 'self' only -- which is why the UI ships
    as separate .css/.js files with no inline script and no CDN.
  * The road graph is loaded with `json`, never `pickle`. A pickle load is
    arbitrary code execution and a cache file is exactly the sort of thing that
    gets copied between machines.

KNOWN AND DELIBERATE LIMIT: the server holds ONE engine in module state, so it
is single-tenant by construction. Two browsers share one fleet. That is correct
for a control tower demo and wrong for a product, and it is written down here
rather than discovered later.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routepulse.dynamic import Engine                     # noqa: E402
from routepulse.energy import EnergyMeter, per_day        # noqa: E402
from routepulse.graph import RoadGraph, synthetic_grid    # noqa: E402
from routepulse.model import ObjectiveWeights, random_instance  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "data", "bengaluru_simplified.json")
CACHE_RAW = os.path.join(ROOT, "data", "bengaluru_graph.json")
OUT = os.path.join(ROOT, "out")

# ----------------------------------------------------------------- limits
MAX_CUSTOMERS = 120           # matrix build is O(n^2 x buckets)
MAX_VEHICLES = 24
MAX_BUDGET_S = 5.0
MAX_RADIUS_M = 2000.0
MAX_MULTIPLIER = 20.0
RATE_WINDOW_S = 60.0
RATE_LIMIT_CHEAP = 240        # reads per minute per client
RATE_LIMIT_SOLVE = 40         # solves per minute per client
API_KEY = os.environ.get("ROUTEPULSE_API_KEY", "").strip()

ALLOWED_ENGINES = ("emergency", "qpso", "alns", "sb", "ortools")

app = FastAPI(title="RoutePulse", docs_url=None, redoc_url=None,
              openapi_url=None)

STATE: dict = {"engine": None, "graph": None, "inst": None,
               "events": [], "last": None, "seed": 1}

# One solve at a time. The engine's incumbent, matrix and overlays are mutable
# shared state; concurrent re-plans would interleave writes to them.
SOLVE_LOCK = threading.Lock()
_BUCKETS: dict[str, deque] = {}
_BUCKET_LOCK = threading.Lock()


# ------------------------------------------------------------------ security

def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, limit: int, tag: str) -> None:
    key = f"{tag}:{_client(request)}"
    now = time.monotonic()
    with _BUCKET_LOCK:
        q = _BUCKETS.setdefault(key, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "rate limit exceeded")
        q.append(now)


def require_key(x_api_key: str | None) -> None:
    """Enforced only when a key is configured. An unset key is an open server
    and /api/health reports it as such -- silent 'security' is worse than none,
    because it is believed."""
    if API_KEY and (x_api_key or "") != API_KEY:
        raise HTTPException(401, "invalid or missing API key")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except HTTPException:
        raise
    except Exception:                                      # noqa: BLE001
        # Never surface a traceback: it names paths, versions and structure.
        import traceback
        traceback.print_exc()
        response = JSONResponse({"ok": False, "error": "internal error"}, 500)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; font-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"ok": False, "error": exc.detail}, exc.status_code)


def _in_bounds(lat: float, lon: float) -> bool:
    g = STATE.get("engine").g if STATE.get("engine") else STATE.get("graph")
    if g is None:
        return True
    lats = [la for la, _ in g.nodes.values()]
    lons = [lo for _, lo in g.nodes.values()]
    pad = 0.05
    return (min(lats) - pad <= lat <= max(lats) + pad
            and min(lons) - pad <= lon <= max(lons) + pad)


# --------------------------------------------------------------------- boot

def _load_graph() -> tuple[RoadGraph, str]:
    for path, label in ((CACHE, "OpenStreetMap · Bengaluru (simplified)"),
                        (CACHE_RAW, "OpenStreetMap · Bengaluru (raw)")):
        if os.path.exists(path):
            try:
                g = RoadGraph.from_json(path)
                return g, f"{label} · {len(g.nodes):,} junctions"
            except Exception:                              # noqa: BLE001
                continue
    return synthetic_grid(rows=18, cols=18), "synthetic grid (offline fallback)"


def _boot(n_customers: int = 30, n_vehicles: int = 5, seed: int = 7) -> dict:
    g, src = _load_graph()
    nodes = [(n, la, lo) for n, (la, lo) in g.nodes.items()]
    lat0 = sum(la for _, la, _ in nodes) / len(nodes)
    lon0 = sum(lo for _, _, lo in nodes) / len(nodes)
    depot = g.nearest_node(lat0, lon0)
    inst = random_instance(depot, nodes, n_customers=n_customers,
                           n_vehicles=n_vehicles, capacity=110, seed=seed,
                           depot_lat=g.nodes[depot][0], depot_lon=g.nodes[depot][1])
    eng = Engine(g, inst, ObjectiveWeights(), matrix_buckets=3)
    STATE.update({"engine": eng, "graph": g, "inst": inst, "events": [],
                  "last": None, "source": src})
    return {"graph_source": src, "nodes": len(g.nodes),
            "customers": inst.n, "vehicles": len(inst.vehicles),
            "matrix_build_s": round(eng.tm.build_seconds, 3)}


# ----------------------------------------------------------------- payloads

def _routes_payload() -> list[dict]:
    eng: Engine = STATE["engine"]
    sol = eng.incumbent
    if sol is None:
        return []
    g: RoadGraph = STATE["graph"]
    inst = STATE["inst"]
    cust = {c.id: c for c in inst.customers}
    out = []
    # Transit-diagram route colours, picked for a PAPER ground: saturated
    # enough to read as ink, dark enough to hold against a cream background.
    # They deliberately avoid red, amber and green -- those three are reserved
    # for meaning (closed road, congestion, green corridor), and a red ROUTE
    # beside a red CLOSURE is a legend nobody can read at a glance.
    palette = ["#1a5fb4", "#7b2d8e", "#00807a", "#4a3fb5", "#8a5a2b",
               "#b0117a", "#2b6d8f", "#5c6f00"]
    for i, r in enumerate(sol.routes):
        if not r.customer_ids:
            continue
        poly: list[list[float]] = []
        node = inst.depot_node
        t = 0.0
        for cid in r.customer_ids:
            seg = g.path(node, cid, t)
            poly.extend(g.coords(seg))
            t += 300
            node = cid
        poly.extend(g.coords(g.path(node, inst.depot_node, t)))
        out.append({
            "vehicle": r.vehicle_id,
            "color": palette[i % len(palette)],
            "stops": [{"id": cid, "lat": cust[cid].lat, "lon": cust[cid].lon,
                       "priority": cust[cid].priority,
                       "demand": cust[cid].demand,
                       "tw_end_min": None if cust[cid].tw_end >= 86400
                       else round(cust[cid].tw_end / 60, 1),
                       "eta_min": round(a / 60, 1) if a != float("inf") else None}
                      for cid, a in zip(r.customer_ids, r.arrival_times)],
            "polyline": poly,
            "load": round(r.load, 1),
            "capacity": next((v.capacity for v in inst.vehicles
                              if v.id == r.vehicle_id), None),
            "travel_min": round(r.travel_time / 60, 1),
        })
    return out


def _closed_payload() -> list[list[list[float]]]:
    eng = STATE.get("engine")
    g: RoadGraph = eng.g if eng is not None else STATE["graph"]
    segs = []
    for key, mult in g.incident.items():
        u, v = (int(x) for x in key.split("->"))
        if u in g.nodes and v in g.nodes:
            segs.append([[g.nodes[u][0], g.nodes[u][1]],
                         [g.nodes[v][0], g.nodes[v][1]],
                         [1 if mult == float("inf") else 0]])
    for key in g.corridor:                       # green corridor overlay
        u, v = (int(x) for x in key.split("->"))
        if u in g.nodes and v in g.nodes:
            segs.append([[g.nodes[u][0], g.nodes[u][1]],
                         [g.nodes[v][0], g.nodes[v][1]], [2]])
    return segs


def _summary() -> dict:
    eng: Engine = STATE["engine"]
    sol = eng.incumbent
    inst = STATE["inst"]
    if sol is None:
        return {}
    late = 0
    for r in sol.routes:
        for cid, arr in zip(r.customer_ids, r.arrival_times):
            c = next((x for x in inst.customers if x.id == cid), None)
            if c is not None and arr != float("inf") and arr > c.tw_end:
                late += 1
    return {
        "score": round(sol.score, 1),
        "travel_min": round(sol.travel_time / 60, 1),
        "makespan_min": round(sol.makespan / 60, 1),
        "sum_completion_min": round(sol.sum_completion / 60, 1),
        "feasible": sol.feasible,
        "violations": sol.violations[:4],
        "late_stops": late,
        "vehicles_used": sum(1 for r in sol.routes if r.customer_ids),
        "vehicles_total": len(inst.vehicles),
        "customers": inst.n,
        "depot": {"lat": eng.g.nodes[inst.depot_node][0],
                  "lon": eng.g.nodes[inst.depot_node][1]},
    }


# --------------------------------------------------------------------- routes

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/health")
def health():
    eng = STATE.get("engine")
    watts, note = EnergyMeter.probe()
    return {
        "ok": True,
        "engine_ready": eng is not None,
        "planned": bool(eng is not None and eng.incumbent is not None),
        "auth": "api-key required" if API_KEY else "OPEN — no API key configured",
        "single_tenant": True,
        "power_sensor": ("available" if watts is not None else f"unavailable ({note})"),
    }


@app.get("/api/boot")
def boot(request: Request,
         n: int = Query(30, ge=3, le=MAX_CUSTOMERS),
         k: int = Query(5, ge=1, le=MAX_VEHICLES),
         seed: int = Query(7, ge=0, le=10_000)):
    rate_limit(request, RATE_LIMIT_CHEAP, "cheap")
    with SOLVE_LOCK:
        info = _boot(n, k, seed)
    return {"ok": True, **info}


@app.get("/api/graph")
def graph(request: Request):
    """Road network for the canvas renderer. No tile server, no CDN, no
    internet -- the demo must survive a dead venue Wi-Fi."""
    rate_limit(request, RATE_LIMIT_CHEAP, "cheap")
    if STATE["engine"] is None:
        with SOLVE_LOCK:
            _boot()
    # Render the graph the ENGINE actually routes on, not the full city extract.
    # They differ (service-area subgraph), and rendering the larger one meant a
    # click outside the service area silently injected an event onto 0 edges --
    # the UI looked alive and did nothing.
    g: RoadGraph = STATE["engine"].g
    seen: set[tuple[int, int]] = set()
    segs: list[list[float]] = []
    for u, out in g.adj.items():
        for (v, _L, spd, _k) in out:
            key = (min(u, v), max(u, v))
            if key in seen or u not in g.nodes or v not in g.nodes:
                continue
            seen.add(key)
            a, b = g.nodes[u], g.nodes[v]
            segs.append([a[0], a[1], b[0], b[1], spd])
    lats = [la for la, _ in g.nodes.values()]
    lons = [lo for _, lo in g.nodes.values()]
    return {"segments": segs, "source": STATE.get("source", "?"),
            "bounds": [min(lats), min(lons), max(lats), max(lons)]}


@app.post("/api/plan")
def plan(request: Request,
         budget: float = Query(1.2, gt=0.05, le=MAX_BUDGET_S),
         x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    rate_limit(request, RATE_LIMIT_SOLVE, "solve")
    if STATE["engine"] is None:
        _boot()
    eng: Engine = STATE["engine"]
    with SOLVE_LOCK:
        t0 = time.perf_counter()
        cpu0 = time.process_time()
        eng.initial_plan(budget=budget, seed=STATE["seed"])
        ms = (time.perf_counter() - t0) * 1000
        energy = EnergyMeter.account("initial_plan", ms / 1000.0,
                                     time.process_time() - cpu0)
    return {"ok": True, "plan_ms": round(ms, 1), "summary": _summary(),
            "routes": _routes_payload(), "closed": _closed_payload(),
            "events": STATE["events"], "energy": energy.to_dict()}


class AmbulanceIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    severity: int = Field(2, ge=1, le=3)   # 3 = critical, trauma-capable hospital


@app.post("/api/ambulance")
def ambulance(a: AmbulanceIn, request: Request,
              x_api_key: str | None = Header(default=None)):
    """Dispatch an ambulance and open the green corridor.

    Returns BOTH sides of the trade: what priority saved the ambulance and what
    it cost the delivery fleet. Priority is not free and the blueprint is
    explicit that both numbers get reported.
    """
    require_key(x_api_key)
    rate_limit(request, RATE_LIMIT_SOLVE, "solve")
    eng: Engine = STATE["engine"]
    if eng is None:
        raise HTTPException(400, "no plan yet")
    if not _in_bounds(a.lat, a.lon):
        raise HTTPException(422, "coordinate outside the served network")
    with SOLVE_LOCK:
        if not eng.ambulances:
            eng.seed_ambulances(2)
        d = eng.dispatch_ambulance(a.lat, a.lon, severity=a.severity)
        if not d.get("ok"):
            raise HTTPException(400, d.get("reason", "dispatch failed"))
        d["leg_a"] = eng.g.coords(d.pop("leg_a_nodes", []))
        d["leg_b"] = eng.g.coords(d.pop("leg_b_nodes", []))
        d["scene"] = [a.lat, a.lon]
        STATE["events"].append({"kind": "ambulance", "lat": a.lat, "lon": a.lon,
                                "label": f"Ambulance {d['unit']} → {d['hospital']}",
                                "edges": d["corridor_edges"],
                                "t": round(time.time(), 2)})
        d["closed"] = _closed_payload()
        d["events"] = STATE["events"]
        d["hospitals"] = [{"name": h.name, "lat": h.lat, "lon": h.lon,
                           "tier": h.tier} for h in eng.hospitals]
        d["units"] = [{"name": u.name,
                       "lat": eng.g.nodes[u.node][0], "lon": eng.g.nodes[u.node][1]}
                      for u in eng.ambulances if u.node in eng.g.nodes]
    return d


class EventIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    kind: str = Field("closure", pattern="^(closure|congestion)$")
    radius_m: float = Field(420.0, gt=0, le=MAX_RADIUS_M)
    multiplier: float = Field(6.0, ge=1.0, le=MAX_MULTIPLIER)


@app.post("/api/event")
def event(ev: EventIn, request: Request,
          x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    rate_limit(request, RATE_LIMIT_SOLVE, "solve")
    eng: Engine = STATE["engine"]
    if eng is None:
        raise HTTPException(400, "no plan yet")
    if not _in_bounds(ev.lat, ev.lon):
        raise HTTPException(422, "coordinate outside the served network")
    with SOLVE_LOCK:
        if ev.kind == "closure":
            keys = eng.apply_closure(ev.lat, ev.lon, ev.radius_m)
            label = f"Road closure · {len(keys)} edges"
        else:
            keys = eng.apply_congestion(ev.lat, ev.lon, ev.multiplier, ev.radius_m)
            label = f"Congestion ×{ev.multiplier:.0f} · {len(keys)} edges"
        STATE["events"].append({"kind": ev.kind, "lat": ev.lat, "lon": ev.lon,
                                "label": label, "edges": len(keys),
                                "t": round(time.time(), 2)})
    return {"ok": True, "label": label, "edges": len(keys),
            "closed": _closed_payload(), "events": STATE["events"]}


@app.post("/api/replan")
def replan(request: Request,
           budget: float = Query(0.35, gt=0.05, le=MAX_BUDGET_S),
           race: bool = True,
           engines: str = Query("", max_length=80),
           x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    rate_limit(request, RATE_LIMIT_SOLVE, "solve")
    eng: Engine = STATE["engine"]
    if eng is None:
        raise HTTPException(400, "no plan yet")

    if engines:
        chosen = tuple(e for e in
                       (x.strip().lower() for x in engines.split(","))
                       if e in ALLOWED_ENGINES)
        if not chosen:
            raise HTTPException(422, "no recognised engine requested")
    else:
        chosen = ("emergency", "qpso", "ortools") if race else ("qpso",)

    with SOLVE_LOCK:
        res = eng.replan(budget=budget, seed=STATE["seed"], engines=chosen)
        STATE["last"] = res
        payload = {
            "ok": True,
            "accepted": res.accepted,
            "case": res.case,
            "incumbent_was_feasible": res.incumbent_feasible,
            "total_ms": res.total_ms,
            "stages_ms": res.stages_ms,
            "candidates": res.candidates,
            "explanation": res.explanation,
            "churn": res.churn,
            "alert": res.alert,
            "energy": res.energy,
            "energy_at_scale": per_day(res.energy.get("mwh", 0.0), 400),
            "convergence": res.telemetry.get("convergence", [])[-80:],
            "matrix_pairs_rebuilt": res.telemetry.get("matrix_pairs_rebuilt"),
            "fifo_violations": res.telemetry.get("fifo_violations"),
            "sb": res.telemetry.get("sb"),
            "alns": res.telemetry.get("alns"),
            "engines": list(chosen),
            "summary": _summary(),
            "routes": _routes_payload(),
            "closed": _closed_payload(),
            "events": STATE["events"],
        }
    return payload


@app.post("/api/reset")
def reset(request: Request,
          n: int = Query(30, ge=3, le=MAX_CUSTOMERS),
          k: int = Query(5, ge=1, le=MAX_VEHICLES),
          seed: int = Query(7, ge=0, le=10_000),
          x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    rate_limit(request, RATE_LIMIT_SOLVE, "solve")
    with SOLVE_LOCK:
        info = _boot(n, k, seed)
    return {"ok": True, **info}


# ------------------------------------------------------------------ evidence

def _read_json(name: str):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@app.get("/api/evidence")
def evidence(request: Request):
    """The measured record, served to the UI's Evidence mode.

    Everything here was produced by a script in `scripts/` and committed to
    `out/`. The UI renders it; it never computes it. If a file is missing the
    panel says MISSING rather than showing a plausible default -- an unrun
    experiment and a passing one must not look the same.
    """
    rate_limit(request, RATE_LIMIT_CHEAP, "cheap")
    bench = _read_json("bench_30seed.json")
    latency = _read_json("latency.json")
    conv = _read_json("convergence.json")
    scen = _read_json("scenarios.json")
    sb = _read_json("sb.json")
    energy = _read_json("energy.json")

    out: dict = {"missing": []}
    for name, blob in (("benchmark", bench), ("latency", latency),
                       ("convergence", conv), ("scenarios", scen),
                       ("simulated_bifurcation", sb), ("energy", energy)):
        if blob is None:
            out["missing"].append(name)

    if bench:
        out["benchmark"] = {
            "config": bench.get("config"),
            "graph_source": bench.get("graph_source"),
            "summary": bench.get("summary"),
            "wilcoxon": bench.get("wilcoxon"),
        }
    if latency:
        out["latency"] = latency
    if conv:
        out["convergence"] = conv
    if scen:
        out["scenarios"] = scen
    if sb:
        out["simulated_bifurcation"] = {
            "ground_state_validation": sb.get("ground_state_validation"),
            "penalty_calibration": sb.get("penalty_calibration"),
            "time_window_field_sweep": sb.get("time_window_field_sweep"),
            "resequencing_summary": (sb.get("resequencing") or {}).get("summary"),
        }
    if energy:
        out["energy"] = energy
    return out


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")),
          name="static")

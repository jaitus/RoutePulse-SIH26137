"""RoutePulse control tower — FastAPI backend.

Deliverable 4 (Software Platform / Prototype): API + UI, network + traffic
input, optimised route output, visualisation on a map.

Run:  python -m uvicorn server.app:app --reload --port 8000
"""
from __future__ import annotations

import os
import sys
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routepulse.dynamic import Engine                     # noqa: E402
from routepulse.graph import RoadGraph, synthetic_grid    # noqa: E402
from routepulse.model import ObjectiveWeights, random_instance  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "data", "bengaluru_simplified.json")
CACHE_RAW = os.path.join(ROOT, "data", "bengaluru_graph.json")

app = FastAPI(title="RoutePulse")

STATE: dict = {"engine": None, "graph": None, "inst": None,
               "events": [], "last": None, "seed": 1}


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


def _routes_payload() -> list[dict]:
    eng: Engine = STATE["engine"]
    sol = eng.incumbent
    if sol is None:
        return []
    g: RoadGraph = STATE["graph"]
    inst = STATE["inst"]
    cust = {c.id: c for c in inst.customers}
    out = []
    palette = ["#1b4b8f", "#0f6b46", "#98231f", "#8a5a00", "#5b3a8f",
               "#0b6b7a", "#a03f72", "#3f6b1f"]
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
                       "eta_min": round(a / 60, 1) if a != float("inf") else None}
                      for cid, a in zip(r.customer_ids, r.arrival_times)],
            "polyline": poly,
            "load": round(r.load, 1),
            "travel_min": round(r.travel_time / 60, 1),
        })
    return out


def _closed_payload() -> list[list[list[float]]]:
    g: RoadGraph = STATE["graph"]
    segs = []
    for key, mult in g.incident.items():
        u, v = (int(x) for x in key.split("->"))
        if u in g.nodes and v in g.nodes:
            segs.append([[g.nodes[u][0], g.nodes[u][1]],
                         [g.nodes[v][0], g.nodes[v][1]],
                         [1 if mult == float("inf") else 0]])
    return segs


def _summary() -> dict:
    eng: Engine = STATE["engine"]
    sol = eng.incumbent
    inst = STATE["inst"]
    if sol is None:
        return {}
    return {
        "score": round(sol.score, 1),
        "travel_min": round(sol.travel_time / 60, 1),
        "makespan_min": round(sol.makespan / 60, 1),
        "sum_completion_min": round(sol.sum_completion / 60, 1),
        "feasible": sol.feasible,
        "violations": sol.violations[:4],
        "vehicles_used": sum(1 for r in sol.routes if r.customer_ids),
        "customers": inst.n,
        "depot": {"lat": eng.g.nodes[inst.depot_node][0],
                  "lon": eng.g.nodes[inst.depot_node][1]},
    }


# --------------------------------------------------------------------- routes

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/boot")
def boot(n: int = 30, k: int = 5, seed: int = 7):
    info = _boot(n, k, seed)
    return {"ok": True, **info}


@app.get("/api/graph")
def graph():
    """Road network for the canvas renderer. No tile server, no CDN, no
    internet -- the demo must survive a dead venue Wi-Fi."""
    if STATE["engine"] is None:
        _boot()
    g: RoadGraph = STATE["graph"]
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
def plan(budget: float = 1.2):
    if STATE["engine"] is None:
        _boot()
    eng: Engine = STATE["engine"]
    t0 = time.perf_counter()
    eng.initial_plan(budget=budget, seed=STATE["seed"])
    ms = (time.perf_counter() - t0) * 1000
    return {"ok": True, "plan_ms": round(ms, 1), "summary": _summary(),
            "routes": _routes_payload(), "closed": _closed_payload(),
            "events": STATE["events"]}


class EventIn(BaseModel):
    lat: float
    lon: float
    kind: str = "closure"          # closure | congestion
    radius_m: float = 420.0
    multiplier: float = 6.0


@app.post("/api/event")
def event(ev: EventIn):
    eng: Engine = STATE["engine"]
    if eng is None:
        return JSONResponse({"ok": False, "error": "no plan yet"}, 400)
    if ev.kind == "closure":
        keys = eng.apply_closure(ev.lat, ev.lon, ev.radius_m)
        label = f"Road closure ({len(keys)} edges)"
    else:
        keys = eng.apply_congestion(ev.lat, ev.lon, ev.multiplier, ev.radius_m)
        label = f"Severe congestion x{ev.multiplier:.0f} ({len(keys)} edges)"
    STATE["events"].append({"kind": ev.kind, "lat": ev.lat, "lon": ev.lon,
                            "label": label, "edges": len(keys)})
    return {"ok": True, "label": label, "edges": len(keys),
            "closed": _closed_payload(), "events": STATE["events"]}


@app.post("/api/replan")
def replan(budget: float = 0.35, race: bool = True):
    eng: Engine = STATE["engine"]
    if eng is None:
        return JSONResponse({"ok": False, "error": "no plan yet"}, 400)
    engines = ("emergency", "qpso", "ortools") if race else ("qpso",)
    res = eng.replan(budget=budget, seed=STATE["seed"], engines=engines)
    STATE["last"] = res
    return {
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
        "convergence": res.telemetry.get("convergence", [])[-60:],
        "matrix_pairs_rebuilt": res.telemetry.get("matrix_pairs_rebuilt"),
        "fifo_violations": res.telemetry.get("fifo_violations"),
        "summary": _summary(),
        "routes": _routes_payload(),
        "closed": _closed_payload(),
        "events": STATE["events"],
    }


@app.post("/api/reset")
def reset(n: int = 30, k: int = 5, seed: int = 7):
    info = _boot(n, k, seed)
    return {"ok": True, **info}


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")),
          name="static")

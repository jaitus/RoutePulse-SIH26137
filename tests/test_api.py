"""API contract: bounds, auth, and the endpoints that must not mutate.

These run in-process against the ASGI app, so they cover the same code the
security script probes over HTTP but without needing a live server.
"""
from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    import server.app as app_module
    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        yield c


# ------------------------------------------------------------------- bounds

@pytest.mark.parametrize("body,reason", [
    ({"lat": 999, "lon": 77.6, "kind": "closure"}, "latitude off the globe"),
    ({"lat": 12.97, "lon": 77.59, "kind": "'; DROP TABLE"}, "unknown kind"),
    ({"lat": 12.97, "lon": 77.59, "radius_m": 1e9}, "absurd radius"),
    ({"lat": 12.97, "lon": 77.59, "kind": "congestion", "multiplier": -5},
     "negative multiplier"),
])
def test_event_rejects_out_of_range_input(client, body, reason):
    r = client.post("/api/event", json=body)
    assert r.status_code == 422, f"{reason} was accepted"


def test_replan_rejects_an_unknown_engine(client):
    assert client.post("/api/replan?engines=rm-rf").status_code == 422


def test_replan_rejects_an_out_of_range_budget(client):
    assert client.post("/api/replan?budget=999").status_code == 422


# ------------------------------------------------------- boot is read-only

def test_boot_is_read_only(client):
    """P0-12. A public unauthenticated GET must not rebuild the simulation."""
    client.post("/api/plan?budget=0.4")
    before = client.get("/api/boot").json()
    assert before["planned"] is True

    # the old signature took n/k/seed and rebuilt; those must now be ignored
    after = client.get("/api/boot?n=90&k=20&seed=999").json()
    assert after["planned"] is True, "GET /api/boot destroyed the plan"
    assert after["customers"] == before["customers"]
    assert after["vehicles"] == before["vehicles"]


def test_boot_reports_the_clock_and_horizon(client):
    j = client.get("/api/boot").json()
    assert "sim_clock_s" in j and "horizon" in j


# ------------------------------------------------------------------- auth

def test_open_mode_declares_itself(client):
    j = client.get("/api/health").json()
    assert "OPEN" in j["auth"]


# -------------------------------------------------------------- one action

def test_ambulance_dispatch_recovers_the_fleet_in_one_call(client):
    """P0-03. Dispatch -> corridor -> fleet recovery, without a second click."""
    client.post("/api/reset")
    client.post("/api/plan?budget=0.6")
    g = client.get("/api/graph").json()
    lat = (g["bounds"][0] + g["bounds"][2]) / 2
    lon = (g["bounds"][1] + g["bounds"][3]) / 2
    r = client.post("/api/ambulance?budget=0.3",
                    json={"lat": lat, "lon": lon, "severity": 2})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"]
    assert "recovery" in j, "the fleet did not react to the dispatch"
    rec = j["recovery"]
    assert "case" in rec and "total_ms" in rec
    assert rec["routes"], "no routes came back from the recovery"


def test_headers_are_hardened(client):
    h = {k.lower(): v for k, v in client.get("/").headers.items()}
    csp = h.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"


def test_interactive_docs_are_not_served(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404

"""Cold-server semantics — P0-05.

`GET /api/boot` no longer accepts caller-controlled rebuild parameters, but
until this file existed it still called `_boot()` when the engine was missing.
That meant an unauthenticated cold GET allocated a graph, an instance and two
travel-time matrices. "It does not take parameters" is not the same as "it does
not mutate", and only the second is a read-only endpoint.

Own module because the cold app must start with ROUTEPULSE_NO_PRELOAD set,
which means reloading `server.app` — and that reload would otherwise leak into
every other TestClient in the same file.
"""
from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def cold_client():
    os.environ["ROUTEPULSE_NO_PRELOAD"] = "1"
    import server.app as app_module
    importlib.reload(app_module)
    try:
        with TestClient(app_module.app) as c:
            yield c, app_module
    finally:
        os.environ.pop("ROUTEPULSE_NO_PRELOAD", None)
        importlib.reload(app_module)


def test_cold_get_boot_creates_nothing(cold_client):
    c, mod = cold_client
    assert mod.STATE["engine"] is None, "preload should be disabled here"
    j = c.get("/api/boot").json()
    assert j["ok"] is True
    assert j["engine_ready"] is False
    assert j["planned"] is False
    assert mod.STATE["engine"] is None, "GET /api/boot created server state"


def test_cold_get_boot_is_stable(cold_client):
    c, mod = cold_client
    first = c.get("/api/boot").json()
    second = c.get("/api/boot").json()
    assert first == second
    assert mod.STATE["engine"] is None


def test_cold_reads_that_need_an_engine_say_so(cold_client):
    c, _mod = cold_client
    assert c.get("/api/graph").status_code == 503
    assert c.post("/api/plan?budget=0.3").status_code == 503


def test_coordinate_validation_works_before_any_engine_exists(cold_client):
    """`_in_bounds` used to return True when no graph was loaded, which made
    the service-area check vacuous on exactly the request that creates state.
    Bounds now come from the cached graph file without building an engine."""
    c, mod = cold_client
    assert mod.STATE["engine"] is None
    # Paris is outside the served extract and must be rejected on a cold server
    r = c.post("/api/event", json={"lat": 48.8566, "lon": 2.3522,
                                   "kind": "closure"})
    assert r.status_code in (422, 503), r.text
    assert r.status_code == 422, "bounds should be known from the cached graph"
    assert mod.STATE["engine"] is None


def test_reset_is_what_creates_state(cold_client):
    c, mod = cold_client
    assert mod.STATE["engine"] is None
    r = c.post("/api/reset")
    assert r.status_code == 200
    assert r.json()["engine_ready"] is True
    assert mod.STATE["engine"] is not None

    after = c.get("/api/boot").json()
    assert after["engine_ready"] is True
    assert after["customers"] == 30

    # and reading it again still does not change anything
    again = c.get("/api/boot").json()
    assert again == after

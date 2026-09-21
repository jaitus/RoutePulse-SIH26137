"""API-key enforcement.

In its own module on purpose. `require_key` reads a module-level constant set
at import, so a fixture that reloads `server.app` with a key configured also
changes the module every other TestClient in the same file is bound to. Kept
separate, the reload cannot leak into the open-mode tests.
"""
from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

KEY = "unit-test-key"


@pytest.fixture(scope="module")
def keyed_client():
    os.environ["ROUTEPULSE_API_KEY"] = KEY
    import server.app as app_module
    importlib.reload(app_module)
    try:
        with TestClient(app_module.app) as c:
            yield c
    finally:
        os.environ.pop("ROUTEPULSE_API_KEY", None)
        importlib.reload(app_module)      # leave the module open for other files


def test_health_reports_the_key_as_enforced(keyed_client):
    assert keyed_client.get("/api/health").json()["auth"] == "api-key required"


def test_mutating_endpoint_refuses_a_missing_key(keyed_client):
    assert keyed_client.post("/api/reset").status_code == 401


def test_mutating_endpoint_refuses_a_wrong_key(keyed_client):
    assert keyed_client.post("/api/reset",
                             headers={"X-API-Key": "wrong"}).status_code == 401


def test_mutating_endpoint_accepts_the_right_key(keyed_client):
    assert keyed_client.post(
        "/api/reset", headers={"X-API-Key": KEY}).status_code == 200


def test_the_mock_feed_is_behind_the_key_too(keyed_client):
    """The external ambulance feed writes state, so it is a mutating endpoint
    like any other -- an unauthenticated telemetry push would let anyone move
    an ambulance."""
    body = {"ambulance_id": 0, "lat": 12.95, "lon": 77.60, "status": "enroute"}
    assert keyed_client.post("/api/mock/ambulance/telemetry",
                             json=body).status_code == 401


def test_read_only_endpoints_stay_readable(keyed_client):
    """P0-12 was about GET /api/boot not MUTATING, not about hiding it."""
    assert keyed_client.get("/api/boot").status_code == 200
    assert keyed_client.get("/api/health").status_code == 200

"""Tests for the FastAPI read API.

Uses FastAPI's TestClient — no external HTTP server needed.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.routes import app
from src.normalizer.schema import make_trend, TrendSignal
from src.storage.db import Storage
from datetime import datetime, timezone


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    # Patch the storage to use a temp DB
    storage = Storage(tmp_path / "test_api.db")

    # Insert some test data
    now = datetime.now(tz=timezone.utc)
    t1 = make_trend(
        platform="tiktok_oembed", name="#aiart", trend_type="hashtag",
        platform_native_id="1", url="https://example.com/1", score=1000.0,
    )
    t1.signals = [TrendSignal(captured_at=now, score=1000.0, rank=1)]
    t2 = make_trend(
        platform="x", name="#aiart", trend_type="topic",
        platform_native_id="2", url=None, score=500.0,
    )
    t2.signals = [TrendSignal(captured_at=now, score=500.0, rank=2)]
    t3 = make_trend(
        platform="tiktok_oembed", name="#cats", trend_type="hashtag",
        platform_native_id="3", url=None, score=50.0,
    )
    t3.signals = [TrendSignal(captured_at=now, score=50.0, rank=3)]
    storage.upsert_many([t1, t2, t3])
    storage.record_run("tiktok_oembed", now, now, "success", 2)
    storage.record_run("x", now, now, "success", 1)

    # Patch the _get_storage function in the routes module
    import src.api.routes as routes_mod
    original_storage = routes_mod._storage
    routes_mod._storage = storage

    with TestClient(app) as c:
        yield c

    # Cleanup
    routes_mod._storage = original_storage
    storage.close()


def test_healthz(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert "trends_total" in data
    assert data["trends_total"] == 3


def test_list_trends_all(client: TestClient):
    resp = client.get("/api/trends")
    assert resp.status_code == 200
    trends = resp.json()
    assert len(trends) == 3


def test_list_trends_by_platform(client: TestClient):
    resp = client.get("/api/trends?platform=tiktok_oembed")
    assert resp.status_code == 200
    trends = resp.json()
    assert len(trends) == 2
    assert all(t["platform"] == "tiktok_oembed" for t in trends)


def test_list_trends_min_score(client: TestClient):
    resp = client.get("/api/trends?min_score=100")
    assert resp.status_code == 200
    trends = resp.json()
    assert len(trends) == 2  # aiart (1000) + aiart (500), cats (50) excluded


def test_list_trends_invalid_platform(client: TestClient):
    resp = client.get("/api/trends?platform=myspace")
    assert resp.status_code == 400


def test_trends_by_platform_endpoint(client: TestClient):
    resp = client.get("/api/trends/x")
    assert resp.status_code == 200
    trends = resp.json()
    assert len(trends) == 1
    assert trends[0]["platform"] == "x"


def test_get_trend_by_id(client: TestClient):
    # First, list to get an ID
    resp = client.get("/api/trends?platform=tiktok_oembed&limit=1")
    trend_id = resp.json()[0]["id"]

    resp = client.get(f"/api/trend/{trend_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == trend_id
    assert "snapshots" in data
    assert len(data["snapshots"]) >= 1


def test_get_trend_not_found(client: TestClient):
    resp = client.get("/api/trend/nonexistent:12345")
    assert resp.status_code == 404


def test_list_runs(client: TestClient):
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 2


def test_list_runs_by_platform(client: TestClient):
    resp = client.get("/api/runs?platform=tiktok_oembed")
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 1
    assert runs[0]["platform"] == "tiktok_oembed"


def test_groups_endpoint(client: TestClient):
    """Test the semantic groups endpoint (will use exact fallback since
    Ollama is not running in test env)."""
    resp = client.get("/api/groups")
    assert resp.status_code == 200
    groups = resp.json()
    # aiart appears on tiktok_oembed + x → one group; cats → another group
    assert len(groups) == 2
    aiart_group = next(g for g in groups if "aiart" in g["canonical_name"].lower())
    assert aiart_group["member_count"] == 2
    assert sorted(aiart_group["platforms"]) == ["tiktok_oembed", "x"]
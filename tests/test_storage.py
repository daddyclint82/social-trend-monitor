"""Tests for the SQLite storage layer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.normalizer.schema import make_trend
from src.storage.db import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


def test_upsert_and_list(storage: Storage):
    t = make_trend(
        platform="tiktok",
        name="#aiart",
        trend_type="hashtag",
        platform_native_id="42",
        url="https://example.com",
        score=1234.0,
    )
    storage.upsert_trend(t)
    rows = storage.list_trends(platform="tiktok")
    assert len(rows) == 1
    assert rows[0]["name"] == "#aiart"
    assert rows[0]["latest_score"] == 1234.0


def test_upsert_updates_existing(storage: Storage):
    from datetime import datetime, timezone
    from src.normalizer.schema import TrendSignal

    now = datetime.now(tz=timezone.utc)
    t1 = make_trend(
        platform="x", name="#aiart", trend_type="topic",
        platform_native_id="woeid:1:aiart", url=None, score=100.0,
    )
    t1.signals = [TrendSignal(captured_at=now, score=100.0)]
    storage.upsert_trend(t1)

    t2 = make_trend(
        platform="x", name="#aiart", trend_type="topic",
        platform_native_id="woeid:1:aiart", url=None, score=200.0,
    )
    t2.signals = [TrendSignal(captured_at=now, score=200.0)]
    storage.upsert_trend(t2)
    rows = storage.list_trends(platform="x")
    assert len(rows) == 1
    assert rows[0]["latest_score"] == 200.0
    snapshots = storage.get_snapshots(rows[0]["id"])
    assert len(snapshots) == 2  # both writes recorded as snapshots


def test_record_run(storage: Storage):
    now = datetime.now(tz=timezone.utc)
    rid = storage.record_run(
        platform="tiktok", started_at=now, finished_at=now,
        status="success", items_collected=42,
    )
    assert rid > 0
    last = storage.last_run("tiktok")
    assert last is not None
    assert last["status"] == "success"
    assert last["items_collected"] == 42


def test_health(storage: Storage):
    h = storage.health()
    assert "trends_total" in h
    assert "by_platform" in h
    assert "last_runs" in h
    assert "now" in h

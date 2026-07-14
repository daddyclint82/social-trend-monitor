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
        platform="tiktok_oembed",
        name="#aiart",
        trend_type="hashtag",
        platform_native_id="42",
        url="https://example.com",
        score=1234.0,
    )
    storage.upsert_trend(t)
    rows = storage.list_trends(platform="tiktok_oembed")
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
        platform="tiktok_oembed", started_at=now, finished_at=now,
        status="success", items_collected=42,
    )
    assert rid > 0
    last = storage.last_run("tiktok_oembed")
    assert last is not None
    assert last["status"] == "success"
    assert last["items_collected"] == 42


def test_health(storage: Storage):
    h = storage.health()
    assert "trends_total" in h
    assert "by_platform" in h
    assert "last_runs" in h
    assert "now" in h


# --- ADR-0013: list_trends trend_type filter + metadata parsing ---

def test_list_trends_filter_by_trend_type(storage: Storage):
    """The new trend_type filter (ADR-0013) returns only matching rows."""
    now = datetime.now(tz=timezone.utc)
    # Insert 3 different trend types
    for name, ttype in [("#hashtag1", "hashtag"), ("topic1", "search"), ("video1", "video")]:
        t = make_trend(
            platform="tiktok_oembed" if ttype == "hashtag" else ("google_trends" if ttype == "search" else "youtube"),
            name=name,
            trend_type=ttype,
            platform_native_id=f"{ttype}:{name}",
            url="https://example.com",
            score=1.0,
        )
        storage.upsert_trend(t)

    assert len(storage.list_trends(trend_type="hashtag")) == 1
    assert len(storage.list_trends(trend_type="search")) == 1
    assert len(storage.list_trends(trend_type="video")) == 1
    # No filter → all 3
    assert len(storage.list_trends()) >= 3


def test_list_trends_combined_platform_and_type_filter(storage: Storage):
    """Both filters work together."""
    for ttype in ("hashtag", "search", "video"):
        t = make_trend(
            platform="google_trends",
            name=f"gt-{ttype}",
            trend_type=ttype,
            platform_native_id=f"gt-{ttype}",
            url="https://example.com",
            score=1.0,
        )
        storage.upsert_trend(t)

    rows = storage.list_trends(platform="google_trends", trend_type="search")
    assert len(rows) == 1
    assert rows[0]["name"] == "gt-search"


def test_list_trends_parses_metadata_json(storage: Storage):
    """metadata_json column is parsed into a `metadata` dict for callers (ADR-0013)."""
    t = make_trend(
        platform="google_trends",
        name="test topic",
        trend_type="search",
        platform_native_id="test:1",
        url="https://example.com",
        score=5.0,
        metadata={"geo": "US", "pub_date": "2026-07-13T18:10:00", "news_titles": ["headline 1", "headline 2"]},
    )
    storage.upsert_trend(t)

    rows = storage.list_trends(platform="google_trends")
    assert len(rows) == 1
    assert rows[0]["metadata"]["geo"] == "US"
    assert rows[0]["metadata"]["pub_date"] == "2026-07-13T18:10:00"
    assert rows[0]["metadata"]["news_titles"] == ["headline 1", "headline 2"]
    # Raw column is still present for backward compat
    assert "metadata_json" in rows[0]


def test_list_trends_handles_missing_metadata_json(storage: Storage):
    """A trend with empty metadata doesn't crash the parser."""
    t = make_trend(
        platform="tiktok_oembed",
        name="#fyp",
        trend_type="hashtag",
        platform_native_id="fyp:1",
        url="https://example.com",
        score=0.0,
    )
    storage.upsert_trend(t)
    rows = storage.list_trends(platform="tiktok_oembed")
    assert len(rows) == 1
    assert rows[0]["metadata"] == {}


def test_list_trends_handles_corrupt_metadata_json(storage: Storage):
    """Corrupt JSON in metadata_json → empty dict, no crash."""
    import sqlite3
    # Manually insert a row with corrupt JSON
    with sqlite3.connect(str(storage.db_path)) if hasattr(storage, 'db_path') else storage.cursor() as cur:
        pass
    # Use the public API to insert a valid row, then corrupt it via direct SQL
    t = make_trend(
        platform="tiktok_oembed",
        name="#x",
        trend_type="hashtag",
        platform_native_id="x:1",
        url="https://example.com",
        score=0.0,
        metadata={"valid": "value"},
    )
    storage.upsert_trend(t)
    # Corrupt the metadata_json column directly
    with storage.cursor() as cur:
        cur.execute(
            "UPDATE trends SET metadata_json = ? WHERE id = ?",
            ("NOT VALID JSON", t.id),
        )
    rows = storage.list_trends(platform="tiktok_oembed")
    # Should not raise; metadata should be empty dict on fallback
    assert len(rows) == 1
    assert rows[0]["metadata"] == {}

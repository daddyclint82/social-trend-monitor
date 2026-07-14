"""Tests for the Apify vendor bridge collector (ADR-0012).

Covers: token resolution, spend ledger math, monthly + cycle cap gating,
mapper functions (TikTok + Instagram), and end-to-end collect() with
mocked HTTP. No real network calls.
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors.platforms.apify import (
    ApifyBridgeCollector,
    ApifySpendLedger,
    _instagram_post_to_trend,
    _tiktok_post_to_trend,
)


# ---------- helpers ----------


def _make_collector(config: dict) -> tuple[ApifyBridgeCollector, MagicMock]:
    http = MagicMock()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    c = ApifyBridgeCollector(http_client=http, rate_limiter=limiter, config=config)
    return c, http


def _tiktok_item(hashtags=None, plays=10000, post_id="abc"):
    return {
        "id": post_id,
        "text": "Sample tiktok",
        # Use 'is None' (not 'or') so explicit [] stays []
        "hashtags": [{"name": "fyp"}, {"name": "viral"}] if hashtags is None else hashtags,
        "playCount": plays,
        "diggCount": 500,
        "shareCount": 100,
        "commentCount": 30,
        "authorMeta": {"name": "creator1", "fans": 50000},
        "webVideoUrl": f"https://tiktok.com/@creator1/video/{post_id}",
    }


def _instagram_item(hashtags=None, likes=2000, post_id="xyz"):
    return {
        "id": post_id,
        "type": "Video",
        "shortCode": post_id,
        "caption": "Cool reel",
        "hashtags": hashtags or ["aiart", "design"],
        "likesCount": likes,
        "commentsCount": 20,
        "ownerUsername": "cooluser",
        "url": f"https://www.instagram.com/p/{post_id}/",
    }


def _http_response(payload, status=200, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = headers or {}
    resp.text = ""
    return resp


# ---------- credential resolution ----------


def test_token_from_literal_config():
    c, _ = _make_collector({"token": "abc123"})
    assert c._resolve_token() == "abc123"


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "env_tok")
    c, _ = _make_collector({})
    assert c._resolve_token() == "env_tok"


def test_token_from_custom_env(monkeypatch):
    monkeypatch.setenv("MY_APIFY", "x")
    c, _ = _make_collector({"token_env": "MY_APIFY"})
    assert c._resolve_token() == "x"


def test_token_missing_returns_none():
    c, _ = _make_collector({})
    assert c._resolve_token() is None


def test_collect_skips_without_token():
    c, _ = _make_collector({})
    trends = asyncio.run(c.collect())
    assert trends == []


# ---------- spend ledger ----------


def test_ledger_starts_at_zero():
    with tempfile.TemporaryDirectory() as d:
        ledger = ApifySpendLedger(str(Path(d) / "spend.db"))
        assert ledger.month_total() == 0.0
        assert ledger.cycle_total() == 0.0
        ledger.close()


def test_ledger_records_and_sums():
    with tempfile.TemporaryDirectory() as d:
        ledger = ApifySpendLedger(str(Path(d) / "spend.db"))
        ledger.record(actor_id="actor_a", usd=0.05, items=50)
        ledger.record(actor_id="actor_b", usd=0.03, items=30)
        assert ledger.month_total() == pytest.approx(0.08)
        assert ledger.cycle_total() == pytest.approx(0.08)
        ledger.close()


def test_ledger_persists_across_instances():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "spend.db")
        l1 = ApifySpendLedger(path)
        l1.record(actor_id="x", usd=0.10, items=10)
        l1.close()
        l2 = ApifySpendLedger(path)
        assert l2.month_total() == pytest.approx(0.10)
        # Fresh process = fresh cycle counter
        assert l2.cycle_total() == 0.0
        l2.close()


def test_ledger_creates_table_on_init():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "fresh.db")
        ApifySpendLedger(path)
        # Verify the table exists by querying it
        conn = sqlite3.connect(path)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='apify_spend'"
        )
        assert cur.fetchone() is not None
        conn.close()


# ---------- cost header extraction ----------


def test_extract_cost_from_headers_present():
    from src.collectors.platforms.apify import _extract_cost_from_headers

    class H:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=None):
            return self._d.get(k, default)

    assert _extract_cost_from_headers(H({"x-apify-usage-total-usd": "0.05"})) == 0.05
    assert _extract_cost_from_headers(H({"x-apify-usage-actor-usd": "0.02"})) == 0.02
    assert _extract_cost_from_headers(H({})) == 0.0
    assert _extract_cost_from_headers(H({"x-apify-usage": "not-a-number"})) == 0.0


# ---------- mappers ----------


def test_tiktok_mapper_basic():
    actor = {"actor_id": "clockworks~tiktok-scraper", "source_platform": "tiktok"}
    item = _tiktok_item(hashtags=[{"name": "fyp"}, {"name": "viral"}])
    t = _tiktok_post_to_trend(item, actor=actor)
    assert t.platform == "apify"
    assert t.trend_type == "hashtag"
    assert t.name == "#fyp"
    assert t.score == 10000.0
    assert t.metadata["source_platform"] == "tiktok"
    assert t.metadata["apify_actor"] == "clockworks~tiktok-scraper"
    assert "fyp" in t.metadata["all_hashtags"]


def test_tiktok_mapper_string_hashtags():
    actor = {"actor_id": "x", "source_platform": "tiktok"}
    item = _tiktok_item(hashtags=["fyp", "viral"])
    t = _tiktok_post_to_trend(item, actor=actor)
    assert t.name == "#fyp"


def test_tiktok_mapper_no_hashtags_falls_back_to_text():
    actor = {"actor_id": "x", "source_platform": "tiktok"}
    item = _tiktok_item(hashtags=[])
    # _tiktok_item already sets text="Sample tiktok"
    t = _tiktok_post_to_trend(item, actor=actor)
    assert t.trend_type == "post"
    # Mapper still prepends '#' to make it consistent with hashtag trends
    assert t.name == "#Sample tiktok"


def test_instagram_mapper_basic():
    actor = {"actor_id": "apify~instagram-scraper", "source_platform": "instagram"}
    item = _instagram_item(hashtags=["aiart", "design"])
    t = _instagram_post_to_trend(item, actor=actor)
    assert t.platform == "apify"
    assert t.trend_type == "hashtag"
    assert t.name == "#aiart"
    assert t.score == 2000.0
    assert t.metadata["source_platform"] == "instagram"
    assert t.metadata["apify_actor"] == "apify~instagram-scraper"


def test_instagram_mapper_url_from_shortcode():
    actor = {"actor_id": "x", "source_platform": "instagram"}
    item = {"id": "1", "shortCode": "ABC123", "hashtags": [], "likesCount": 0}
    t = _instagram_post_to_trend(item, actor=actor)
    assert t.url == "https://www.instagram.com/p/ABC123/"


# ---------- end-to-end collect() ----------


def test_collect_runs_actors_and_maps_results():
    """Happy path: token present, one actor, one item returned."""
    c, http = _make_collector(
        {
            "token": "tok",
            "monthly_cap_usd": 0,  # disable cap check (no ledger)
            "per_cycle_cap_usd": 0,
            "actors": [
                {
                    "actor_id": "test~tiktok",
                    "source_platform": "tiktok",
                    "input": {"hashtags": ["fyp"]},
                    "item_mapper": "tiktok_post",
                }
            ],
        }
    )

    async def fake_post(*args, **kwargs):
        resp = _http_response([_tiktok_item()], headers={"x-apify-usage-total-usd": "0.01"})
        return resp

    http.post = AsyncMock(side_effect=fake_post)
    c.limiter = MagicMock()
    c.limiter.acquire = AsyncMock()
    # Use the same http mock
    c.http = http

    trends = asyncio.run(c.collect())
    assert len(trends) == 1
    assert trends[0].platform == "apify"
    assert trends[0].metadata["source_platform"] == "tiktok"
    assert http.post.call_count == 1


def test_collect_blocks_on_monthly_cap():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "spend.db")
        ledger = ApifySpendLedger(path)
        ledger.record(actor_id="x", usd=0.50, items=1)
        ledger.close()

        ledger2 = ApifySpendLedger(path)
        c, http = _make_collector(
            {
                "token": "tok",
                "monthly_cap_usd": 0.40,  # already exceeded
                "per_cycle_cap_usd": 0,
                "actors": [
                    {
                        "actor_id": "a",
                        "source_platform": "tiktok",
                        "input": {},
                        "item_mapper": "tiktok_post",
                    }
                ],
                "_spend_ledger": ledger2,
            }
        )
        http.post = AsyncMock()
        c.limiter.acquire = AsyncMock()
        trends = asyncio.run(c.collect())
        assert trends == []
        assert http.post.call_count == 0
        ledger2.close()


def test_collect_blocks_on_cycle_cap():
    """If the per-cycle cap is already exhausted (in-memory on the ledger), bail without HTTP."""
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "spend.db")
        ledger = ApifySpendLedger(path)
        # Seed the in-process cycle counter above the cap
        ledger._cycle_spent = 0.50
        c, http = _make_collector(
            {
                "token": "tok",
                "monthly_cap_usd": 10.0,
                "per_cycle_cap_usd": 0.10,
                "actors": [
                    {
                        "actor_id": "a",
                        "source_platform": "tiktok",
                        "input": {},
                        "item_mapper": "tiktok_post",
                    },
                    {
                        "actor_id": "b",
                        "source_platform": "instagram",
                        "input": {},
                        "item_mapper": "instagram_post",
                    },
                ],
                "_spend_ledger": ledger,
            }
        )
        c.limiter.acquire = AsyncMock()
        http.post = AsyncMock()
        trends = asyncio.run(c.collect())
        assert trends == []
        assert http.post.call_count == 0
        ledger.close()


def test_collect_skips_actor_too_soon():
    """An actor run within min_interval_hours is skipped."""
    c, http = _make_collector(
        {
            "token": "tok",
            "monthly_cap_usd": 0,
            "per_cycle_cap_usd": 0,
            "min_interval_hours": 24,
            "actors": [
                {
                    "actor_id": "recently_run",
                    "source_platform": "tiktok",
                    "input": {},
                    "item_mapper": "tiktok_post",
                }
            ],
        }
    )
    # Mark as just run
    import time as _t
    c._last_run["recently_run"] = _t.monotonic()
    http.post = AsyncMock()
    trends = asyncio.run(c.collect())
    assert trends == []
    assert http.post.call_count == 0


def test_collect_handles_http_error_gracefully():
    c, http = _make_collector(
        {
            "token": "tok",
            "monthly_cap_usd": 0,
            "per_cycle_cap_usd": 0,
            "actors": [
                {
                    "actor_id": "fail_actor",
                    "source_platform": "tiktok",
                    "input": {},
                    "item_mapper": "tiktok_post",
                }
            ],
        }
    )
    err = MagicMock()
    err.status_code = 500
    err.text = "internal error"
    http.post = AsyncMock(return_value=err)
    c.http = http
    c.limiter.acquire = AsyncMock()
    trends = asyncio.run(c.collect())
    assert trends == []


def test_collect_handles_non_list_payload():
    c, http = _make_collector(
        {
            "token": "tok",
            "monthly_cap_usd": 0,
            "per_cycle_cap_usd": 0,
            "actors": [
                {
                    "actor_id": "weird_actor",
                    "source_platform": "tiktok",
                    "input": {},
                    "item_mapper": "tiktok_post",
                }
            ],
        }
    )
    http.post = AsyncMock(return_value=_http_response({"not": "a list"}))
    c.http = http
    c.limiter.acquire = AsyncMock()
    trends = asyncio.run(c.collect())
    assert trends == []


def test_collect_unwraps_items_payload():
    """Some actors return {items: [...]} — we handle that."""
    c, http = _make_collector(
        {
            "token": "tok",
            "monthly_cap_usd": 0,
            "per_cycle_cap_usd": 0,
            "actors": [
                {
                    "actor_id": "wrapped",
                    "source_platform": "instagram",
                    "input": {},
                    "item_mapper": "instagram_post",
                }
            ],
        }
    )
    http.post = AsyncMock(
        return_value=_http_response({"items": [_instagram_item()]})
    )
    c.http = http
    c.limiter.acquire = AsyncMock()
    trends = asyncio.run(c.collect())
    assert len(trends) == 1
    assert trends[0].metadata["source_platform"] == "instagram"


# ---------- registry auto-discovery ----------


def test_apify_collector_registered():
    from src.collectors.registry import CollectorRegistry

    r = CollectorRegistry()
    r.discover()
    assert "apify" in r.available()
    assert r.get("apify") is ApifyBridgeCollector

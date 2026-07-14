"""YouTube Data API v3 collector tests.

Sample payload is a literal copy of the real API shape.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors.platforms.youtube import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_REGIONS,
    YouTubeTrendingCollector,
)


SAMPLE_PAYLOAD = {
    "kind": "youtube#videoListResponse",
    "items": [
        {
            "id": "dQw4w9WgXcQ",
            "snippet": {
                "title": "Never Gonna Give You Up",
                "channelTitle": "Rick Astley",
                "categoryId": "10",
                "publishedAt": "2009-10-25T06:57:33Z",
                "thumbnails": {
                    "high": {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"},
                    "medium": {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"},
                },
            },
            "statistics": {
                "viewCount": "1500000000",
                "likeCount": "15000000",
            },
        },
        {
            "id": "abc12345678",
            "snippet": {
                "title": "New trending video",
                "channelTitle": "Channel X",
                "categoryId": "20",
                "publishedAt": "2026-07-13T00:00:00Z",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "50000",
                "likeCount": "1000",
            },
        },
        {
            "id": "noStats001",
            "snippet": {
                "title": "Live stream",
                "channelTitle": "Streamer",
                "categoryId": "24",
                "publishedAt": "2026-07-13T01:00:00Z",
            },
            "statistics": {},
        },
    ],
}


def _make_collector(api_key=None, **config):
    http = MagicMock()
    http.get = AsyncMock()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    limiter.apply_retry_after = MagicMock()
    cfg = {"api_key": api_key} if api_key else {}
    cfg.update(config)
    return (
        YouTubeTrendingCollector(
            http_client=http, rate_limiter=limiter, config=cfg
        ),
        http,
    )


def test_platform_constant():
    assert YouTubeTrendingCollector.platform == "youtube"


def test_default_regions():
    assert "US" in DEFAULT_REGIONS
    assert "GB" in DEFAULT_REGIONS


def test_default_max_results():
    assert DEFAULT_MAX_RESULTS == 25


def test_resolve_key_literal_takes_priority(monkeypatch):
    """A literal api_key in config overrides env var."""
    monkeypatch.setenv("YOUTUBE_API_KEY", "env-key")
    collector, _ = _make_collector(api_key="literal-key")
    assert collector._resolve_key() == "literal-key"


def test_resolve_key_from_env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "env-key-value")
    collector, _ = _make_collector()
    assert collector._resolve_key() == "env-key-value"


def test_resolve_key_custom_env_var(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_KEY", "custom-value")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    collector, _ = _make_collector(api_key_env="MY_CUSTOM_KEY")
    assert collector._resolve_key() == "custom-value"


def test_resolve_key_missing_returns_none(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    collector, _ = _make_collector()
    assert collector._resolve_key() is None


def test_parse_response_happy():
    collector, _ = _make_collector(api_key="k")
    trends = collector._parse_response(SAMPLE_PAYLOAD, region="US")
    assert len(trends) == 3

    rick = trends[0]
    assert rick.platform == "youtube"
    assert rick.trend_type == "video"
    assert rick.name == "Never Gonna Give You Up"
    assert rick.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert rick.score == 1500000000.0
    assert rick.metadata["channel"] == "Rick Astley"
    assert rick.metadata["category_id"] == "10"
    assert rick.metadata["view_count"] == 1500000000
    assert rick.metadata["like_count"] == 15000000
    assert "hqdefault" in rick.metadata["thumbnail"]


def test_parse_response_handles_no_stats():
    """Video with empty statistics still produces a Trend (views=0)."""
    collector, _ = _make_collector(api_key="k")
    trends = collector._parse_response(SAMPLE_PAYLOAD, region="US")
    live = next(t for t in trends if t.name == "Live stream")
    assert live.score == 0.0
    assert live.metadata["view_count"] == 0
    assert live.metadata["thumbnail"] == ""  # no thumbs at all


def test_parse_response_handles_invalid_viewcount():
    payload = {
        "items": [
            {
                "id": "bad123",
                "snippet": {"title": "weird stats", "channelTitle": "x"},
                "statistics": {"viewCount": "not-a-number"},
            }
        ]
    }
    collector, _ = _make_collector(api_key="k")
    trends = collector._parse_response(payload, region="US")
    assert len(trends) == 1
    assert trends[0].score == 0.0  # falls back to 0


def test_parse_response_skips_item_without_id():
    payload = {
        "items": [
            {"snippet": {"title": "no id"}},
            {"id": "valid01", "snippet": {"title": "valid"}},
        ]
    }
    collector, _ = _make_collector(api_key="k")
    trends = collector._parse_response(payload, region="US")
    assert len(trends) == 1
    assert trends[0].name == "valid"


def test_parse_response_skips_item_without_title():
    payload = {
        "items": [
            {"id": "notitle", "snippet": {"title": ""}},
            {"id": "valid01", "snippet": {"title": "valid"}},
        ]
    }
    collector, _ = _make_collector(api_key="k")
    trends = collector._parse_response(payload, region="US")
    assert len(trends) == 1


def test_parse_response_empty_items():
    collector, _ = _make_collector(api_key="k")
    trends = collector._parse_response({"items": []}, region="US")
    assert trends == []


def test_parse_response_missing_items_key():
    collector, _ = _make_collector(api_key="k")
    trends = collector._parse_response({}, region="US")
    assert trends == []


def test_thumbnail_fallback_chain():
    """If 'high' missing, falls back to 'medium' then 'default'."""
    payload = {
        "items": [
            {
                "id": "fb1",
                "snippet": {
                    "title": "x",
                    "channelTitle": "y",
                    "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/fb1/default.jpg"}},
                },
            }
        ]
    }
    collector, _ = _make_collector(api_key="k")
    trends = collector._parse_response(payload, region="US")
    assert trends[0].metadata["thumbnail"] == "https://i.ytimg.com/vi/fb1/default.jpg"


@pytest.mark.asyncio
async def test_collect_happy_path():
    collector, http = _make_collector(api_key="k", regions=["US", "GB"])
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = SAMPLE_PAYLOAD
    http.get.return_value = resp

    trends = await collector.collect()
    # 2 regions × 3 items = 6
    assert len(trends) == 6
    assert {t.metadata["region"] for t in trends} == {"US", "GB"}


@pytest.mark.asyncio
async def test_collect_skipped_without_api_key():
    """If no key is set, collector logs a warning and returns []."""
    collector, http = _make_collector()  # no api_key, no env
    # Ensure env var not set
    os.environ.pop("YOUTUBE_API_KEY", None)
    trends = await collector.collect()
    assert trends == []
    http.get.assert_not_called()


@pytest.mark.asyncio
async def test_collect_handles_fetch_failure():
    collector, http = _make_collector(api_key="k", regions=["US", "GB"])

    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        if call_count["n"] == 1:
            resp.status_code = 500
        else:
            resp.status_code = 200
            resp.json.return_value = SAMPLE_PAYLOAD
        return resp

    http.get.side_effect = fake_get
    trends = await collector.collect()
    assert len(trends) == 3
    assert trends[0].metadata["region"] == "GB"


@pytest.mark.asyncio
async def test_collect_sends_category_param_when_set():
    """videoCategoryId is included only if config sets it."""
    collector, http = _make_collector(api_key="k", regions=["US"], category_id="10")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = SAMPLE_PAYLOAD
    http.get.return_value = resp

    await collector.collect()
    # Inspect the params of the first http.get call
    call = http.get.call_args
    assert "params" in call.kwargs
    assert call.kwargs["params"]["videoCategoryId"] == "10"
    assert call.kwargs["params"]["regionCode"] == "US"


@pytest.mark.asyncio
async def test_collect_caps_max_results_at_50():
    """max_results is clamped to 50 (YouTube's hard cap)."""
    collector, http = _make_collector(api_key="k", max_results=200)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"items": []}
    http.get.return_value = resp

    await collector.collect()
    call = http.get.call_args
    assert call.kwargs["params"]["maxResults"] == 50

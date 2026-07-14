"""End-to-end smoke test: build a sample API response, run the collector,
verify the Trend output. No real network — uses unittest.mock.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors.platforms.x import XTrendsCollector


def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def test_x_extract_items_v2_shape():
    payload = {
        "data": [
            {"trend_name": "#AIart", "tweet_count": 12345, "rank": 1},
            {"trend_name": "Taylor Swift", "tweet_count": 9876, "rank": 2},
        ]
    }
    items = XTrendsCollector._extract_items(payload)
    assert len(items) == 2


def test_x_extract_items_legacy_shape():
    payload = [
        {"name": "#AIart", "tweet_volume": 12345},
    ]
    items = XTrendsCollector._extract_items(payload)
    assert len(items) == 1


def test_x_item_to_trend():
    item = {"trend_name": "aiart", "tweet_count": 5000, "rank": 5}
    t = XTrendsCollector._item_to_trend(item, woeid=1)
    assert t.platform == "x"
    assert t.name == "#aiart"
    assert t.score == 5000.0
    assert t.metadata["woeid"] == 1
    assert t.metadata["rank"] == 5


def test_x_item_with_phrase_no_hash():
    item = {"trend_name": "World Cup", "tweet_count": 100, "rank": 1}
    t = XTrendsCollector._item_to_trend(item, woeid=1)
    # Phrases with spaces don't get a # prefix
    assert t.name == "World Cup"


def test_x_resolve_token_from_env(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-token-abc")
    http = MagicMock()
    limiter = MagicMock()
    c = XTrendsCollector(http_client=http, rate_limiter=limiter, config={})
    assert c._resolve_token() == "test-token-abc"


def test_x_resolve_token_missing():
    http = MagicMock()
    limiter = MagicMock()
    c = XTrendsCollector(http_client=http, rate_limiter=limiter, config={})
    assert c._resolve_token() is None


def test_x_collect_skips_when_no_token(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    http = MagicMock()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    c = XTrendsCollector(http_client=http, rate_limiter=limiter, config={})
    trends = asyncio.run(c.collect())
    assert trends == []
    # No HTTP calls should have been made
    http.get.assert_not_called()

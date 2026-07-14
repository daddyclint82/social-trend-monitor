"""Tests for the TikTok oEmbed collector (revised per ADR-0002).

TikTok's discovery API is anti-bot gated. Our v1 collector uses oEmbed
and user-supplied hashtag lists. These tests cover the mapping logic
without hitting the network.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors.platforms.tiktok import TikTokOEmbedCollector
from src.normalizer.schema import PLATFORMS


def _make_collector(config: dict) -> tuple[TikTokOEmbedCollector, MagicMock]:
    http = MagicMock()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    c = TikTokOEmbedCollector(http_client=http, rate_limiter=limiter, config=config)
    return c, http


def test_hashtag_to_trend_strips_hash():
    t = TikTokOEmbedCollector._hashtag_to_trend("#aiart")
    assert t.name == "#aiart"
    assert t.platform == "tiktok_oembed"
    assert t.score == 0.0
    assert t.metadata["source"] == "user-supplied"
    assert t.metadata["discoverable"] is False


def test_hashtag_to_trend_adds_hash_if_missing():
    t = TikTokOEmbedCollector._hashtag_to_trend("booktok")
    assert t.name == "#booktok"


def test_oembed_to_trend_extracts_author():
    payload = {
        "author_name": "cool_creator",
        "title": "My video",
        "url": "https://www.tiktok.com/@cool_creator/video/123",
        "thumbnail_url": "https://example.com/thumb.jpg",
        "html": "<iframe ...></iframe>",
    }
    t = TikTokOEmbedCollector._oembed_to_trend(payload, payload["url"])
    assert t.platform == "tiktok_oembed"
    assert t.name == "@cool_creator"
    assert t.metadata["source"] == "oembed"
    assert t.metadata["title"] == "My video"


def test_collect_with_empty_config():
    c, _http = _make_collector({})
    trends = asyncio.run(c.collect())
    assert trends == []


def test_collect_with_hashtags_only():
    c, _http = _make_collector({"hashtags": ["aiart", "fyp"]})
    trends = asyncio.run(c.collect())
    assert len(trends) == 2
    assert trends[0].name == "#aiart"
    assert trends[1].name == "#fyp"


def test_collect_with_creator_urls_handles_oembed_failure():
    c, http = _make_collector({"creator_urls": ["https://www.tiktok.com/@test"]})
    # Mock get_json to return None (oembed failed)
    c.get_json = AsyncMock(return_value=None)
    trends = asyncio.run(c.collect())
    assert len(trends) == 1
    assert trends[0].trend_type == "creator"
    assert trends[0].metadata["oembed"] == "failed"


def test_collect_with_creator_urls_handles_oembed_success():
    c, _http = _make_collector({"creator_urls": ["https://www.tiktok.com/@user"]})
    c.get_json = AsyncMock(
        return_value={
            "author_name": "user",
            "title": "Cool",
            "url": "https://www.tiktok.com/@user/video/1",
            "html": "<iframe></iframe>",
        }
    )
    trends = asyncio.run(c.collect())
    assert len(trends) == 1
    assert trends[0].name == "@user"
    assert trends[0].metadata["source"] == "oembed"

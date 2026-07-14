"""Tests for the unified Trend schema and cross-platform key generation."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.normalizer.schema import (
    PLATFORMS,
    Trend,
    make_cross_platform_key,
    make_trend,
    make_trend_id,
)


def test_make_trend_id_stable():
    # Use a platform key that survives the namespace split
    assert make_trend_id("tiktok_oembed", "abc123") == make_trend_id("tiktok_oembed", "abc123")
    assert make_trend_id("tiktok_oembed", "abc123") != make_trend_id("x", "abc123")


def test_cross_platform_key_normalizes():
    assert make_cross_platform_key("x", "#AIart") == make_cross_platform_key("x", "aiart")
    assert make_cross_platform_key("tiktok_oembed", "  AIart  ") == "tiktok_oembed::aiart"
    assert make_cross_platform_key("x", "@elonmusk") == "x::elonmusk"


def test_make_trend_basic():
    t = make_trend(
        platform="tiktok_oembed",
        name="#aiart",
        trend_type="hashtag",
        platform_native_id="42",
        url="https://example.com",
        score=1234.0,
    )
    assert t.platform == "tiktok_oembed"
    # cross_platform_key normalizes away the leading # for joining
    assert t.cross_platform_key == "tiktok_oembed::aiart"
    assert t.id.startswith("tiktok_oembed:")
    assert isinstance(t.first_seen, datetime)


def test_trend_validates_platform():
    with pytest.raises(ValueError):
        make_trend(
            platform="myspace",
            name="test",
            trend_type="hashtag",
            platform_native_id="1",
            url=None,
            score=0,
        )


def test_trend_validates_type():
    with pytest.raises(ValueError):
        make_trend(
            platform="tiktok_oembed",
            name="test",
            trend_type="magic",
            platform_native_id="1",
            url=None,
            score=0,
        )


def test_trend_roundtrip():
    t = make_trend(
        platform="x",
        name="#AIart",
        trend_type="topic",
        platform_native_id="1:#AIart",
        url="https://x.com/search?q=AIart",
        score=12345.0,
        metadata={"rank": 1, "woeid": 1},
    )
    d = t.to_dict()
    t2 = Trend.from_dict(d)
    assert t2.platform == t.platform
    assert t2.name == t.name
    assert t2.score == t.score
    assert t2.metadata == t.metadata

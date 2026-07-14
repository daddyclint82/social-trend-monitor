"""TikTok Discover collector tests (antiops/tiktok-trending-data JSON shape).

Sample JSON is a literal copy of the real shape, validated 2026-07-13.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors.platforms.tiktok_discover import (
    DEFAULT_REGIONS,
    TikTokDiscoverCollector,
)


SAMPLE_PAYLOAD = {
    "statusCode": 0,
    "errMsg": "",
    "body": {
        "discoverList": [
            {
                "type": 3,
                "title": "tiktokgostar",
                "link": "https://www.tiktok.com/tag/tiktokgostar",
                "isInternalLink": False,
            },
            {
                "type": 3,
                "title": "tiktokshopfanaticsfest",
                "link": "https://www.tiktok.com/tag/tiktokshopfanaticsfest",
                "isInternalLink": False,
            },
            {
                "type": 4,
                "title": "Forever (From \"Euphoria: Season 1\" Soundtrack) - Labrinth",
                "link": "https://www.tiktok.com/music/Forever-From-Euphoria-Season-1-Soundtrack-6740248251825391617",
                "isInternalLink": False,
            },
            {
                "type": 4,
                "title": "Quinceañera - Banda Machos",
                "link": "https://www.tiktok.com/music/Quinceanera-5000000001008467945",
                "isInternalLink": False,
            },
            {
                "type": 99,  # unknown type
                "title": "weird",
                "link": "https://www.tiktok.com/something",
                "isInternalLink": False,
            },
        ]
    },
}


def _make_collector(regions=None):
    http = MagicMock()
    http.get = AsyncMock()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    limiter.apply_retry_after = MagicMock()
    return (
        TikTokDiscoverCollector(
            http_client=http,
            rate_limiter=limiter,
            config={"regions": regions or ["us"]},
        ),
        http,
    )


def test_platform_constant():
    """Both tiktok collectors share the platform key — that's the point."""
    assert TikTokDiscoverCollector.platform == "tiktok"


def test_default_regions():
    assert "us" in DEFAULT_REGIONS
    assert len(DEFAULT_REGIONS) >= 1


def test_item_to_trend_hashtag():
    item = {
        "type": 3,
        "title": "aiart",
        "link": "https://www.tiktok.com/tag/aiart",
        "isInternalLink": False,
    }
    t = TikTokDiscoverCollector._item_to_trend(item, region="us")
    assert t is not None
    assert t.platform == "tiktok"
    assert t.trend_type == "hashtag"
    assert t.name == "#aiart"
    assert t.url == "https://www.tiktok.com/tag/aiart"
    assert t.metadata["region"] == "us"
    assert t.metadata["source"] == "antiops_github"
    assert t.metadata["raw_type"] == 3


def test_item_to_trend_hashtag_preserves_existing_hash():
    """If the title already starts with #, we don't double it."""
    item = {
        "type": 3,
        "title": "#fyp",
        "link": "https://www.tiktok.com/tag/fyp",
        "isInternalLink": False,
    }
    t = TikTokDiscoverCollector._item_to_trend(item, region="us")
    assert t.name == "#fyp"


def test_item_to_trend_sound():
    item = {
        "type": 4,
        "title": "Quinceañera - Banda Machos",
        "link": "https://www.tiktok.com/music/Quinceanera-5000000001008467945",
        "isInternalLink": False,
    }
    t = TikTokDiscoverCollector._item_to_trend(item, region="us")
    assert t is not None
    assert t.trend_type == "sound"
    assert t.name == "Quinceañera - Banda Machos"
    assert "Quinceanera" in t.url  # accented chars normalized in URL


def test_item_to_trend_unknown_type_skipped():
    item = {"type": 99, "title": "x", "link": "https://tiktok.com/x"}
    t = TikTokDiscoverCollector._item_to_trend(item, region="us")
    assert t is None


def test_item_to_trend_missing_title_skipped():
    item = {"type": 3, "title": "", "link": "https://tiktok.com/x"}
    t = TikTokDiscoverCollector._item_to_trend(item, region="us")
    assert t is None


def test_item_to_trend_missing_link_skipped():
    item = {"type": 3, "title": "x", "link": ""}
    t = TikTokDiscoverCollector._item_to_trend(item, region="us")
    assert t is None


def test_parse_payload_happy():
    collector, _ = _make_collector()
    trends = collector._parse_payload(SAMPLE_PAYLOAD, region="us")
    # 4 valid items, 1 unknown type filtered
    assert len(trends) == 4
    types = [t.trend_type for t in trends]
    assert types.count("hashtag") == 2
    assert types.count("sound") == 2


def test_parse_payload_bad_status_code():
    payload = {"statusCode": 1, "errMsg": "rate limited", "body": {}}
    collector, _ = _make_collector()
    trends = collector._parse_payload(payload, region="us")
    assert trends == []


def test_parse_payload_missing_body():
    payload = {"statusCode": 0}  # no body
    collector, _ = _make_collector()
    trends = collector._parse_payload(payload, region="us")
    assert trends == []


def test_parse_payload_empty_discover_list():
    payload = {"statusCode": 0, "body": {"discoverList": []}}
    collector, _ = _make_collector()
    trends = collector._parse_payload(payload, region="us")
    assert trends == []


def test_native_id_region_prefix():
    """Same link in two regions must produce different ids."""
    item = {"type": 3, "title": "fyp", "link": "https://tiktok.com/tag/fyp"}
    t_us = TikTokDiscoverCollector._item_to_trend(item, region="us")
    t_www = TikTokDiscoverCollector._item_to_trend(item, region="www")
    assert t_us.id != t_www.id


@pytest.mark.asyncio
async def test_collect_happy_path():
    collector, http = _make_collector(regions=["us", "www"])

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = SAMPLE_PAYLOAD
        return resp

    http.get.side_effect = fake_get
    trends = await collector.collect()
    # 2 regions × 4 valid items = 8
    assert len(trends) == 8
    regions_seen = {t.metadata["region"] for t in trends}
    assert regions_seen == {"us", "www"}


@pytest.mark.asyncio
async def test_collect_handles_fetch_failure():
    """A failed fetch for one region doesn't kill the cycle."""
    collector, http = _make_collector(regions=["us", "www"])

    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        if call_count["n"] == 1:
            resp.status_code = 404
            resp.json.side_effect = ValueError("404 not JSON")
        else:
            resp.status_code = 200
            resp.json.return_value = SAMPLE_PAYLOAD
        return resp

    http.get.side_effect = fake_get
    trends = await collector.collect()
    assert len(trends) == 4
    assert trends[0].metadata["region"] == "www"


@pytest.mark.asyncio
async def test_collect_handles_bad_payload():
    """A payload that fails _parse_payload (e.g. missing body) is skipped."""
    collector, http = _make_collector(regions=["us", "www"])

    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        if call_count["n"] == 1:
            resp.status_code = 200
            resp.json.return_value = {"statusCode": 1, "errMsg": "broken"}
        else:
            resp.status_code = 200
            resp.json.return_value = SAMPLE_PAYLOAD
        return resp

    http.get.side_effect = fake_get
    trends = await collector.collect()
    assert len(trends) == 4

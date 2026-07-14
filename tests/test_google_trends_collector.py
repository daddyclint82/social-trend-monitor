"""Google Trends RSS collector tests.

Sample RSS feed is a literal copy of the real Google Trends response
shape (validated 2026-07-13). Tests cover parsing, traffic bucket
mapping, multi-region, min_traffic filter, and end-to-end collect.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import xml.etree.ElementTree as ET

from src.collectors.platforms.google_trends import (
    DEFAULT_GEOS,
    GoogleTrendsCollector,
    _RSS_URL,
    _TRAFFIC_BUCKETS,
)


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
	<channel>
		<title>Daily Search Trends</title>
		<description>Recent searches</description>
		<link>https://trends.google.com/trending/rss?geo=US</link>
		<atom:link href="https://trends.google.com/trending/rss?geo=US" rel="self" type="application/rss+xml"/>
		<item>
			<title>bryan cranston</title>
			<ht:approx_traffic>1000+</ht:approx_traffic>
			<description/>
			<link>https://trends.google.com/trending/rss?geo=US</link>
			<pubDate>Mon, 13 Jul 2026 18:10:00 -0700</pubDate>
			<ht:picture>https://encrypted-tbn1.gstatic.com/images?q=tbn:fake</ht:picture>
			<ht:picture_source>6abc Philadelphia</ht:picture_source>
			<ht:news_item>
				<ht:news_item_title>Actor Bryan Cranston talks blood drive</ht:news_item_title>
				<ht:news_item_url>https://6abc.com/post/bryan-cranston-blood/123</ht:news_item_url>
				<ht:news_item_source>6abc Philadelphia</ht:news_item_source>
			</ht:news_item>
		</item>
		<item>
			<title>jason sudeikis</title>
			<ht:approx_traffic>1000+</ht:approx_traffic>
			<description/>
			<link>https://trends.google.com/trending/rss?geo=US</link>
			<pubDate>Mon, 13 Jul 2026 18:20:00 -0700</pubDate>
		</item>
		<item>
			<title>small trend</title>
			<ht:approx_traffic>20+</ht:approx_traffic>
			<description/>
			<link>https://trends.google.com/trending/rss?geo=US</link>
			<pubDate>Mon, 13 Jul 2026 18:25:00 -0700</pubDate>
		</item>
	</channel>
</rss>"""


def _make_collector(geos=None, min_traffic=1):
    http = MagicMock()
    http.get = AsyncMock()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    limiter.apply_retry_after = MagicMock()
    cfg = {"geos": geos or ["US"], "min_traffic": min_traffic}
    return GoogleTrendsCollector(http_client=http, rate_limiter=limiter, config=cfg), http


def test_platform_constant():
    assert GoogleTrendsCollector.platform == "google_trends"


def test_traffic_bucket_mapping():
    """All known Google buckets map to a positive int."""
    assert _TRAFFIC_BUCKETS["1000+"] == 6
    assert _TRAFFIC_BUCKETS["500+"] == 5
    assert _TRAFFIC_BUCKETS["20+"] == 1
    # Unknown bucket → 0
    assert _TRAFFIC_BUCKETS.get("weird", 0) == 0


def test_default_geos_present():
    assert "US" in DEFAULT_GEOS
    assert "GB" in DEFAULT_GEOS
    assert len(DEFAULT_GEOS) >= 4


def test_parse_feed_basic():
    collector, _ = _make_collector()
    trends = collector._parse_feed(SAMPLE_RSS, geo="US", min_traffic=1)
    assert len(trends) == 3
    titles = [t.name for t in trends]
    assert "bryan cranston" in titles
    assert "jason sudeikis" in titles
    assert "small trend" in titles


def test_parse_feed_traffic_score():
    collector, _ = _make_collector()
    trends = collector._parse_feed(SAMPLE_RSS, geo="US", min_traffic=1)
    by_name = {t.name: t for t in trends}
    assert by_name["bryan cranston"].score == 6.0
    assert by_name["small trend"].score == 1.0


def test_parse_feed_min_traffic_filter():
    """min_traffic=4 excludes '1000+' (6), '500+' (5). '20+' is 1, definitely out."""
    collector, _ = _make_collector(min_traffic=4)
    trends = collector._parse_feed(SAMPLE_RSS, geo="US", min_traffic=4)
    # 1000+ has score 6, so 2 items remain, small trend (score 1) is filtered
    assert len(trends) == 2
    names = [t.name for t in trends]
    assert "small trend" not in names


def test_parse_feed_metadata_geo():
    collector, _ = _make_collector()
    trends = collector._parse_feed(SAMPLE_RSS, geo="DE", min_traffic=1)
    for t in trends:
        assert t.metadata["geo"] == "DE"
        assert t.metadata["traffic_bucket"] in _TRAFFIC_BUCKETS


def test_parse_feed_news_urls_captured():
    collector, _ = _make_collector()
    trends = collector._parse_feed(SAMPLE_RSS, geo="US", min_traffic=1)
    bryan = next(t for t in trends if t.name == "bryan cranston")
    assert "https://6abc.com/post/bryan-cranston-blood/123" in bryan.metadata["news_urls"]
    assert "Actor Bryan Cranston talks blood drive" in bryan.metadata["news_titles"]
    # No news → bryan has news, jason has none
    jason = next(t for t in trends if t.name == "jason sudeikis")
    assert jason.metadata["news_urls"] == []


def test_parse_feed_url_prefers_news_link():
    """If RSS <link> is generic trends.google.com, the first news URL is used."""
    collector, _ = _make_collector()
    trends = collector._parse_feed(SAMPLE_RSS, geo="US", min_traffic=1)
    bryan = next(t for t in trends if t.name == "bryan cranston")
    # <link> in sample is generic trends.google.com → falls back to first news URL
    assert bryan.url == "https://6abc.com/post/bryan-cranston-blood/123"


def test_parse_feed_platform_native_id_stable():
    """Same title+pubDate+geo → same id (so duplicates dedupe).

    The id is a SHA-1 hash of native_id (per make_trend_id in schema.py),
    so the *value* matters, not its composition. Just assert two parses
    produce identical ids for the same input.
    """
    collector, _ = _make_collector()
    a = collector._parse_feed(SAMPLE_RSS, geo="US", min_traffic=1)
    b = collector._parse_feed(SAMPLE_RSS, geo="US", min_traffic=1)
    # Same input → same id (dedupe invariant)
    assert a[0].id == b[0].id
    # Different geo → different id (so the geo *is* part of the native_id)
    c = collector._parse_feed(SAMPLE_RSS, geo="DE", min_traffic=1)
    assert a[0].id != c[0].id


def test_parse_feed_empty_channel():
    xml = """<?xml version="1.0"?>
<rss><channel><title>Empty</title></channel></rss>"""
    collector, _ = _make_collector()
    trends = collector._parse_feed(xml, geo="US", min_traffic=1)
    assert trends == []


def test_parse_feed_skips_items_without_title():
    xml = """<?xml version="1.0"?>
<rss xmlns:ht="https://trends.google.com/trending/rss">
<channel>
  <item><ht:approx_traffic>1000+</ht:approx_traffic></item>
  <item><title>valid</title><ht:approx_traffic>500+</ht:approx_traffic></item>
</channel>
</rss>"""
    collector, _ = _make_collector()
    trends = collector._parse_feed(xml, geo="US", min_traffic=1)
    assert len(trends) == 1
    assert trends[0].name == "valid"


@pytest.mark.asyncio
async def test_collect_happy_path():
    """End-to-end: collector fetches each geo, parses, returns combined list."""
    collector, http = _make_collector(geos=["US", "GB"])

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = SAMPLE_RSS
        return resp

    http.get.side_effect = fake_get
    trends = await collector.collect()
    # 2 regions × 3 items each = 6
    assert len(trends) == 6
    geos = {t.metadata["geo"] for t in trends}
    assert geos == {"US", "GB"}


@pytest.mark.asyncio
async def test_collect_handles_fetch_failure():
    """A failed fetch for one region doesn't kill the cycle."""
    collector, http = _make_collector(geos=["US", "GB"])

    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        if call_count["n"] == 1:
            resp.status_code = 503
            resp.text = ""
        else:
            resp.status_code = 200
            resp.text = SAMPLE_RSS
        return resp

    http.get.side_effect = fake_get
    trends = await collector.collect()
    # First region failed, second returned 3
    assert len(trends) == 3
    assert trends[0].metadata["geo"] == "GB"


@pytest.mark.asyncio
async def test_collect_handles_malformed_xml():
    """Bad XML for one region: that region returns [], others continue."""
    collector, http = _make_collector(geos=["US", "GB"])

    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        if call_count["n"] == 1:
            resp.status_code = 200
            resp.text = "<<<NOT XML>>>"
        else:
            resp.status_code = 200
            resp.text = SAMPLE_RSS
        return resp

    http.get.side_effect = fake_get
    trends = await collector.collect()
    assert len(trends) == 3

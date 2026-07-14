"""Google Trends RSS collector (free, no auth, no rate limit).

Source: https://trends.google.com/trending/rss?geo={COUNTRY}

Google Trends publishes a public RSS feed of trending searches per region.
10 trending searches per region. Each item carries:
  - title: the search term
  - ht:approx_traffic: traffic bucket ("1000+", "500+", "200+", "100+", "50+", "20+")
  - pubDate: when the trend started
  - ht:news_item: list of related news articles with title, url, source

This is the single most valuable free trend source we have. It is
1. the most reliable (Google's own data, no anti-bot)
2. the most semantically rich (real human search queries, not hashtags)
3. cross-platform by nature (people search for things that are also on TikTok/X/Reddit)

Config keys:
    geos: list[str]          — country codes to fetch. Default: US, GB, DE, JP, IN, BR.
    min_traffic: int        — minimum approx_traffic to include (1..10).
                              Buckets are 1000+, 500+, 200+, 100+, 50+, 20+.
                              We map to numbers 6, 5, 4, 3, 2, 1.
                              Default: 1 (include everything).
    timeout_s: float        — request timeout (default 15.0)
"""
from __future__ import annotations

import structlog
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

_RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"

# Default geos: worldwide-ish coverage with no overlap. Same regions as
# the X WOEIDs in the x.py collector for parallel coverage.
DEFAULT_GEOS: list[str] = ["US", "GB", "DE", "JP", "IN", "BR"]

# Map Google's "1000+" / "500+" / etc. traffic buckets to numeric scores.
# These are the only buckets Google publishes; the "+" implies "at least".
_TRAFFIC_BUCKETS: dict[str, int] = {
    "1000+": 6,
    "500+":  5,
    "200+":  4,
    "100+":  3,
    "50+":   2,
    "20+":   1,
}

# XML namespace constants for the RSS feed
_NS_H = "https://trends.google.com/trending/rss"
_NS_ATOM = "http://www.w3.org/2005/Atom"


class GoogleTrendsCollector(BaseCollector):
    """Fetch trending searches from Google Trends RSS for configured regions.

    Public, free, no auth. The rate-limiter is largely a no-op here
    (Google doesn't gate this endpoint) but we still acquire to be polite.
    """

    platform = "google_trends"
    timeout_s = 15.0

    async def collect(self) -> list[Trend]:
        geos: list[str] = self.config.get("geos", DEFAULT_GEOS)
        min_traffic: int = int(self.config.get("min_traffic", 1))
        trends: list[Trend] = []
        for geo in geos:
            url = _RSS_URL.format(geo=geo)
            xml_text = await self.get_text(url)
            if not xml_text:
                logger.warning("google_trends.fetch_failed", geo=geo)
                continue
            try:
                region_trends = self._parse_feed(xml_text, geo=geo, min_traffic=min_traffic)
            except ET.ParseError as e:
                logger.warning(
                    "google_trends.parse_error", geo=geo, error=str(e)
                )
                continue
            trends.extend(region_trends)
            logger.info(
                "google_trends.collected", geo=geo, items=len(region_trends)
            )
        return trends

    def _parse_feed(
        self, xml_text: str, *, geo: str, min_traffic: int
    ) -> list[Trend]:
        """Parse a single region's RSS feed into Trends.

        Strategy:
        - Find all <item> elements
        - For each, extract title, pubDate, ht:approx_traffic, ht:news_item list
        - Build one Trend per item
        - news_item URLs go into metadata["news_urls"] for the read API
        - pubDate becomes the platform_native_id (so duplicates across cycles dedupe)
        """
        root = ET.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return []
        trends: list[Trend] = []
        for item in channel.findall("item"):
            title_el = item.find("title")
            if title_el is None or not (title_el.text or "").strip():
                continue
            title = title_el.text.strip()

            traffic_el = item.find(f"{{{_NS_H}}}approx_traffic")
            traffic_text = (traffic_el.text or "").strip() if traffic_el is not None else ""
            traffic_score = _TRAFFIC_BUCKETS.get(traffic_text, 0)
            if traffic_score < min_traffic:
                continue

            pub_el = item.find("pubDate")
            pub_text = (pub_el.text or "").strip() if pub_el is not None else ""
            # Native ID: title + pubDate + geo → unique per item per cycle
            native_id = f"{geo}:{title}:{pub_text}"

            # News items: collect first 3 URLs into metadata
            news_urls: list[str] = []
            news_titles: list[str] = []
            for news in item.findall(f"{{{_NS_H}}}news_item"):
                url_el = news.find(f"{{{_NS_H}}}news_item_url")
                title_news = news.find(f"{{{_NS_H}}}news_item_title")
                if url_el is not None and url_el.text:
                    news_urls.append(url_el.text.strip())
                if title_news is not None and title_news.text:
                    news_titles.append(title_news.text.strip())

            link_el = item.find("link")
            url = (link_el.text or "").strip() if link_el is not None else None

            # We use the first news URL as the canonical link if RSS link is generic
            if (not url or "trends.google.com" in url) and news_urls:
                url = news_urls[0]

            trends.append(
                make_trend(
                    platform="google_trends",
                    name=title,
                    trend_type="search",
                    platform_native_id=native_id,
                    url=url,
                    score=float(traffic_score),
                    metadata={
                        "geo": geo,
                        "traffic_bucket": traffic_text,
                        "pub_date": pub_text,
                        "news_urls": news_urls[:5],
                        "news_titles": news_titles[:5],
                    },
                )
            )
        return trends

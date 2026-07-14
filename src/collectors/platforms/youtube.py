"""YouTube Data API v3 — Trending videos collector.

Source: https://www.googleapis.com/youtube/v3/videos?chart=mostPopular

This is NOT a scrape of youtube.com/feed/trending. YouTube's HTML
trending page is fully JS-rendered (we tried). The Data API v3 is the
supported, documented path and it has a generous free tier.

**Free tier reality (verified 2026-07-13):**
- 10,000 quota units per day per project
- `videos.list?chart=mostPopular` costs **1 unit per call**
- = 10,000 daily calls. We use 1-2 per cycle → free for ~5,000 cycles/day
- Reset at midnight Pacific Time
- Requires a Google Cloud project + YouTube Data API v3 enabled + API key
- **Free, no credit card required** for the basic key

**Why we don't scrape youtube.com directly:**
- Page is hydrated client-side; raw HTML returns "try searching to get started"
- Bypassing requires Playwright/headless browser (heavy, breaks our
  single-async-event-loop architecture)
- Violates YouTube ToS for automated scraping without API

**Setup (one-time):**
1. https://console.cloud.google.com → create project
2. Enable "YouTube Data API v3"
3. Credentials → Create API key
4. Add `YOUTUBE_API_KEY=AIza…` to .env

Config keys:
    api_key_env: str        — env var name (default YOUTUBE_API_KEY)
    api_key: str            — literal key (testing only)
    regions: list[str]      — ISO country codes (default: US, GB, DE, JP, IN, BR)
    max_results: int        — per region, max 50 (default 25)
    category_id: str        — videoCategoryId filter, default empty (all)
                              Common: 10=Music, 20=Gaming, 24=Entertainment
    timeout_s: float        — default 15
"""
from __future__ import annotations

import os
import structlog

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

_API_URL = "https://www.googleapis.com/youtube/v3/videos"
DEFAULT_REGIONS: list[str] = ["US", "GB", "DE", "JP", "IN", "BR"]
DEFAULT_MAX_RESULTS = 25  # max 50 allowed by API


class YouTubeTrendingCollector(BaseCollector):
    """Pull trending videos from YouTube Data API v3.

    Free tier: 10k units/day, 1 unit per call. Default config uses
    6 regions × 1 call = 6 units per cycle. Polling every 30 minutes
    = 288 units/day. Well under the 10k cap.

    Platform: "youtube". Trend type: "video".
    """

    platform = "youtube"
    timeout_s = 15.0

    async def collect(self) -> list[Trend]:
        api_key = self._resolve_key()
        if not api_key:
            logger.warning("youtube.api_key_missing")
            return []
        regions: list[str] = self.config.get("regions", DEFAULT_REGIONS)
        max_results: int = int(self.config.get("max_results", DEFAULT_MAX_RESULTS))
        category_id: str = str(self.config.get("category_id", ""))

        trends: list[Trend] = []
        for region in regions:
            params: dict = {
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": region,
                "maxResults": min(max_results, 50),
                "key": api_key,
            }
            if category_id:
                params["videoCategoryId"] = category_id

            payload = await self.get_json(_API_URL, params=params)
            if payload is None:
                logger.warning("youtube.fetch_failed", region=region)
                continue
            try:
                region_trends = self._parse_response(payload, region=region)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(
                    "youtube.parse_error", region=region, error=str(e)
                )
                continue
            trends.extend(region_trends)
            logger.info(
                "youtube.collected", region=region, items=len(region_trends)
            )
        return trends

    def _resolve_key(self) -> str | None:
        env_name: str = self.config.get("api_key_env", "YOUTUBE_API_KEY")
        literal: str | None = self.config.get("api_key")
        if literal:
            return literal
        val = os.environ.get(env_name)
        return val if val else None

    @staticmethod
    def _parse_response(payload: dict, *, region: str) -> list[Trend]:
        """Map YouTube API response shape → list[Trend].

        Shape:
            {
              "items": [
                {
                  "id": "dQw4w9WgXcQ",
                  "snippet": {
                    "title": "...",
                    "channelTitle": "...",
                    "categoryId": "10",
                    "publishedAt": "2026-07-12T...",
                    "thumbnails": {"high": {"url": "..."}}
                  },
                  "statistics": {
                    "viewCount": "1234567",
                    "likeCount": "12345"
                  }
                }
              ]
            }
        """
        items = payload.get("items") or []
        trends: list[Trend] = []
        for item in items:
            video_id = item.get("id")
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            if not video_id or not snippet:
                continue

            title = (snippet.get("title") or "").strip()
            if not title:
                continue
            channel = (snippet.get("channelTitle") or "").strip()
            views_raw = stats.get("viewCount", "0")
            try:
                views = int(views_raw)
            except (ValueError, TypeError):
                views = 0
            try:
                likes = int(stats.get("likeCount", "0"))
            except (ValueError, TypeError):
                likes = 0

            url = f"https://www.youtube.com/watch?v={video_id}"
            thumbnail = ""
            thumbs = snippet.get("thumbnails") or {}
            for quality in ("high", "medium", "default"):
                if quality in thumbs and thumbs[quality].get("url"):
                    thumbnail = thumbs[quality]["url"]
                    break

            trends.append(
                make_trend(
                    platform="youtube",
                    name=title,
                    trend_type="video",
                    platform_native_id=video_id,
                    url=url,
                    score=float(views),
                    metadata={
                        "region": region,
                        "channel": channel,
                        "category_id": snippet.get("categoryId", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "thumbnail": thumbnail,
                        "view_count": views,
                        "like_count": likes,
                    },
                )
            )
        return trends

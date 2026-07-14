"""TikTok oEmbed collector (Tier 1 — public post metadata only).

Strategy (revised 2026-07-13, see ADR-0002):
TikTok's internal JSON APIs (Creative Center, etc.) are gated by Pumbaa
anti-bot. Bypassing that violates our ethics posture (ADR-0004).

For v1 we ship a *user-supplied* collector: the user provides a list of
hashtag names or creator URLs, and we hit the **public TikTok oEmbed
endpoint** (`https://www.tiktok.com/oembed?url=...`) which is documented,
supported, and requires no anti-bot bypass.

This is intentionally limited: we track *given* hashtags, we don't
discover *new* trends. The v2 path is the official TikTok Research API
(apply for access).

Config keys:
    hashtags: list[str]         — e.g. ["aiart", "booktok", "fyp"]
    creator_urls: list[str]     — e.g. ["https://www.tiktok.com/@user"]
    include_hashtag_search: bool — also fetch a public hashtag landing
                                   page for each hashtag (low yield,
                                   disabled by default — these pages are
                                   JS-rendered and give us little)
"""
from __future__ import annotations

import structlog

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

_OEMBED_URL = "https://www.tiktok.com/oembed"


class TikTokOEmbedCollector(BaseCollector):
    """Fetch public metadata for user-supplied TikTok hashtags / creators.

    Honest limits:
    - We cannot discover *new* trending hashtags this way.
    - Score is 0 (oEmbed doesn't expose trending data).
    - The value: same Trend schema lets us join user-curated TikTok
      watchlists with trends from other platforms that *do* have discovery.
    """

    platform = "tiktok"
    timeout_s = 10.0

    async def collect(self) -> list[Trend]:
        trends: list[Trend] = []
        for hashtag in self.config.get("hashtags", []):
            trends.append(self._hashtag_to_trend(hashtag))

        for url in self.config.get("creator_urls", []):
            payload = await self._fetch_oembed(url)
            if payload is not None:
                trends.append(self._oembed_to_trend(payload, url))
            else:
                # Even on failure, record the intent
                trends.append(
                    make_trend(
                        platform="tiktok",
                        name=url.rstrip("/").split("/")[-1],
                        trend_type="creator",
                        platform_native_id=url,
                        url=url,
                        score=0.0,
                        metadata={"oembed": "failed"},
                    )
                )

        logger.info(
            "tiktok.oembed.collected",
            hashtags=len(self.config.get("hashtags", [])),
            creators=len(self.config.get("creator_urls", [])),
        )
        return trends

    async def _fetch_oembed(self, url: str) -> dict | None:
        """Hit TikTok's public oEmbed endpoint."""
        return await self.get_json(
            _OEMBED_URL,
            params={"url": url},
        )

    @staticmethod
    def _hashtag_to_trend(hashtag: str) -> Trend:
        # Hashtags: we don't have a public endpoint to get post count
        # for a hashtag (that requires login or the gated search API).
        # We record the hashtag with a zero score and let the user fill
        # in trending metrics manually, or use the v2 Research API.
        clean = hashtag.lstrip("#")
        url = f"https://www.tiktok.com/tag/{clean}"
        return make_trend(
            platform="tiktok",
            name=f"#{clean}",
            trend_type="hashtag",
            platform_native_id=clean,
            url=url,
            score=0.0,
            metadata={"source": "user-supplied", "discoverable": False},
        )

    @staticmethod
    def _oembed_to_trend(payload: dict, original_url: str) -> Trend:
        author = payload.get("author_name", "unknown")
        title = payload.get("title", "")
        url = payload.get("url") or original_url
        return make_trend(
            platform="tiktok",
            name=f"@{author}",
            trend_type="creator",
            platform_native_id=url,
            url=url,
            score=0.0,
            metadata={
                "title": title,
                "thumbnail_url": payload.get("thumbnail_url"),
                "author_url": payload.get("author_url"),
                "html": payload.get("html"),
                "source": "oembed",
            },
        )

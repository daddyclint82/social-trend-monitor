"""Instagram oEmbed collector (Tier 1 — public post metadata only).

Instagram's anti-scraping is the most aggressive of the four platforms.
This collector only fetches public post metadata via the **official
oEmbed endpoint** for URLs the user supplies. It does NOT attempt to
discover what's trending on Instagram — that would violate our ethics
posture (see ADR-0004).

Config:
    urls: list[str]  — public post URLs to fetch
"""
from __future__ import annotations

import structlog

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

_OEMBED_URL = "https://api.instagram.com/oembed/"


class InstagramOEmbedCollector(BaseCollector):
    """Fetch public Instagram post metadata via the official oEmbed endpoint.

    Limited utility: it gives us *given* posts, not discovered trends. The
    value is normalizing this data into the same Trend schema so it can
    be joined with the other platforms' trend data in the database.
    """

    platform = "instagram"
    timeout_s = 10.0

    async def collect(self) -> list[Trend]:
        urls: list[str] = self.config.get("urls", [])
        if not urls:
            logger.info("instagram.no_urls_configured")
            return []

        trends: list[Trend] = []
        for url in urls:
            payload = await self.get_json(
                _OEMBED_URL,
                params={"url": url},
            )
            if payload is None:
                continue
            try:
                trend = self._payload_to_trend(payload)
            except Exception as e:  # noqa: BLE001
                logger.debug("instagram.oembed.item_skip", url=url, error=str(e))
                continue
            trends.append(trend)
        logger.info("instagram.oembed.collected", count=len(trends))
        return trends

    @staticmethod
    def _payload_to_trend(payload: dict) -> Trend:
        # oEmbed response:
        # {type, version, title, author_name, author_url, provider_name,
        #  provider_url, width, height, html, thumbnail_url, ...}
        author = payload.get("author_name", "unknown")
        title = payload.get("title", "")
        url = payload.get("url") or payload.get("provider_url", "")
        # We don't get post count or trending info from oEmbed — the score
        # is 0 and the metadata carries the relevant details.
        return make_trend(
            platform="instagram",
            name=f"@{author}",
            trend_type="creator",
            platform_native_id=url or author,
            url=url,
            score=0.0,
            metadata={
                "title": title,
                "thumbnail_url": payload.get("thumbnail_url"),
                "author_url": payload.get("author_url"),
                "html": payload.get("html"),  # useful for embedding
            },
        )

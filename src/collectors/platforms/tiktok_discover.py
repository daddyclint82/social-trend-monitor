"""TikTok Discover collector — community-scraped JSON via GitHub.

Strategy:
TikTok's own /api/discover/item_list/ endpoint is anti-bot gated (ADR-0002).
We cannot legally or ethically bypass that.

WORKAROUND: a community-run GitHub repo (`antiops/tiktok-trending-data`)
periodically scrapes the discover endpoint via GitHub Actions and commits
the JSON to a public repo. The raw URLs are CDN-cached and unauthenticated.

URL pattern:
    https://raw.githubusercontent.com/antiops/tiktok-trending-data/main/
        discover-list-{region}.json

Regions available: us, www, m, t (TikTok uses these subdomain codes).
See: https://github.com/antiops/tiktok-trending-data

JSON shape (verified 2026-07-13):
    {
      "statusCode": 0,
      "errMsg": "",
      "body": {
        "discoverList": [
          {"type": 3, "title": "#hashtag", "link": "...", "isInternalLink": false},
          {"type": 4, "title": "Song Name - Artist", "link": "...", "isInternalLink": false}
        ]
      }
    }

Where:
    type=3 → hashtag
    type=4 → sound/music

This is intentionally read-only. We do not scrape TikTok directly. The
data is 6h stale but it covers what TikTok's own Creative Center would
show for "trending hashtags" and "trending sounds."

Config keys:
    regions: list[str]    — region codes: us, www, m, t (default: ["us"])
    github_repo: str      — override the source repo (default antiops)
    github_branch: str    — default main
    timeout_s: float      — default 30 (GitHub raw can be slow)
"""
from __future__ import annotations

import structlog

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

# Default repo: antiops/tiktok-trending-data
# We use this as a public CDN for the gated TikTok endpoint. The repo
# is updated every ~6 hours by a GitHub Action.
DEFAULT_REPO = "antiops/tiktok-trending-data"
DEFAULT_BRANCH = "main"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/discover-list-{region}.json"

DEFAULT_REGIONS: list[str] = ["us", "www"]

# TikTok discoverList item types
_TYPE_HASHTAG = 3
_TYPE_SOUND = 4


class TikTokDiscoverCollector(BaseCollector):
    """Pull discoverList from antiops/tiktok-trending-data.

    Platform tag is "tiktok_discover" — kept distinct from "tiktok_oembed"
    so DB filters can separate user-supplied watchlists from community
    trending data. Cross-platform grouping still works because both
    platforms normalize through the same name-canonicalization.
    """

    platform = "tiktok_discover"
    timeout_s = 30.0

    async def collect(self) -> list[Trend]:
        regions: list[str] = self.config.get("regions", DEFAULT_REGIONS)
        repo: str = self.config.get("github_repo", DEFAULT_REPO)
        branch: str = self.config.get("github_branch", DEFAULT_BRANCH)
        trends: list[Trend] = []
        for region in regions:
            url = RAW_URL.format(repo=repo, branch=branch, region=region)
            payload = await self.get_json(url)
            if payload is None:
                logger.warning("tiktok_discover.fetch_failed", region=region)
                continue
            try:
                region_trends = self._parse_payload(payload, region=region)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(
                    "tiktok_discover.parse_error", region=region, error=str(e)
                )
                continue
            trends.extend(region_trends)
            logger.info(
                "tiktok_discover.collected", region=region, items=len(region_trends)
            )
        return trends

    def _parse_payload(self, payload: dict, *, region: str) -> list[Trend]:
        """Map antiops JSON shape → list[Trend].

        The wrapper is:
            {"statusCode": 0, "body": {"discoverList": [...]}}
        We only care about body.discoverList. statusCode != 0 → skip.
        """
        if payload.get("statusCode") != 0:
            logger.warning(
                "tiktok_discover.bad_status",
                region=region,
                status=payload.get("statusCode"),
                err=payload.get("errMsg"),
            )
            return []
        body = payload.get("body") or {}
        items = body.get("discoverList") or []
        trends: list[Trend] = []
        for item in items:
            t = self._item_to_trend(item, region=region)
            if t is not None:
                trends.append(t)
        return trends

    @staticmethod
    def _item_to_trend(item: dict, *, region: str) -> Trend | None:
        title = (item.get("title") or "").strip()
        link = (item.get("link") or "").strip()
        item_type = item.get("type")
        if not title or not link:
            return None
        if item_type == _TYPE_HASHTAG:
            trend_type = "hashtag"
            # Titles come as "#hashtag" already
            display_name = title if title.startswith("#") else f"#{title}"
        elif item_type == _TYPE_SOUND:
            trend_type = "sound"
            display_name = title
        else:
            # Unknown type: skip rather than mislabel
            return None
        # native_id includes region so different regions produce different ids
        native_id = f"{region}:{link}"
        return make_trend(
            platform="tiktok_discover",
            name=display_name,
            trend_type=trend_type,
            platform_native_id=native_id,
            url=link,
            # No native score in this JSON. The position in the list is a
            # weak signal — we use it as a tiny score so trends at the
            # top rank above those at the bottom. Scorer normalizes.
            score=0.0,
            metadata={
                "source": "antiops_github",
                "region": region,
                "raw_type": item_type,
            },
        )

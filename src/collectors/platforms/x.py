"""X (Twitter) API v2 trends collector.

Source: https://api.x.com/2/trends/by/woeid/:woeid

The official X API v2 trends endpoint. Requires a Bearer token (paid tier —
free tier was killed in 2023). Returns 50 trending topics per WOEID (Yahoo!
Where On Earth ID).

WOEIDs to monitor (default): worldwide (1) + 5 major regions.
Override via config['woeids'].
"""
from __future__ import annotations

import structlog
import os

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

# Default WOEIDs: worldwide + top regions for content strategy.
DEFAULT_WOEIDS: list[int] = [
    1,        # Worldwide
    23424977, # United States
    23424975, # United Kingdom
    23424768, # Brazil
    23424856, # Japan
    23424819, # India
]

_TRENDS_URL = "https://api.x.com/2/trends/by/woeid/{woeid}"


class XTrendsCollector(BaseCollector):
    """Pulls trending topics from X API v2 for configured WOEIDs.

    Config keys:
        woeids: list[int]          — WOEIDs to fetch (default DEFAULT_WOEIDS)
        bearer_token_env: str      — env var name holding the bearer token
                                     (default 'X_BEARER_TOKEN')
        bearer_token: str          — literal token (overrides env var; v1
                                     testing convenience only)
    """

    platform = "x"
    timeout_s = 15.0

    async def collect(self) -> list[Trend]:
        token = self._resolve_token()
        if not token:
            logger.warning("x.bearer_token_missing")
            return []

        woeids: list[int] = self.config.get("woeids", DEFAULT_WOEIDS)
        all_trends: list[Trend] = []
        for woeid in woeids:
            url = _TRENDS_URL.format(woeid=woeid)
            payload = await self.get_json(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            if payload is None:
                continue
            items = self._extract_items(payload)
            for item in items:
                try:
                    trend = self._item_to_trend(item, woeid)
                except Exception as e:  # noqa: BLE001
                    logger.debug("x.item_skip", woeid=woeid, error=str(e))
                    continue
                all_trends.append(trend)
            logger.info("x.collected", woeid=woeid, count=len(items))
        return all_trends

    def _resolve_token(self) -> str | None:
        # 1. Literal in config (v1 testing only)
        if self.config.get("bearer_token"):
            return self.config["bearer_token"]
        # 2. Env var
        env_name = self.config.get("bearer_token_env", "X_BEARER_TOKEN")
        return os.environ.get(env_name)

    @staticmethod
    def _extract_items(payload) -> list[dict]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        return []

    @staticmethod
    def _item_to_trend(item: dict, woeid: int) -> Trend:
        # X API v2 trends response shape (current docs):
        # {"trend_name": "...", "tweet_count": 12345, "rank": 1}
        # Some older shapes use "name" + "tweet_volume".
        name = item.get("trend_name") or item.get("name")
        if not name:
            raise ValueError("no trend_name field")
        # Normalize: ensure it starts with # if it looks like a hashtag
        if not name.startswith("#") and " " not in name and len(name) <= 50:
            name = f"#{name}"
        tweet_count = (
            item.get("tweet_count")
            or item.get("tweet_volume")
            or item.get("volume")
            or 0
        )
        score = float(tweet_count)
        rank = item.get("rank")
        # X doesn't expose a direct URL per trend in the v2 trends endpoint.
        # Best effort: search URL.
        query = name.lstrip("#")
        url = f"https://x.com/search?q=%23{query}&src=trend_click"
        return make_trend(
            platform="x",
            name=name,
            trend_type="topic",
            platform_native_id=f"{woeid}:{name}",
            url=url,
            score=score,
            metadata={"woeid": woeid, "rank": rank},
        )

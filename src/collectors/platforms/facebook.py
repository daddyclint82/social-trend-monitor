"""Facebook Graph API collector (Page-level, opt-in).

Facebook trend discovery is not possible without violating the
public-only posture (see ADR-0004 and `docs/research/platforms.md`).
This collector reads posts from public Pages the user has admin/editor
access to — useful for monitoring *your own* Facebook page's posts in
the context of the broader trend data.

Requires:
    FB_PAGE_TOKENS env var: JSON map of {page_id: page_access_token}

Or config['page_tokens'] = {"123": "EAA..."}.

Graph API endpoint:
    GET /v21.0/{page-id}/posts?fields=id,message,created_time,reactions.summary(true),comments.summary(true),shares
"""
from __future__ import annotations

import json
import structlog
import os

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

_GRAPH_URL = "https://graph.facebook.com/v21.0/{page_id}/posts"


class FacebookPageCollector(BaseCollector):
    """Read recent posts from Facebook Pages you have tokens for."""

    platform = "facebook"
    timeout_s = 15.0

    async def collect(self) -> list[Trend]:
        tokens = self._resolve_tokens()
        if not tokens:
            logger.info("facebook.no_page_tokens")
            return []

        all_trends: list[Trend] = []
        for page_id, token in tokens.items():
            url = _GRAPH_URL.format(page_id=page_id)
            payload = await self.get_json(
                url,
                params={
                    "fields": "id,message,created_time,reactions.summary(true),comments.summary(true),shares",
                    "access_token": token,
                    "limit": self.config.get("limit", 25),
                },
            )
            if payload is None:
                continue
            for item in (payload.get("data") or []):
                try:
                    trend = self._item_to_trend(item, page_id)
                except Exception as e:  # noqa: BLE001
                    logger.debug("facebook.item_skip", error=str(e))
                    continue
                all_trends.append(trend)
            logger.info("facebook.collected", page_id=page_id, count=len(payload.get("data") or []))
        return all_trends

    def _resolve_tokens(self) -> dict[str, str]:
        if self.config.get("page_tokens"):
            return dict(self.config["page_tokens"])
        env_name = self.config.get("page_tokens_env", "FB_PAGE_TOKENS")
        raw = os.environ.get(env_name)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("facebook.page_tokens_json_error", error=str(e))
            return {}

    @staticmethod
    def _item_to_trend(item: dict, page_id: str) -> Trend:
        post_id = item.get("id", "unknown")
        message = item.get("message", "") or ""
        url = f"https://facebook.com/{post_id.replace('_', '/posts/')}"
        # Score = sum of reactions + comments + shares (rough engagement)
        reactions = (item.get("reactions") or {}).get("summary", {}).get("total_count", 0)
        comments = (item.get("comments") or {}).get("summary", {}).get("total_count", 0)
        shares = (item.get("shares") or {}).get("count", 0)
        score = float(reactions + comments + shares)
        return make_trend(
            platform="facebook",
            name=message[:80] or f"Post {post_id}",
            trend_type="video" if "video" in (item.get("status_type", "") or "") else "topic",
            platform_native_id=post_id,
            url=url,
            score=score,
            metadata={
                "page_id": page_id,
                "created_time": item.get("created_time"),
                "reactions": reactions,
                "comments": comments,
                "shares": shares,
            },
        )

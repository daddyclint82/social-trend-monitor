"""Reddit collector via official OAuth API (script app).

Source: https://www.reddit.com/dev/api/

The unauthenticated `.json` endpoints were deprecated in late 2025 and now
return 403. This collector uses the official OAuth2 client_credentials flow
with a script-type app (free, public-data access, 100 req/min).

Reference: ADR-0011.

Endpoints used (all public read-only listings):
- GET /r/all/hot
- GET /r/popular (newer discovery surface; falls back to /r/all/hot if empty)
- GET /r/{subreddit}/top?t=day  (for each configured niche sub)
- GET /subreddits/popular       (trending subreddit list)

Trend types emitted:
- trend_type="post"      — top post from a feed
- trend_type="subreddit" — trending subreddit from /subreddits/popular

Mapping decisions:
- Score = ups (post score) or subscriber_count (subreddit)
- Cross-platform key uses the post title normalized (lowercase, whitespace
  collapsed) so a Reddit hot post titled "Taylor Swift announces..." groups
  with a TikTok trend named "Taylor Swift".
- We do NOT collect comments, author IDs beyond what's in the listing, or
  PII. Listing JSON only.
"""
from __future__ import annotations

import asyncio
import os
import structlog
import time
from base64 import b64encode
from typing import Any

import httpx

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

# OAuth endpoint
_REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

# Public read endpoints
_REDDIT_OAUTH_BASE = "https://oauth.reddit.com"
_LISTING_PATH = "/{path}.json"

# Default user-agent for Reddit (script apps must identify themselves)
_REDDIT_USER_AGENT_TEMPLATE = (
    "SocialTrendMonitor/0.1 (script:{client_id}; "
    "+https://github.com/DaddyClint82/social-trend-monitor)"
)

# Default niche subreddits for content-strategy discovery
DEFAULT_NICHE_SUBREDDITS: list[str] = [
    "technology",
    "programming",
    "artificial",
    "marketing",
    "socialmedia",
    "YouTubers",
    "TikTok",
    "Instagram",
    "podcasting",
    "design",
    "photography",
    "futurology",
]

# Public default feeds (always polled unless disabled)
DEFAULT_FEEDS: list[str] = [
    "r/all/hot",
    "r/popular",
]


class RedditCollector(BaseCollector):
    """Pulls trending posts + subreddits from Reddit's official API.

    Config keys:
        client_id: str              — script app client_id (literal)
        client_secret: str          — script app client_secret (literal)
        client_id_env: str          — env var name for client_id (preferred)
        client_secret_env: str      — env var name for client_secret (preferred)
        user_agent: str             — overrides default; required for some
                                       subreddits to allow listing
        feeds: list[str]            — public feeds to poll (default DEFAULT_FEEDS)
        niche_subreddits: list[str] — additional subs to poll (top t=day)
        max_posts_per_listing: int  — cap per listing (default 25)
        max_subscribers: int        — min subs to include a trending sub
        max_age_minutes: int         — skip posts older than this in feeds
    """

    platform = "reddit"
    timeout_s = 15.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._token_lock = asyncio.Lock()

    # ---- credential resolution ----

    def _resolve_creds(self) -> tuple[str | None, str | None]:
        cid = (
            self.config.get("client_id")
            or os.environ.get(self.config.get("client_id_env", "REDDIT_CLIENT_ID",))
        )
        secret = (
            self.config.get("client_secret")
            or os.environ.get(self.config.get("client_secret_env", "REDDIT_SECRET",))
        )
        return cid, secret

    def _user_agent(self) -> str:
        if self.config.get("user_agent"):
            return self.config["user_agent"]
        cid, _ = self._resolve_creds()
        # Reddit requires a descriptive UA; show client_id (or placeholder)
        return _REDDIT_USER_AGENT_TEMPLATE.format(client_id=cid or "anonymous")

    # ---- OAuth token management ----

    async def _get_token(self) -> str | None:
        """Return a valid bearer token, refreshing if needed.

        Uses client_credentials grant for script-type apps. The token is
        cached in-process until 60s before its stated expiry.
        """
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expiry:
                return self._token

            cid, secret = self._resolve_creds()
            if not cid or not secret:
                logger.warning("reddit.creds_missing")
                return None

            # Reddit requires HTTP Basic auth for client_credentials
            basic = b64encode(f"{cid}:{secret}".encode()).decode()
            headers = {
                "Authorization": f"Basic {basic}",
                "User-Agent": self._user_agent(),
                "Content-Type": "application/x-www-form-urlencoded",
            }
            data = {"grant_type": "client_credentials"}

            try:
                # Don't go through self.limiter for the token endpoint —
                # it's a single startup call, and we don't want to share
                # the bucket with the data-fetching GETs.
                async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                    resp = await c.post(
                        _REDDIT_TOKEN_URL, headers=headers, data=data
                    )
            except httpx.HTTPError as e:
                logger.warning("reddit.token_http_error", error=str(e))
                return None

            if resp.status_code >= 400:
                logger.warning(
                    "reddit.token_status",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return None

            try:
                payload = resp.json()
            except ValueError:
                return None

            token = payload.get("access_token")
            expires_in = int(payload.get("expires_in", 3600))
            if not token:
                logger.warning("reddit.token_no_access_token", payload=payload)
                return None

            self._token = token
            # Refresh 60s early to avoid edge-of-expiry 401s
            self._token_expiry = now + max(60, expires_in - 60)
            logger.info("reddit.token_acquired", expires_in=expires_in)
            return self._token

    async def _authed_get(
        self, path: str, params: dict | None = None
    ) -> dict | list | None:
        """OAuth-authenticated GET against oauth.reddit.com.

        Wraps the base get_json with Reddit-specific auth headers and UA.
        """
        token = await self._get_token()
        if not token:
            return None
        url = f"{_REDDIT_OAUTH_BASE}{path}.json"
        headers = {
            "Authorization": f"bearer {token}",
            "User-Agent": self._user_agent(),
        }
        return await self.get_json(url, params=params, headers=headers)

    # ---- public collect() ----

    async def collect(self) -> list[Trend]:
        cid, secret = self._resolve_creds()
        if not cid or not secret:
            logger.info("reddit.skipped_no_creds")
            return []

        trends: list[Trend] = []
        max_posts = int(self.config.get("max_posts_per_listing", 25))
        max_age_min = int(self.config.get("max_age_minutes", 1440))  # 24h
        feeds: list[str] = list(self.config.get("feeds", DEFAULT_FEEDS))
        niche: list[str] = list(
            self.config.get("niche_subreddits", DEFAULT_NICHE_SUBREDDITS)
        )

        # 1) Public default feeds (r/all, r/popular)
        for feed in feeds:
            path = feed if feed.startswith("/") else f"/{feed}"
            payload = await self._authed_get(path, params={"limit": max_posts})
            items = self._extract_listing(payload)
            for post in items:
                try:
                    t = self._post_to_trend(post, source=feed)
                except Exception as e:  # noqa: BLE001
                    logger.debug("reddit.post_skip", feed=feed, error=str(e))
                    continue
                if self._is_fresh(post, max_age_min):
                    trends.append(t)
            logger.info("reddit.feed_collected", feed=feed, count=len(items))

        # 2) Niche subreddit top-of-day
        for sub in niche:
            path = f"/r/{sub}/top"
            payload = await self._authed_get(
                path, params={"t": "day", "limit": max_posts}
            )
            items = self._extract_listing(payload)
            for post in items:
                try:
                    t = self._post_to_trend(post, source=f"r/{sub}")
                except Exception as e:  # noqa: BLE001
                    logger.debug("reddit.post_skip", sub=sub, error=str(e))
                    continue
                if self._is_fresh(post, max_age_min):
                    trends.append(t)
            logger.info("reddit.niche_collected", sub=sub, count=len(items))

        # 3) Trending subreddits
        subs_payload = await self._authed_get(
            "/subreddits/popular", params={"limit": 25}
        )
        sub_items = self._extract_listing(subs_payload)
        for sub in sub_items:
            try:
                t = self._subreddit_to_trend(sub)
            except Exception as e:  # noqa: BLE001
                logger.debug("reddit.sub_skip", error=str(e))
                continue
            trends.append(t)
        logger.info("reddit.subreddits_collected", count=len(sub_items))

        return trends

    # ---- response parsing ----

    @staticmethod
    def _extract_listing(payload: Any) -> list[dict]:
        """Reddit's listing response: { data: { children: [{data: {...}}, ...] } }

        Some endpoints wrap things slightly differently; we normalize to a
        flat list of post data dicts.
        """
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        children = data.get("children", [])
        out: list[dict] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            d = child.get("data")
            if isinstance(d, dict):
                out.append(d)
        return out

    @staticmethod
    def _is_fresh(post: dict, max_age_minutes: int) -> bool:
        """Filter out old posts. Reddit's `created_utc` is unix seconds."""
        ts = post.get("created_utc")
        if not ts:
            return True
        try:
            age_min = (time.time() - float(ts)) / 60.0
        except (TypeError, ValueError):
            return True
        return age_min <= max_age_minutes

    @staticmethod
    def _post_to_trend(post: dict, source: str) -> Trend:
        """Map a Reddit post listing to a Trend."""
        title = (post.get("title") or "").strip()
        if not title:
            raise ValueError("post has no title")
        name = post.get("name", "")  # e.g. "t3_abc123"
        # Use the fullname as the platform-native id (stable, unique)
        pid = name or post.get("id") or title
        ups = int(post.get("ups") or post.get("score") or 0)
        num_comments = int(post.get("num_comments") or 0)
        permalink = post.get("permalink") or ""
        url = (
            f"https://www.reddit.com{permalink}" if permalink else post.get("url")
        )
        # Trending entity: we use the post title as the trend name because
        # that is the public, shareable artifact. The subreddit is in metadata.
        return make_trend(
            platform="reddit",
            name=title,
            trend_type="post",
            platform_native_id=pid,
            url=url,
            score=float(ups),
            metadata={
                "source_feed": source,
                "subreddit": post.get("subreddit"),
                "num_comments": num_comments,
                "upvote_ratio": post.get("upvote_ratio"),
                "author": post.get("author"),
                "domain": post.get("domain"),
                "is_video": bool(post.get("is_video")),
                "over_18": bool(post.get("over_18")),
                "permalink": permalink,
                "created_utc": post.get("created_utc"),
            },
        )

    @staticmethod
    def _subreddit_to_trend(sub: dict) -> Trend:
        """Map a /subreddits/popular entry to a Trend of type 'subreddit'."""
        name = (sub.get("display_name") or sub.get("name") or "").strip()
        if not name:
            raise ValueError("subreddit has no name")
        sub_id = sub.get("id") or name
        subs = int(sub.get("subscribers") or 0)
        url = f"https://www.reddit.com/r/{name}/"
        return make_trend(
            platform="reddit",
            name=f"r/{name}",
            trend_type="subreddit",
            platform_native_id=f"sub_{sub_id}",
            url=url,
            score=float(subs),
            metadata={
                "subscribers": subs,
                "active_user_count": sub.get("active_user_count"),
                "public_description": sub.get("public_description"),
                "over_18": bool(sub.get("over_18")),
            },
        )

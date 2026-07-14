"""Apify vendor bridge collector.

Source: https://docs.apify.com/api/v2/

This collector delegates trend discovery to public Apify Actors (community
scrapers) for TikTok and Instagram. It is OPT-IN (disabled by default,
requires APIFY_TOKEN) and includes a cost guard so the $5/month free tier
isn't accidentally blown by an aggressive poll cycle.

Reference: ADR-0012.

Design:
- Single collector, `platform = "apify"`. Trends it produces carry their
  source platform in `metadata["source_platform"]` (tiktok or instagram).
- Uses the Apify v2 **synchronous** run endpoint:
  POST /v2/acts/{actorId}/run-sync-get-dataset-items
  Returns dataset items directly in the response (no run-state machine).
- Cost guard: each cycle checks a persisted monthly-spend counter
  (SQLite: apify_spend table) and skips the run if the cap would be
  exceeded.
- Min interval between cycles (default 4h) — actors are slow, we don't
  want to hammer.
- Trend mapping is per-actor:
  - TikTok: items have hashtag names, view counts, video URLs
  - Instagram: items have usernames, follower counts, post engagement

We keep the actor input/output mapping config-driven so a user can swap
in a different Actor without code changes.
"""
from __future__ import annotations

import os
import structlog
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ...normalizer.schema import Trend, make_trend
from ..base import BaseCollector

logger = structlog.get_logger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"

# Default actor registry — what we ship out of the box
# Each entry: (actor_id, source_platform, input_template, item_mapper_name)
DEFAULT_ACTORS: list[dict] = [
    {
        "actor_id": "clockworks~tiktok-scraper",
        "source_platform": "tiktok",
        "description": "Generic TikTok scraper; we feed it trending hashtag names",
        "input": {
            "hashtags": ["fyp", "foryou", "viral"],
            "resultsPerPage": 20,
            "shouldDownloadVideos": False,
        },
        "item_mapper": "tiktok_post",
    },
    {
        "actor_id": "apify~instagram-scraper",
        "source_platform": "instagram",
        "description": "Generic Instagram profile+post scraper",
        "input": {
            "usernames": ["instagram"],
            "resultsLimit": 20,
        },
        "item_mapper": "instagram_post",
    },
]


# ---------- main collector ----------


class ApifyBridgeCollector(BaseCollector):
    """Run configured Apify Actors and map their dataset items to Trends.

    Config keys:
        token: str               — Apify API token (literal)
        token_env: str           — env var name (default APIFY_TOKEN)
        actors: list[dict]       — actor registry (default DEFAULT_ACTORS)
        min_interval_hours: float — min hours between cycles per actor
                                    (default 4)
        monthly_cap_usd: float   — hard cap on spend per calendar month
                                   (default 4.00 — leaves buffer under
                                   the $5 free tier)
        per_cycle_cap_usd: float — soft cap per single cycle (default 0.10)
        timeout_s: float         — HTTP timeout for the run-sync call
                                   (default 240s; endpoint max 300s)
        max_items_per_actor: int — cap items we accept per actor run
                                   (default 100)

    The spend tracker is persisted via a shared `ApifySpendLedger`
    instance injected at construction. The orchestrator wires this up.
    """

    platform = "apify"
    timeout_s = 240.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # In-process last-run cache (actor_id -> monotonic timestamp)
        self._last_run: dict[str, float] = {}

    # ---- credentials ----

    def _resolve_token(self) -> str | None:
        return (
            self.config.get("token")
            or os.environ.get(self.config.get("token_env", "APIFY_TOKEN"))
        )

    # ---- public collect() ----

    async def collect(self) -> list[Trend]:
        token = self._resolve_token()
        if not token:
            logger.info("apify.skipped_no_token")
            return []

        ledger: ApifySpendLedger | None = self.config.get("_spend_ledger")
        monthly_cap = float(self.config.get("monthly_cap_usd", 4.0))
        per_cycle_cap = float(self.config.get("per_cycle_cap_usd", 0.10))
        min_interval_h = float(self.config.get("min_interval_hours", 4))
        max_items = int(self.config.get("max_items_per_actor", 100))

        # Pre-flight: monthly cap check
        if ledger is not None:
            month_spent = ledger.month_total()
            if month_spent >= monthly_cap:
                logger.warning(
                    "apify.monthly_cap_reached",
                    spent=round(month_spent, 4),
                    cap=monthly_cap,
                )
                return []

        actors = self.config.get("actors", DEFAULT_ACTORS)
        all_trends: list[Trend] = []

        for actor in actors:
            actor_id = actor["actor_id"]
            # Min-interval gate
            last = self._last_run.get(actor_id)
            if last is not None:
                hours_since = (time.monotonic() - last) / 3600.0
                if hours_since < min_interval_h:
                    logger.info(
                        "apify.actor_skipped_too_soon",
                        actor=actor_id,
                        hours_since=round(hours_since, 2),
                        min_hours=min_interval_h,
                    )
                    continue

            # Per-cycle cap check
            if ledger is not None and per_cycle_cap > 0:
                cycle_spent = ledger.cycle_total()
                if cycle_spent >= per_cycle_cap:
                    logger.warning(
                        "apify.cycle_cap_reached",
                        spent=round(cycle_spent, 4),
                        cap=per_cycle_cap,
                    )
                    break

            items = await self._run_actor(actor, token, ledger, max_items)
            self._last_run[actor_id] = time.monotonic()
            if items is None:
                continue
            mapper = _MAPPERS.get(actor.get("item_mapper", ""))
            if mapper is None:
                logger.warning("apify.no_mapper", actor=actor_id, mapper=actor.get("item_mapper"))
                continue
            for item in items[:max_items]:
                try:
                    t = mapper(item, actor=actor)
                except Exception as e:  # noqa: BLE001
                    logger.debug("apify.item_skip", actor=actor_id, error=str(e))
                    continue
                all_trends.append(t)
            logger.info(
                "apify.actor_collected",
                actor=actor_id,
                items=min(len(items), max_items),
            )

        return all_trends

    # ---- HTTP path ----

    async def _run_actor(
        self,
        actor: dict,
        token: str,
        ledger: "ApifySpendLedger | None",
        max_items: int,
    ) -> list[dict] | None:
        actor_id = actor["actor_id"]
        url = f"{APIFY_API_BASE}/acts/{actor_id}/run-sync-get-dataset-items"
        params = {"token": token, "format": "json", "limit": max_items}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        payload = actor.get("input", {})

        await self.limiter.acquire(url)
        try:
            resp = await self.http.post(
                url,
                params=params,
                headers=headers,
                json=payload,
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as e:
            logger.warning("apify.http_error", actor=actor_id, error=str(e))
            return None

        if resp.status_code >= 400:
            logger.warning(
                "apify.http_status",
                actor=actor_id,
                status=resp.status_code,
                body=resp.text[:200],
            )
            return None

        # Persist run cost if ledger is available
        # Apify includes usage in the response headers:
        # x-apify-usage-total-usd (free tier) or x-apify-usage-actor-usd
        cost = _extract_cost_from_headers(resp.headers)
        if cost > 0 and ledger is not None:
            ledger.record(
                actor_id=actor_id,
                usd=cost,
                items=len(resp.json() or []) if isinstance(resp.json(), list) else 0,
            )

        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, list):
            # Sometimes wrapped in {"items": [...]}
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            logger.warning("apify.unexpected_payload_shape", actor=actor_id)
            return None
        return data


# ---------- cost extraction ----------


def _extract_cost_from_headers(headers) -> float:
    """Apify's billing headers are: x-apify-usage-* (free tier friendly)."""
    for key in (
        "x-apify-usage-total-usd",
        "x-apify-usage-actor-usd",
        "x-apify-usage",
    ):
        v = headers.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


# ---------- item mappers ----------


def _tiktok_post_to_trend(item: dict, *, actor: dict) -> Trend:
    """Map a clockworks/tiktok-scraper item to a Trend.

    Item shape (typical):
    {
      "id": "...",
      "text": "...",
      "hashtags": [{"name": "fyp"}, ...]  OR  "hashtags": ["fyp", ...],
      "playCount": 12345,
      "diggCount": 1000,
      "authorMeta": {"name": "...", "fans": 100000},
      "webVideoUrl": "..."
    }
    """
    hashtags = item.get("hashtags") or []
    primary: str | None = None
    if isinstance(hashtags, list) and hashtags:
        if isinstance(hashtags[0], dict):
            names = [h.get("name", "") for h in hashtags if isinstance(h, dict)]
        else:
            names = [str(h) for h in hashtags if h]
        # Use the first hashtag as the trend name (the most common pattern)
        primary = next((n for n in names if n), None)

    has_hashtag = primary is not None
    if not primary:
        text = (item.get("text") or "").strip()
        primary = (text[:40] + "…") if len(text) > 40 else (text or "tiktok-post")

    name = f"#{primary}" if not primary.startswith("#") else primary
    play_count = int(item.get("playCount") or item.get("viewCount") or 0)
    url = (
        item.get("webVideoUrl")
        or item.get("videoUrl")
        or item.get("url")
    )
    platform_id = item.get("id") or name
    return make_trend(
        platform="apify",
        name=name,
        trend_type="hashtag" if has_hashtag else "post",
        platform_native_id=platform_id,
        url=url,
        score=float(play_count),
        metadata={
            "source_platform": "tiktok",
            "apify_actor": actor.get("actor_id"),
            "digg_count": item.get("diggCount"),
            "share_count": item.get("shareCount"),
            "comment_count": item.get("commentCount"),
            "author": (item.get("authorMeta") or {}).get("name"),
            "author_fans": (item.get("authorMeta") or {}).get("fans"),
            "all_hashtags": names if has_hashtag else None,
        },
    )


def _instagram_post_to_trend(item: dict, *, actor: dict) -> Trend:
    """Map an apify/instagram-scraper item to a Trend.

    Item shape (typical):
    {
      "id": "...",
      "type": "Image" | "Video" | "Sidecar",
      "shortCode": "...",
      "caption": "...",
      "hashtags": ["aiart", ...],
      "likesCount": 1000,
      "commentsCount": 50,
      "ownerUsername": "...",
      "url": "..."
    }
    """
    hashtags = item.get("hashtags") or []
    has_hashtag = bool(hashtags)
    if has_hashtag:
        primary = str(hashtags[0])
    else:
        caption = (item.get("caption") or "").strip()
        primary = (caption[:40] + "…") if len(caption) > 40 else (caption or "instagram-post")

    name = f"#{primary}" if not primary.startswith("#") else primary
    likes = int(item.get("likesCount") or 0)
    url = item.get("url") or (
        f"https://www.instagram.com/p/{item.get('shortCode')}/"
        if item.get("shortCode") else None
    )
    platform_id = item.get("id") or item.get("shortCode") or name
    return make_trend(
        platform="apify",
        name=name,
        trend_type="hashtag" if has_hashtag else "post",
        platform_native_id=platform_id,
        url=url,
        score=float(likes),
        metadata={
            "source_platform": "instagram",
            "apify_actor": actor.get("actor_id"),
            "comments_count": item.get("commentsCount"),
            "owner": item.get("ownerUsername"),
            "type": item.get("type"),
            "all_hashtags": list(hashtags) if has_hashtag else None,
        },
    )


_MAPPERS: dict[str, Any] = {
    "tiktok_post": _tiktok_post_to_trend,
    "instagram_post": _instagram_post_to_trend,
}


# ---------- spend ledger ----------


class ApifySpendLedger:
    """In-memory + SQLite-backed monthly spend tracker.

    The orchestrator creates one instance and shares it across cycles by
    passing it into the collector config as `config["_spend_ledger"]`.
    On each Apify cycle, the collector calls `ledger.record(...)` for
    each actor run, and `ledger.month_total()` before each run to gate
    future runs.

    Persistence: a small SQLite table `apify_spend` keyed by month
    (YYYY-MM string). Cycle totals are in-memory (resets at process
    restart — that's fine, cycles are short).
    """

    def __init__(self, db_path: str) -> None:
        import sqlite3
        self._db_path = db_path
        self._cycle_start: float = time.monotonic()
        self._cycle_spent: float = 0.0
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS apify_spend (
                month TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                usd REAL NOT NULL,
                items INTEGER NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def record(self, *, actor_id: str, usd: float, items: int) -> None:
        """Record a spend event."""
        month = datetime.now(tz=timezone.utc).strftime("%Y-%m")
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO apify_spend (month, actor_id, usd, items, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (month, actor_id, usd, items, now_iso),
        )
        self._conn.commit()
        self._cycle_spent += usd
        logger.info("apify.spend_recorded", actor=actor_id, usd=round(usd, 4))

    def month_total(self) -> float:
        """Sum of all spend for the current calendar month (UTC)."""
        month = datetime.now(tz=timezone.utc).strftime("%Y-%m")
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(usd), 0) FROM apify_spend WHERE month = ?",
            (month,),
        )
        row = cur.fetchone()
        return float(row[0]) if row else 0.0

    def cycle_total(self) -> float:
        """Total spend in the current process lifetime (resets on restart)."""
        return self._cycle_spent

    def close(self) -> None:
        self._conn.close()

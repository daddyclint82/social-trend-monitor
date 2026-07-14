"""Orchestrator — runs the collection cycle.

Each cycle:
1. Asks the registry which collectors are active
2. Runs them in parallel (asyncio.gather)
3. Deduplicates & scores
4. Writes to storage
5. Records a run entry per platform
"""
from __future__ import annotations

import asyncio
import structlog
from datetime import datetime, timezone
from typing import Any

import httpx

from .collectors.base import BaseCollector
from .collectors.registry import CollectorRegistry
from .collectors.platforms.apify import ApifySpendLedger
from .config import AppConfig
from .normalizer.schema import Trend
from .normalizer.semantic import SemanticGrouper
from .scoring.engine import cross_platform_groups, score
from .storage.db import Storage
from .utils.rate_limit import RateLimiter

logger = structlog.get_logger(__name__)


class Orchestrator:
    def __init__(self, config: AppConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self.registry = CollectorRegistry()
        self.registry.discover()
        self.limiter = RateLimiter(
            default_rate=config.rate_limits.default.rate,
            default_burst=config.rate_limits.default.burst or 5,
            jitter_pct=config.rate_limits.jitter_pct,
        )
        for host, hl in config.rate_limits.per_host.items():
            self.limiter.set_host_rate(host, rate=hl.rate, burst=hl.burst)
        # Apify spend ledger: persists across cycles (same Storage db)
        # Only created if apify collector is enabled (saves a no-op DB table)
        self.apify_ledger: ApifySpendLedger | None = None
        if config.collectors.get("apify") and config.collectors["apify"].enabled:
            self.apify_ledger = ApifySpendLedger(config.storage.db_path)
        logger.info(
            "orchestrator.initialized",
            collectors=self.registry.available(),
        )

    def _is_enabled(self, platform: str) -> bool:
        return bool(self.config.collectors.get(platform, {}).enabled)

    def _build_collector(
        self, platform: str, http: httpx.AsyncClient
    ) -> BaseCollector | None:
        cls = self.registry.get(platform)
        if cls is None:
            return None
        opts = self.config.collector_options.get(platform, {})
        # Inject the Apify spend ledger if applicable
        if platform == "apify" and self.apify_ledger is not None:
            opts = {**opts, "_spend_ledger": self.apify_ledger}
        return cls(http_client=http, rate_limiter=self.limiter, config=opts)

    async def run_cycle(self) -> dict[str, Any]:
        """Run one collection cycle. Returns a summary dict."""
        async with httpx.AsyncClient(http2=True) as http:
            collectors: list[tuple[str, BaseCollector]] = []
            for platform in self.registry.available():
                if not self._is_enabled(platform):
                    logger.info("cycle.collector_skipped", platform=platform, reason="disabled")
                    continue
                c = self._build_collector(platform, http)
                if c is None:
                    continue
                collectors.append((platform, c))

            if not collectors:
                logger.warning("cycle.no_collectors_enabled")
                return {"summary": "no collectors enabled", "runs": []}

            # Run all collectors in parallel
            started = datetime.now(tz=timezone.utc)
            coros = [self._run_one(platform, c) for platform, c in collectors]
            results = await asyncio.gather(*coros, return_exceptions=True)
            finished = datetime.now(tz=timezone.utc)

            # Persist & score
            all_trends: list[Trend] = []
            runs: list[dict] = []
            for (platform, _c), result in zip(collectors, results):
                if isinstance(result, Exception):
                    self.storage.record_run(
                        platform=platform,
                        started_at=started,
                        finished_at=finished,
                        status="error",
                        items_collected=0,
                        error=str(result),
                    )
                    runs.append({"platform": platform, "status": "error", "error": str(result)})
                    logger.error("cycle.collector_failed", platform=platform, error=str(result))
                    continue
                trends, run_info = result
                self.storage.upsert_many(trends)
                self.storage.record_run(
                    platform=platform,
                    started_at=run_info["started_at"],
                    finished_at=run_info["finished_at"],
                    status=run_info["status"],
                    items_collected=len(trends),
                    error=run_info.get("error"),
                )
                runs.append({"platform": platform, **run_info, "items": len(trends)})
                all_trends.extend(trends)

            # Cross-platform grouping & top trends
            # Use semantic grouper (falls back to exact if Ollama absent)
            grouper = SemanticGrouper(
                base_url=self.config.llm.base_url,
            )
            groups = await grouper.group(all_trends)
            cross_platform_count = sum(1 for g in groups if len(g.platforms) > 1)
            logger.info(
                "cycle.completed",
                platforms=len(collectors),
                total_items=len(all_trends),
                cross_platform_groups=cross_platform_count,
                grouping_method=groups[0].grouping_method if groups else "none",
            )
            return {
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "total_items": len(all_trends),
                "cross_platform_groups": cross_platform_count,
                "runs": runs,
            }

    async def _run_one(self, platform: str, c: BaseCollector) -> tuple[list[Trend], dict]:
        started = datetime.now(tz=timezone.utc)
        try:
            trends = await c.collect()
            finished = datetime.now(tz=timezone.utc)
            return trends, {
                "started_at": started,
                "finished_at": finished,
                "status": "success" if trends else "empty",
            }
        except Exception as e:  # noqa: BLE001
            finished = datetime.now(tz=timezone.utc)
            logger.warning("cycle.collector_exception", platform=platform, error=str(e))
            return [], {
                "started_at": started,
                "finished_at": finished,
                "status": "error",
                "error": str(e),
            }

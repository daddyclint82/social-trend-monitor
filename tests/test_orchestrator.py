"""Integration smoke: run the full orchestrator with all collectors
disabled except one, mock its HTTP layer, verify storage gets written."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors.platforms.tiktok import TikTokOEmbedCollector
from src.config import load_config
from src.normalizer.schema import Trend
from src.orchestrator import Orchestrator
from src.storage.db import Storage


def test_orchestrator_writes_to_storage(tmp_path: Path):
    """End-to-end: orchestrator runs TikTok collector (mocked), persists,
    and storage.list_trends returns the result."""
    db_path = tmp_path / "test.db"
    storage = Storage(str(db_path))

    # Build a minimal config
    cfg = load_config()
    cfg.storage.db_path = str(db_path)
    cfg.collectors["tiktok_oembed"].enabled = True
    cfg.collectors["tiktok_discover"].enabled = False
    cfg.collectors["x"].enabled = False
    cfg.collectors["instagram"].enabled = False
    cfg.collectors["facebook"].enabled = False
    # New platforms (Reddit, Apify) — disable since this test only stubs TikTok
    from src.config import CollectorConfig
    cfg.collectors.setdefault("reddit", CollectorConfig(enabled=False))
    cfg.collectors.setdefault("apify", CollectorConfig(enabled=False))
    cfg.collectors["reddit"].enabled = False
    cfg.collectors["apify"].enabled = False
    # New platforms (Google Trends, YouTube) — disable for this fixture.
    # See ADR-0013. They are enabled by default in default.yaml for real runs.
    cfg.collectors.setdefault("google_trends", CollectorConfig(enabled=False))
    cfg.collectors.setdefault("youtube", CollectorConfig(enabled=False))
    cfg.collectors["google_trends"].enabled = False
    cfg.collectors["youtube"].enabled = False
    cfg.collector_options["tiktok_oembed"] = {"hashtags": ["test"]}

    # Stub the TikTok collector to skip the network
    class StubTikTok(TikTokOEmbedCollector):
        async def collect(self) -> list[Trend]:
            from src.normalizer.schema import make_trend
            return [
                make_trend(
                    platform="tiktok_oembed", name="#test", trend_type="hashtag",
                    platform_native_id="1", url="https://example.com",
                    score=100.0,
                ),
            ]

    # Monkey-patch the registry to swap in our stub
    from src.collectors import registry as registry_mod
    real_discover = registry_mod.CollectorRegistry.discover

    def stub_discover(self):
        real_discover(self)
        self._classes["tiktok_oembed"] = StubTikTok

    registry_mod.CollectorRegistry.discover = stub_discover

    try:
        orch = Orchestrator(cfg, storage)
        result = asyncio.run(orch.run_cycle())
        assert result["total_items"] >= 1
        trends = storage.list_trends(platform="tiktok_oembed")
        assert any(t["name"] == "#test" for t in trends)
    finally:
        registry_mod.CollectorRegistry.discover = real_discover
        storage.close()

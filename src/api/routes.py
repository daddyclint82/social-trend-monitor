"""FastAPI read-only API for the Social Trend Monitor.

Endpoints:
    GET /healthz                  — health check
    GET /api/trends               — list trends (filter by platform, min_score, limit)
    GET /api/trends/{platform}    — trends for one platform
    GET /api/trends/{trend_id}    — single trend with snapshot history
    GET /api/groups               — semantic cross-platform groups
    GET /api/runs                 — recent collection runs

Run:
    uvicorn src.api.routes:app --host 127.0.0.1 --port 8090
"""
from __future__ import annotations

import structlog
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from ..config import load_config
from ..normalizer.schema import PLATFORMS
from ..normalizer.semantic import SemanticGrouper
from ..storage.db import Storage

logger = structlog.get_logger(__name__)

app = FastAPI(
    title="Social Trend Monitor",
    description="Multi-platform trending content discovery — read API",
    version="0.2.0",
)

# Lazy globals — initialized on startup
_storage: Storage | None = None
_grouper: SemanticGrouper | None = None


def _get_storage() -> Storage:
    global _storage
    if _storage is None:
        config = load_config()
        _storage = Storage(config.storage.db_path)
    return _storage


def _get_grouper() -> SemanticGrouper:
    global _grouper
    if _grouper is None:
        config = load_config()
        _grouper = SemanticGrouper(
            base_url=config.llm.base_url,
        )
    return _grouper


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _storage
    if _storage is not None:
        _storage.close()
        _storage = None


# ---- endpoints ----


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Health check: storage status + last collection runs."""
    storage = _get_storage()
    return storage.health()


@app.get("/api/trends")
async def list_trends(
    platform: str | None = Query(None, description="Filter by platform"),
    limit: int = Query(50, ge=1, le=500),
    min_score: float | None = Query(None, ge=0),
) -> list[dict[str, Any]]:
    """List current trends, optionally filtered by platform and/or minimum score."""
    if platform and platform not in PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown platform '{platform}'. Valid: {list(PLATFORMS)}",
        )
    storage = _get_storage()
    return storage.list_trends(platform=platform, limit=limit, min_score=min_score)


@app.get("/api/trends/{platform}")
async def list_trends_by_platform(
    platform: str,
    limit: int = Query(50, ge=1, le=500),
    min_score: float | None = Query(None, ge=0),
) -> list[dict[str, Any]]:
    """List trends for a specific platform."""
    if platform not in PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown platform '{platform}'. Valid: {list(PLATFORMS)}",
        )
    storage = _get_storage()
    return storage.list_trends(platform=platform, limit=limit, min_score=min_score)


@app.get("/api/trend/{trend_id}")
async def get_trend(trend_id: str) -> dict[str, Any]:
    """Get a single trend with its snapshot history."""
    storage = _get_storage()
    trend = storage.get_trend(trend_id)
    if not trend:
        raise HTTPException(status_code=404, detail="Trend not found")
    snapshots = storage.get_snapshots(trend_id, limit=100)
    trend["snapshots"] = snapshots
    return trend


@app.get("/api/groups")
async def get_groups(
    threshold: float = Query(0.75, ge=0.0, le=1.0),
    platform: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Get semantic cross-platform trend groups.

    Uses Ollama embeddings if available, falls back to exact name match.
    """
    storage = _get_storage()
    # Load trends as Trend objects (reconstruct from DB rows)
    from ..normalizer.schema import Trend, TrendSignal
    from datetime import datetime

    rows = storage.list_trends(platform=platform, limit=limit)
    trends: list[Trend] = []
    for row in rows:
        try:
            t = Trend(
                id=row["id"],
                platform=row["platform"],
                name=row["name"],
                trend_type=row["trend_type"],
                url=row.get("url"),
                first_seen=datetime.fromisoformat(row["first_seen"]),
                last_seen=datetime.fromisoformat(row["last_seen"]),
                score=row.get("latest_score") or 0.0,
            )
            trends.append(t)
        except Exception as e:
            logger.debug("api.group_skip", trend_id=row.get("id"), error=str(e))
            continue

    grouper = _get_grouper()
    groups = await grouper.group(trends, threshold=threshold)
    return [g.to_dict() for g in groups]


@app.get("/api/runs")
async def list_runs(
    platform: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """List recent collection runs (audit trail)."""
    storage = _get_storage()
    with storage.cursor() as cur:
        if platform:
            cur.execute(
                """SELECT * FROM collection_runs
                   WHERE platform = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (platform, limit),
            )
        else:
            cur.execute(
                """SELECT * FROM collection_runs
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            )
        return [dict(row) for row in cur.fetchall()]
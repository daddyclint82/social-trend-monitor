"""CLI entrypoint for social-trend-monitor.

Usage:
    python -m social_trend_monitor.cli collect           # one cycle
    python -m social_trend_monitor.cli serve             # loop
    python -m social_trend_monitor.cli list [--platform PLATFORM]
    python -m social_trend_monitor.cli health
    python -m social_trend_monitor.cli inspect --platform PLATFORM
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import structlog
import sys
from datetime import datetime, timezone

from .config import load_config
from .normalizer.schema import Trend
from .orchestrator import Orchestrator
from .storage.db import Storage

logger = structlog.get_logger(__name__)


def _setup_logging(level: str, json: bool) -> None:
    import structlog

    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,  # reset any prior handlers
    )
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )


def _make_orchestrator() -> Orchestrator:
    config = load_config()
    _setup_logging(config.logging.level, config.logging.json)
    storage = Storage(config.storage.db_path)
    return Orchestrator(config=config, storage=storage), config, storage


async def _cmd_collect(args: argparse.Namespace) -> int:
    orch, _cfg, storage = _make_orchestrator()
    try:
        result = await orch.run_cycle()
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        storage.close()


async def _cmd_serve(args: argparse.Namespace) -> int:
    orch, cfg, storage = _make_orchestrator()
    interval = max(60, args.interval) if args.interval else cfg.collectors.get("tiktok", type("X", (), {"poll_interval_min": 900})()).poll_interval_min
    try:
        while True:
            started = datetime.now(tz=timezone.utc)
            result = await orch.run_cycle()
            logger.info("serve.cycle_done", **result)
            elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
            wait = max(0, interval - elapsed)
            logger.info("serve.sleeping", seconds=wait)
            await asyncio.sleep(wait)
    finally:
        storage.close()


def _cmd_list(args: argparse.Namespace) -> int:
    _orch, cfg, storage = _make_orchestrator()
    try:
        trends = storage.list_trends(
            platform=args.platform,
            trend_type=getattr(args, "trend_type", None),
            limit=args.limit,
            min_score=args.min_score,
        )
        if args.json:
            print(json.dumps(trends, indent=2, default=str))
        else:
            for t in trends:
                rank_str = f"  rank={t.get('latest_rank')}" if t.get("latest_rank") else ""
                print(
                    f"[{t['platform']:14s}/{t.get('trend_type', '?'):8s}] {t['name'][:50]:50s} score={t.get('latest_score', 0):>12.1f}{rank_str}  ({t['last_seen']})"
                )
        return 0
    finally:
        storage.close()


def _cmd_health(args: argparse.Namespace) -> int:
    _orch, _cfg, storage = _make_orchestrator()
    try:
        h = storage.health()
        print(json.dumps(h, indent=2, default=str))
        return 0
    finally:
        storage.close()


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Debug: hit one platform's collector once and show the raw result."""
    import httpx

    from .collectors.registry import CollectorRegistry
    from .utils.rate_limit import RateLimiter

    _orch, cfg, storage = _make_orchestrator()
    try:
        registry = CollectorRegistry()
        registry.discover()
        cls = registry.get(args.platform)
        if cls is None:
            print(f"Unknown platform: {args.platform}", file=sys.stderr)
            return 2
        opts = cfg.collector_options.get(args.platform, {})
        async def go() -> list[Trend]:
            async with httpx.AsyncClient(http2=True) as http:
                limiter = RateLimiter(
                    default_rate=cfg.rate_limits.default.rate,
                    default_burst=cfg.rate_limits.default.burst or 5,
                    jitter_pct=cfg.rate_limits.jitter_pct,
                )
                for host, hl in cfg.rate_limits.per_host.items():
                    limiter.set_host_rate(host, rate=hl.rate, burst=hl.burst)
                c = cls(http_client=http, rate_limiter=limiter, config=opts)
                return await c.collect()
        trends = asyncio.run(go())
        print(f"Platform: {args.platform}  Items: {len(trends)}")
        for t in trends[:10]:
            print(f"  - {t.name}  score={t.score}  url={t.url}")
        return 0
    finally:
        storage.close()


def _cmd_serve_api(args: argparse.Namespace) -> int:
    """Start the FastAPI read API."""
    import uvicorn

    cfg = load_config()
    _setup_logging(cfg.logging.level, cfg.logging.json)
    uvicorn.run(
        "src.api.routes:app",
        host=args.host,
        port=args.port,
        log_level=cfg.logging.level.lower(),
    )
    return 0


async def _cmd_llm_formats(args: argparse.Namespace) -> int:
    """Extract content formats for current trends using local LLM."""
    from .llm.extractor import LLMFormatExtractor

    _orch, cfg, storage = _make_orchestrator()
    try:
        extractor = LLMFormatExtractor(
            base_url=cfg.llm.base_url,
            model=cfg.llm.model,
        )
        if not await extractor.is_available():
            print(
                f"Ollama not available at {cfg.llm.base_url} "
                f"(model: {cfg.llm.model}). Start it with: ollama serve && ollama pull {cfg.llm.model}"
            )
            return 1

        # Get current trends (with optional filters)
        trends = storage.list_trends(
            platform=getattr(args, "platform", None),
            trend_type=getattr(args, "trend_type", None),
            limit=args.limit,
        )
        if not trends:
            print("No trends in storage. Run 'collect' first.")
            return 0

        items = []
        for t in trends:
            trend_type = t.get("trend_type", "hashtag")
            # Build the per-type context dict for prompt construction
            # (ADR-0013: search and video prompts need extra fields)
            meta = t.get("metadata") or {}
            context: dict = {}
            if trend_type == "search":
                # Google Trends: region + pub_date for the prompt
                context["region"] = meta.get("geo", "")
                context["pub_date"] = meta.get("pub_date", "")
            elif trend_type == "video":
                # YouTube: channel + category + views + pub_date
                context["channel"] = meta.get("channel", "")
                context["category"] = meta.get("category_id", "")
                context["region"] = meta.get("region", "")
                context["views"] = meta.get("view_count", "")
                context["pub_date"] = meta.get("published_at", "")
            # hashtag + sound: no extra context, just post_descriptions below

            # For Google Trends 'search' trends, the news headlines go in
            # post_descriptions. For others, we don't have descriptions yet.
            descriptions: list[str] = []
            if trend_type == "search":
                descriptions = list(meta.get("news_titles") or [])

            items.append(
                {
                    "trend_id": t["id"],
                    "platform": t["platform"],
                    "name": t["name"],
                    "trend_type": trend_type,
                    "post_descriptions": descriptions,
                    "context": context,
                }
            )
        results = await extractor.extract_batch(items)
        for r in results:
            label = f"[{r.platform}/{r.trend_type}]"
            print(f"{label} {r.hashtag}")
            print(f"  Format: {r.format_summary}")
            if r.patterns:
                print(f"  Patterns: {r.patterns}")
            if r.why_it_works:
                print(f"  Why: {r.why_it_works}")
            print()
        return 0
    finally:
        storage.close()


def _cmd_groups(args: argparse.Namespace) -> int:
    """Show cross-platform semantic groups."""
    import asyncio as _aio
    from .normalizer.semantic import SemanticGrouper
    from .normalizer.schema import Trend
    from datetime import datetime

    _orch, cfg, storage = _make_orchestrator()
    try:
        rows = storage.list_trends(limit=args.limit)
        trends = []
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
            except Exception:
                continue

        grouper = SemanticGrouper(base_url=cfg.llm.base_url)
        groups = _aio.run(grouper.group(trends, threshold=args.threshold))
        print(f"{len(groups)} groups from {len(trends)} trends:")
        for g in groups:
            platforms_str = ", ".join(sorted(g.platforms))
            print(
                f"  [{platforms_str}] {g.canonical_name}  "
                f"({len(g.members)} members, method={g.grouping_method}, "
                f"sim={g.similarity_score:.2f})"
            )
        return 0
    finally:
        storage.close()


def _cmd_hot(args: argparse.Namespace) -> int:
    """Show what's hot RIGHT NOW: top cross-platform trend groups.

    Phase 0.3 (Trend Aggregator Improvement Ledger) — the morning deliverable.
    Combines:
    - per-platform score normalization (median + MAD z-score → [0, 1])
    - lightweight cross-platform grouper (difflib SequenceMatcher)
    - a single ranked list you can act on

    Output (default text mode):
        🔥 12 cross-platform groups from 74 trends
        1. Bryan Cranston       │ 6.0/1.0 │ 5 members │ google_trends(4) + tiktok(1)
        2. Taylor Swift         │ 5.4/1.0 │ 3 members │ google_trends(2) + youtube(1)
        ...

    Flags:
    --hours N    only consider trends seen in the last N hours (default 168 = 1 week)
    --limit N    max groups to show (default 20)
    --threshold  group similarity threshold (default 0.85)
    --json       output structured JSON for tooling
    """
    from datetime import datetime, timedelta, timezone
    from .normalizer.schema import Trend
    from .normalizer.lightweight_group import LightweightGrouper
    from .scoring.normalize import (
        compute_platform_stats,
        normalize_trends,
    )

    _orch, cfg, storage = _make_orchestrator()
    try:
        # Pull a generous window of recent trends (we'll filter by hours below)
        rows = storage.list_trends(limit=max(500, args.limit * 50))
        now = datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(hours=args.hours)

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
            except Exception:
                continue
            if t.last_seen < cutoff:
                continue
            trends.append(t)

        if not trends:
            print(f"No trends in the last {args.hours}h. Run 'collect' or wait for overnight loop.")
            return 0

        # Step 1: per-platform stats from this batch
        stats = compute_platform_stats(trends)

        # Step 2: normalize scores
        normalized = normalize_trends(trends, stats=stats)

        # Step 3: group cross-platform
        grouper = LightweightGrouper(threshold=args.threshold)
        groups = grouper.group(trends)

        # Step 4: rank groups by aggregate normalized score (sum of member normalized scores)
        # plus a small bonus for cross-platform count (multi-platform = more real)
        norm_by_id = {id(n["trend"]): n for n in normalized}
        ranked = []
        for g in groups:
            agg_norm = 0.0
            peak_norm = 0.0
            for m in g.members:
                n = norm_by_id.get(id(m))
                if n:
                    agg_norm += n["normalized_score"]
                    peak_norm = max(peak_norm, n["normalized_score"])
            # Cross-platform bonus: +0.1 per additional platform beyond the first
            cp_bonus = 0.1 * max(0, len(g.platforms) - 1)
            hot_score = agg_norm + cp_bonus
            ranked.append({
                "canonical_name": g.canonical_name,
                "member_count": g.member_count,
                "platforms": sorted(g.platforms),
                "platform_breakdown": {
                    p: sum(1 for m in g.members if m.platform == p)
                    for p in g.platforms
                },
                "hot_score": round(hot_score, 3),
                "peak_normalized": round(peak_norm, 3),
                "members": [
                    {
                        "platform": m.platform,
                        "name": m.name,
                        "normalized": round(norm_by_id.get(id(m), {}).get("normalized_score", 0), 3),
                        "raw_score": m.score,
                    }
                    for m in g.members
                ],
            })
        ranked.sort(key=lambda x: x["hot_score"], reverse=True)
        ranked = ranked[:args.limit]

        if args.json:
            print(json.dumps({
                "window_hours": args.hours,
                "trend_count": len(trends),
                "group_count": len(ranked),
                "platform_stats": {p: {k: round(v, 3) for k, v in s.items()}
                                    for p, s in stats.items()},
                "groups": ranked,
            }, indent=2, default=str))
            return 0

        # Pretty text output
        print(f"🔥 {len(ranked)} cross-platform groups from {len(trends)} trends "
              f"(last {args.hours}h)")
        print()
        for i, g in enumerate(ranked, start=1):
            breakdown = " + ".join(
                f"{p}({n})" for p, n in g["platform_breakdown"].items()
            )
            print(
                f"  {i:2}. {g['canonical_name'][:40]:40s} │ "
                f"hot={g['hot_score']:.2f} │ peak_norm={g['peak_normalized']:.2f} │ "
                f"{g['member_count']} members │ {breakdown}"
            )
        return 0
    finally:
        storage.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="social-trend-monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect", help="Run one collection cycle")
    p_collect.set_defaults(func=_cmd_collect, is_async=True)

    p_serve = sub.add_parser("serve", help="Run the orchestrator loop")
    p_serve.add_argument("--interval", type=int, default=0, help="Override poll interval (seconds)")
    p_serve.set_defaults(func=_cmd_serve, is_async=True)

    p_list = sub.add_parser("list", help="List current trends")
    p_list.add_argument(
        "--platform",
        choices=(
            "tiktok", "x", "instagram", "facebook",
            "google_trends", "youtube", "reddit", "apify",
        ),
    )
    p_list.add_argument(
        "--trend-type",
        choices=("hashtag", "sound", "search", "video", "topic", "format",
                 "creator", "subreddit", "post"),
    )
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--min-score", type=float, default=None)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list, is_async=False)

    p_health = sub.add_parser("health", help="Show storage health and last runs")
    p_health.set_defaults(func=_cmd_health, is_async=False)

    p_inspect = sub.add_parser("inspect", help="Hit one platform once and show raw results")
    p_inspect.add_argument("--platform", required=True, choices=("tiktok", "x", "instagram", "facebook"))
    p_inspect.set_defaults(func=_cmd_inspect, is_async=False)

    p_api = sub.add_parser("serve-api", help="Start the FastAPI read API")
    p_api.add_argument("--host", default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8090)
    p_api.set_defaults(func=_cmd_serve_api, is_async=False)

    p_llm = sub.add_parser("llm-formats", help="Extract content formats via local LLM")
    p_llm.add_argument("--limit", type=int, default=20)
    p_llm.add_argument(
        "--trend-type",
        choices=("hashtag", "sound", "search", "video"),
        help="Filter trends to a specific type (ADR-0013). Default: all.",
    )
    p_llm.add_argument(
        "--platform",
        choices=(
            "tiktok", "x", "instagram", "facebook",
            "google_trends", "youtube", "reddit", "apify",
        ),
        help="Filter trends to a specific platform. Default: all.",
    )
    p_llm.set_defaults(func=_cmd_llm_formats, is_async=True)

    p_groups = sub.add_parser("groups", help="Show cross-platform semantic groups")
    p_groups.add_argument("--threshold", type=float, default=0.75)
    p_groups.add_argument("--limit", type=int, default=100)
    p_groups.set_defaults(func=_cmd_groups, is_async=False)

    p_hot = sub.add_parser(
        "hot",
        help="Top cross-platform trends RIGHT NOW (the morning deliverable).",
    )
    p_hot.add_argument(
        "--hours", type=int, default=168,
        help="Only trends seen in the last N hours. Default 168 = 1 week.",
    )
    p_hot.add_argument("--limit", type=int, default=20, help="Max groups to show.")
    p_hot.add_argument(
        "--threshold", type=float, default=0.85,
        help="Cross-platform grouping similarity threshold (0..1).",
    )
    p_hot.add_argument("--json", action="store_true", help="Output structured JSON.")
    p_hot.set_defaults(func=_cmd_hot, is_async=False)

    args = parser.parse_args(argv)
    if getattr(args, "is_async", False):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

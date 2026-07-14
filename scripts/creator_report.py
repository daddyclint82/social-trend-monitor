#!/usr/bin/env python3
"""Creator-friendly trend report.

Pulls the top N trending entities from the last week, runs LLM format
extraction on each (Ollama, with graceful degradation), and prints a
report tailored for content creators.

Usage:
    venv/bin/python scripts/creator_report.py             # top 5, 7-day window
    venv/bin/python scripts/creator_report.py --limit 10  # top 10
    venv/bin/python scripts/creator_report.py --hours 48  # 2-day window

The Ollama URL is read from config.llm.base_url; override via:
    STM_LLM_BASE_URL=http://192.168.1.50:11434 venv/bin/python scripts/creator_report.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make src importable when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.llm.extractor import LLMFormatExtractor


def get_top_trends(db_path: str, hours: int, limit: int) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = con.execute("""
        SELECT t.platform, t.name, t.trend_type, t.url, t.id as trend_id,
               t.metadata_json, t.first_seen, t.last_seen, t.latest_score
        FROM trends t
        WHERE t.last_seen >= ?
        ORDER BY t.last_seen DESC
    """, (cutoff,)).fetchall()

    # Dedupe by normalized name; prefer tiktok_discover > tiktok_oembed
    by_name: dict[str, list] = defaultdict(list)
    for r in rows:
        n = r['name'].lower().strip()
        if len(n) < 3:
            continue
        by_name[n].append(dict(r))

    def score(r):
        plat = 2 if r['platform'] == 'tiktok_discover' else (1 if 'tiktok' in r['platform'] else 0)
        cross = len({m['platform'] for m in by_name[r['name'].lower().strip()]})
        return (plat, cross, r['last_seen'])

    candidates = [max(members, key=score) for members in by_name.values()]
    candidates.sort(key=score, reverse=True)
    return candidates[:limit]


async def extract_all(extractor, trends: list[dict]) -> list[tuple[dict, object | None]]:
    results = []
    for r in trends:
        md = json.loads(r['metadata_json']) if r['metadata_json'] else {}
        ctx: dict = {'region': md.get('geo', md.get('region', 'us'))}
        if r['trend_type'] == 'search':
            ctx['pub_date'] = md.get('pub_date', r['first_seen'])
            posts = md.get('news_titles', [])[:5]  # feed news headlines
        elif r['trend_type'] == 'video':
            ctx.update({
                'channel': md.get('channel', ''),
                'category': md.get('category', ''),
                'views': md.get('views', ''),
                'pub_date': md.get('pub_date', r['first_seen']),
            })
            posts = []
        else:
            posts = []

        print(f"  [analyzing] {r['name']}...", file=sys.stderr, flush=True)
        try:
            res = await extractor.extract(
                trend_id=r['trend_id'],
                platform=r['platform'],
                name=r['name'],
                trend_type=r['trend_type'],
                post_descriptions=posts,
                context=ctx,
            )
            results.append((r, res))
            print(f"  [done]      {res.format_summary[:70]}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  [FAIL]      {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            results.append((r, None))
    return results


def main() -> int:
    p = argparse.ArgumentParser(description="Creator-friendly trend report")
    p.add_argument("--limit", type=int, default=5, help="Top N trends to extract")
    p.add_argument("--hours", type=int, default=168, help="Lookback window in hours")
    p.add_argument("--db", default="data/trends.db", help="SQLite DB path")
    p.add_argument("--no-llm", action="store_true", help="Skip LLM extraction (faster)")
    args = p.parse_args()

    cfg = load_config()
    if os.environ.get("STM_LLM_BASE_URL"):
        cfg.llm.base_url = os.environ["STM_LLM_BASE_URL"]
    print(f"📊 Pulling top {args.limit} trends from last {args.hours}h...")
    trends = get_top_trends(args.db, args.hours, args.limit)
    print(f"   Found {len(trends)} candidates.\n")

    results: list[tuple[dict, object | None]] = [(t, None) for t in trends]

    if not args.no_llm:
        extractor = LLMFormatExtractor(
            base_url=cfg.llm.base_url,
            model=cfg.llm.model,
            timeout_s=120.0,
        )
        print(f"🤖 Connecting to Ollama at {cfg.llm.base_url} (model: {cfg.llm.model})")
        if not asyncio.run(extractor.is_available()):
            print("   ⚠️  Ollama not reachable — running without LLM extraction.\n")
        else:
            print("   ✅ Connected.\n")
            results = asyncio.run(extract_all(extractor, trends))

    print()
    print("=" * 80)
    print("🔥 TRENDING THIS WEEK — Creator Report")
    print("=" * 80)
    for i, (r, res) in enumerate(results, 1):
        print(f"\n{i}. {r['name']}  [{r['platform']} / {r['trend_type']}]")
        print(f"   🔗 {r['url']}")
        if res is not None:
            print(f"   📐 FORMAT:        {res.format_summary}")
            if res.patterns:
                print(f"   🎯 PATTERNS:      {res.patterns}")
            if res.why_it_works:
                print(f"   💡 WHY IT WORKS:  {res.why_it_works}")
        else:
            print(f"   (LLM extraction skipped)")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

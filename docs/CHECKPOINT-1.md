# Session Checkpoint #1 — Social Trend Monitor
**Time:** 2026-07-13 16:50–22:05 CDT (~5h 15m elapsed)
**Project:** Social Trend Monitor (multi-platform trending content discovery)

## Phase 1: Research (DONE)

Confirmed discovery surfaces per platform:

| Platform | Primary Discovery | Auth | Confirmed via |
|----------|-------------------|------|---------------|
| TikTok | ~~Creative Center~~ → oEmbed only (Pumbaa gates JSON) | None | Live probe — direct API returns pumbaa-rule HTML |
| X | `GET /2/trends/by/woeid/:woeid` | Bearer (paid) | Official X API v2 docs |
| Instagram | oEmbed only — no discovery | None | Tier 1 of platform research |
| Facebook | Graph API for your own pages | Page tokens | Meta Graph API docs |

**Critical finding (mid-session):** TikTok's internal JSON API is
gated by the Pumbaa anti-bot system. Direct httpx POST returns HTML,
not JSON, with `sampleRate: 0.07`. This violates our ethics posture
if we try to bypass it. **Pivoted:** TikTok v1 collector now uses
oEmbed + user-supplied hashtag lists. v2 will integrate the official
Research API.

## Phase 2: Architecture (DONE)

10 ADRs locked in `docs/architecture/decisions.md`:

- **ADR-0001** Python 3.11+
- **ADR-0002** TikTok (revised same-day) — oEmbed + user list, not Creative Center scraping
- **ADR-0003** X API v2 trends endpoint
- **ADR-0004** Strict ethical collection guardrails
- **ADR-0005** Single Trend schema (collector returns `list[Trend]`)
- **ADR-0006** SQLite (stdlib, no SQLAlchemy)
- **ADR-0007** Per-domain token-bucket rate limiter
- **ADR-0008** Auto-discovery collector registry
- **ADR-0009** CLI first, FastAPI second
- **ADR-0010** Local LLM (Ollama) as optional format extractor

## Phase 3: Implementation (DONE)

**Code shipped (v0.1):**
- `src/collectors/base.py` — BaseCollector ABC with rate-limited `get_json` helper
- `src/collectors/registry.py` — auto-discovery via `pkgutil.iter_modules`
- `src/collectors/platforms/tiktok.py` — oEmbed + user hashtags
- `src/collectors/platforms/x.py` — X API v2 trends (6 WOEIDs default)
- `src/collectors/platforms/instagram.py` — oEmbed for user URLs
- `src/collectors/platforms/facebook.py` — Graph API for page posts
- `src/normalizer/schema.py` — Trend, TrendSignal, `make_trend`, `make_cross_platform_key`
- `src/scoring/engine.py` — velocity (tanh), cross-platform bonus, exponential decay
- `src/storage/db.py` — SQLite with 3 tables: trends, trend_snapshots, collection_runs
- `src/utils/rate_limit.py` — token-bucket per host with jitter + Retry-After
- `src/orchestrator.py` — async cycle runner with `asyncio.gather` + run audit
- `src/config.py` — pydantic + YAML loader
- `src/cli.py` — `collect | serve | list | health | inspect` subcommands

**Tests: 46/46 passing in 1.21s** (no real network)

## Phase 4: Live Verification (DONE)

End-to-end CLI run:
```
$ python -m src.cli collect
{"platforms": 2, "total_items": 5, "cross_platform_groups": 0, "event": "cycle.completed"}
$ python -m src.cli list
[tiktok] #aiart
[tiktok] #booktok
[tiktok] #fyp
[tiktok] #foryou
[tiktok] #learnontiktok
$ python -m src.cli health
{"tiktok": {"status": "success", "last_started": "..."}, "x": {"status": "empty", ...}}
```

TikTok collected 5 user-supplied hashtags. X correctly skipped (no token).
Facebook and Instagram correctly skipped (disabled in config).

## Key Decisions Made This Session

1. **Pivoted TikTok strategy mid-session.** Original plan was Creative
   Center JSON API. Pumbaa anti-bot blocked it. New v1 strategy:
   oEmbed + user-supplied hashtags. Honest, ethical, limited.
2. **Async-first throughout.** Even the SQLite writes go through a
   single connection (we're I/O-bound on the network, not on the DB).
3. **structlog over stdlib logging.** Stdlib's `logger.warning("event",
   kwarg=val)` style doesn't work; structlog's `BoundLogger` does.
   Tests use `tests/conftest.py` to bootstrap structlog before any
   module imports.
4. **SQLite nonces for snapshot uniqueness.** Two upserts in the same
   microsecond would collide on `(trend_id, captured_at)` PK. Added
   `_next_snap_nonce()` to break ties with microsecond offsets.
5. **`parents[1]`, not `parents[2]`, for project root.** Initial bug:
   `src/config.py` resolved to `workspace/config/default.yaml`
   instead of `social-trend-monitor/config/default.yaml`.

## Open Questions for v2

1. **TikTok Research API integration** — apply for access, swap in
   new collector class. Same `list[Trend]` contract. Low effort.
2. **Cross-platform semantic grouping** — current is exact match +
   Levenshtein. Embedding-based grouping (Ollama embeddings?) for
   non-obvious matches like "Taylor Swift" = "T-Swift" = "TSwift".
3. **FastAPI read API** — sketch is in `architecture/overview.md`,
   not implemented. CLI is sufficient for v1.
4. **LLM format extraction** — ADR-0010 plans it, not implemented.
   Add a `src/llm/extractor.py` that takes 3–10 top posts from a
   trending hashtag and returns a format summary.
5. **Snapshot retention/TTL** — schema supports it (storage config
   has `retention_days`), but no purge job exists. Add to
   `src/storage/purge.py` + APScheduler.
6. **Multi-tenant / auth** — single-user for v1. Add when needed.
7. **Webhooks on trending threshold** — mentioned in architecture
   doc, not implemented. Add when v1 ships.

## Artifacts Created This Session

- `social-trend-monitor/` — full project, 1.5K LOC + 46 tests
- 1 entity in MEMORY.md: Social Trend Monitor status
- 1 ADR revised: ADR-0002 (TikTok same-day pivot)
- 1 daily log entry (this file)

## Process Notes

- **Batch then verify pattern** worked: wrote all docs first, then
  scaffolded code, then ran tests, then fixed 4 failures in one pass.
- **The TikTok live probe** was the most valuable action of the
  session — it prevented weeks of work on a gated API.
- **Musk step 1 in action:** "make the requirements less dumb" by
  actually probing what the platform will give us before building
  to a spec.

## Next Phase (after checkpoint, if session continues)

If continuing:
- Add LLM format extraction (ADR-0010)
- Add snapshot retention/TTL job
- Add FastAPI read API
- Improve cross-platform semantic grouping
- Write the v2 plan: TikTok Research API integration
- Add cross-platform trend detection (one trend spotted on TikTok
  that hasn't hit X yet, vs. one that has)

## Status

**v0.1 alpha complete.** All planned v1 features shipped. Tests pass.
Live CLI works. Ethics posture honored. TikTok strategy honestly
documented as limited.

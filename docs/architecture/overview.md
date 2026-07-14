---
title: Social Trend Monitor — Architecture Overview
date: 2026-07-13
tags: [project, social-trend-monitor, architecture, overview]
status: current
---

# Social Trend Monitor — Architecture Overview

## System Shape

```
┌─────────────────────────────────────────────────────────────┐
│                  ORCHESTRATOR (CLI / APScheduler)           │
│  - Runs every N minutes                                    │
│  - Asks Registry: "give me all collectors"                 │
│  - Runs them in parallel, each rate-limited per-domain     │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────┐
│                    COLLECTOR REGISTRY                       │
│  - Auto-discovers BaseCollector subclasses                 │
│  - Instantiates per-platform collectors                    │
│  - Each collector: httpx client + RateLimiter              │
└──┬──────────┬──────────┬──────────┬────────────────────────┘
   │          │          │          │
   ▼          ▼          ▼          ▼
┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐
│TikTok│  │  X   │  │Insta │  │  FB  │
│ CC   │  │ API  │  │OEMbed│  │Graph │  ← Async, rate-limited
└──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘
   │         │         │         │
   │ each returns list[Trend] (unified schema)
   │         │         │         │
   └────┬────┴────┬────┴────┬────┘
        ▼         ▼         ▼
┌─────────────────────────────────────────────────────────────┐
│                     NORMALIZER                              │
│  - Cross-platform dedupe (Levenshtein + name normalize)    │
│  - Compute cross_platform_key                              │
│  - Detect format clusters (LLM-assisted, opt-in)           │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│                       SCORER                                │
│  - Velocity: rate-of-change vs last 7 days                  │
│  - Cross-platform bonus: present on N platforms?            │
│  - Decay: trends older than 7d lose score                   │
│  - Output: ranked list[Trend]                                │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│                     STORAGE (SQLite)                        │
│  - trends: current state per trend                          │
│  - trend_snapshots: time-series for velocity calc           │
│  - collection_runs: per-cycle audit log                     │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│                       READERS                               │
│  - CLI: query / export / inspect                           │
│  - FastAPI: GET /trends /trends/{platform} /trends/{id}     │
│  - Optional: Webhook on "trending above threshold"          │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow (one cycle)

1. **T=0s** — Orchestrator decides to run a cycle (scheduled or manual)
2. **T=0.1s** — Registry returns active collectors based on config
3. **T=0.2s** — All collectors start in parallel (asyncio.gather)
4. **T=0.5s..120s** — Each collector:
   - Acquires per-domain rate-limit token
   - Makes HTTP request(s) via shared httpx client
   - Parses response, normalizes to `Trend[]`
   - Logs result count, latency, status
5. **T=cycle_end** — All collectors return, orchestrator:
   - Deduplicates via cross-platform join
   - Scores and ranks
   - Writes snapshots to SQLite
   - Updates current `trends` table (UPSERT)
   - Logs collection run status
6. **T=cycle_end+1s** — CLI/API readers can query latest data

## Module Boundaries

| Layer | Module | Knows About | Does NOT Know About |
|-------|--------|-------------|---------------------|
| **Collection** | `src/collectors/platforms/*` | HTTP, platform API, parsing | Storage, scoring, other platforms |
| **Normalization** | `src/normalizer/*` | Trend schema, dedupe logic | HTTP, storage |
| **Scoring** | `src/scoring/*` | Trend schema, history queries | HTTP, parsing |
| **Storage** | `src/storage/*` | SQL, Trend schema | HTTP, parsing |
| **Read API** | `src/api/*` | Storage, Trend schema | Collection, scoring internals |
| **Orchestration** | `src/orchestrator.py` | Registry, all layers | Implementation details |

**Rule:** Strict downward dependencies. Storage can read Trend; storage
cannot import collectors.

## Async Model

- One `asyncio` event loop per process
- Collectors are async coroutines; run concurrently with `asyncio.gather`
- Single shared `httpx.AsyncClient` (HTTP/2 connection pooling)
- RateLimiter is a per-domain `asyncio.Lock` + `asyncio.Event`
- SQLite writes serialized through a single connection + lock (or use
  `aiosqlite` if concurrency becomes an issue)

## Configuration

```yaml
# config/default.yaml
collectors:
  tiktok:
    enabled: true
    region: US
    industries: [general, tech_and_gaming]
    poll_interval_min: 15
  x:
    enabled: true
    woeids: [1, 23424977, 23424975]
    bearer_token_env: X_BEARER_TOKEN
    poll_interval_min: 15
  instagram:
    enabled: false  # disabled by default in v1
    oembed_urls: [] # user-supplied
  facebook:
    enabled: false
    page_tokens_env: FB_PAGE_TOKENS  # JSON map page_id -> token

rate_limits:
  default:
    requests_per_second: 0.2  # 1 req per 5s
    burst: 5
    jitter_pct: 0.5
  per_host:
    ads.tiktok.com: 0.1        # 1 req per 10s — extra polite
    api.x.com: 1.0             # 1 req per second — within their limit
    graph.facebook.com: 0.5    # 1 req per 2s

storage:
  db_path: ./data/trends.db
  retention_days: 60
  snapshot_interval_min: 15

llm:
  enabled: false
  base_url: http://localhost:11434  # Ollama
  model: llama3.1:8b
  format_extraction_interval_h: 6

logging:
  level: INFO
  json: true
  path: ./logs/social-trend-monitor.log
```

## Deployment Shape (v1)

- Single Python process, runs `python -m social_trend_monitor serve`
- `systemd --user` service (matches existing workspace pattern)
- SQLite file at `./data/trends.db`
- Optional FastAPI on `127.0.0.1:8090` (different port from Sophos
  Voice on 8080 to avoid conflict)
- Logs to `./logs/social-trend-monitor.log` and stdout

## Extension Points

Adding a 5th platform (e.g., YouTube Shorts, LinkedIn, Reddit):

1. Create `src/collectors/platforms/youtube.py`
2. Subclass `BaseCollector`, set `platform = "youtube"`, implement `collect()`
3. Return `list[Trend]`
4. Add config block to `config/default.yaml`
5. Done — registry auto-discovers

That's it. No central dispatch, no migrations, no schema changes needed.

## Observability

- Every HTTP request logs: method, URL, status, latency_ms, rate_limit_state
- Every collection cycle logs: started_at, finished_at, items_per_platform,
  errors
- SQLite `collection_runs` table provides an audit trail
- Health endpoint (`/healthz`) reports last successful collection per platform
- `inspect` CLI subcommand shows raw platform response for debugging

## Error Handling

- **Soft failures:** Network blip, 429 with retry-after → back off, retry
  up to 3 times, then log and skip this cycle
- **Hard failures:** Schema change (we get 200 but unrecognized JSON) →
  log raw response, mark run as `partial`, alert (log only in v1)
- **No panics:** Any unhandled exception in a collector is caught, logged,
  and that collector is marked as failed for the cycle. Other collectors
  continue.

## What This Is NOT

- Not a real-time engine (15 min cadence is the minimum v1)
- Not a publisher (we don't post anything)
- Not a competitor tracker (we don't have a database of competitors)
- Not an analytics dashboard (we report platform-native metrics, not
  derived insights like "best time to post")
- Not a trend predictor (we report what's trending *now*, we don't forecast)

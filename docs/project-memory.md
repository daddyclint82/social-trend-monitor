---
title: Social Trend Monitor — Project Memory
date: 2026-07-13
tags: [project, social-trend-monitor, content-strategy]
status: in-progress
---

# Social Trend Monitor — Project Memory

## What This Is
A safe, multi-platform **Social Trend Monitor** that identifies trending topics,
formats, and high-performing content styles across **TikTok, Instagram, X, and
Facebook** to support content repurposing and audience growth.

This is **NOT** a scraper for private user feeds. It is an **ethical discovery
tool** built on official public surfaces and, where the platform offers one, the
official public API.

## Why
- Content teams waste hours manually checking 4 apps for what's trending
- Repurposing needs cross-platform trend visibility, not just one platform
- Manual trend watching is biased, slow, and inconsistent
- Most "trend tools" are either paywalled SaaS, scrapers that get banned, or
  analytics dashboards for your *own* account (not the public conversation)

## Who It's For
- Solo creators running cross-platform content
- Small content teams that need a single source of truth
- Strategists who want to spot *patterns* (a format rising on TikTok often
  prefigures a wave on Instagram Reels 2–4 weeks later)

## Strict Guardrails (Locked)
1. **Only official/public discovery pages and ethical methods.** No login
   required, ever. No browser automation that bypasses auth walls.
2. **Never scrape private user feeds.** Public only.
3. **Rate limit aggressively.** Default 1 request per 2–5 seconds, jittered.
   Respect `Retry-After`. Never hammer endpoints.
4. **Proxy rotation** is optional and only for legitimate traffic patterns
   (residential proxies, with proper UA rotation, not datacenter botnet IPs).
5. **Modular and extensible.** Adding a new platform = drop a new collector
   module. No core rewrite.
6. **Observability built in.** Every request logged with status, latency,
   rate-limit state, and result count.
7. **Data minimization.** We collect the minimum needed for trend analysis.
   No PII, no full caption text archive, no individual user identification.

## Scope
**In scope (v1):**
- Aggregate public trending data from 4 platforms
- Normalize into a single trend schema
- Score and rank trends (velocity, cross-platform presence, format detection)
- Simple CLI + local JSON/SQLite storage
- Optional FastAPI for read access
- Optional LLM-based format/style extraction (Ollama local)

**Out of scope (v1):**
- Publishing/posting automation
- Account analytics (own-account metrics)
- Influencer databases
- Ad creative library
- Engagement-bait suggestions
- Anything that requires login

## Platforms — Data Source Strategy

| Platform | Primary Source | Secondary Source | Auth | Confidence |
|----------|----------------|------------------|------|------------|
| **TikTok** | **TikTok Creative Center** (public advertiser tool) | TikTok Research API (apply) | None / API key | HIGH |
| **Instagram** | Public hashtag explore pages + Graph API (limited) | Public Reels/Posts embed metadata | None / App token | MEDIUM |
| **X** | **X API v2** — `GET /2/trends/by/woeid` | No public alternative | Bearer token (paid tier) | HIGH |
| **Facebook** | Public pages & hashtag pages (limited) | Graph API (page-level) | None / App token | LOW-MEDIUM |

**TikTok Creative Center** (`https://ads.tiktok.com/business/creativecenter/...`)
is the anchor. It's a public advertiser tool that TikTok itself runs, exposes
trending hashtags, songs, creators, and videos — no login, no API key, no rate
limits from your account. The data is the same data advertisers pay to access.

**X API v2** trends endpoint requires a paid tier since the 2023 free-tier
kill, but `GET /2/trends/by/woeid/:woeid` is the official supported way and
returns clean, structured data.

**Instagram** is the hardest. Public hashtag pages are heavily JS-rendered and
rate-limited aggressively. Graph API access to hashtag/public content is
restricted. We will use the **official Instagram oEmbed** (public, no auth) for
specific URLs and rely on user-provided keyword lists + limited Graph API for
app-owned accounts.

**Facebook** is similar — public pages are scrape-resistant. The Meta Graph
API is the official path but requires app review for most public content
access. We will support both: a public-pages collector (low yield, polite) and
a Graph API collector (requires token).

## Tech Stack (Locked)
- **Language:** Python 3.11+
- **HTTP:** `httpx` (async-first, HTTP/2, good for rate-limited scraping)
- **HTML parsing:** `selectolax` (fast, simple) for the few static pages we hit
- **Browser automation:** `playwright` (optional, last-resort for JS-heavy pages)
- **Storage:** SQLite (default) + optional JSON dumps
- **Orchestration:** APScheduler (in-process) for v1
- **API:** FastAPI (optional, read-only)
- **LLM (optional):** Ollama local for format/style extraction
- **Config:** YAML via `pydantic-settings`
- **Logging:** `structlog` (JSON output for easy parsing)
- **Testing:** `pytest` + `pytest-asyncio` + `respx` (httpx mock)

## Module Map
```
src/
├── collectors/              # One per platform
│   ├── base.py              # Collector abstract base class
│   ├── platforms/
│   │   ├── tiktok.py        # Creative Center
│   │   ├── instagram.py     # oEmbed + public hashtag
│   │   ├── x.py             # X API v2 trends
│   │   └── facebook.py      # Graph API + public pages
│   └── registry.py          # Auto-discovery of collectors
├── normalizer/              # Platform-specific → unified Trend schema
│   ├── schema.py            # Trend, TrendSignal, TrendFormat dataclasses
│   └── pipeline.py          # Normalize + dedupe + enrich
├── scoring/                 # Velocity, cross-platform bonus, decay
│   ├── velocity.py          # Rate-of-change scoring
│   ├── cross_platform.py    # Multi-platform boost
│   └── decay.py             # Time-based ranking decay
├── storage/                 # SQLite + JSON
│   ├── db.py                # Schema + migrations
│   └── models.py            # SQLAlchemy or raw sqlite3
├── api/                     # Optional FastAPI
│   └── routes.py
├── utils/
│   ├── rate_limit.py        # Token-bucket per-domain limiter
│   ├── retry.py             # Exponential backoff with jitter
│   └── proxy.py             # Proxy rotation config
├── config.py                # Settings via pydantic
└── orchestrator.py          # Runs collection cycle
```

## Data Model (Trend Schema)
```python
@dataclass
class Trend:
    id: str                       # platform:platform_id
    platform: str                 # tiktok | instagram | x | facebook
    name: str                     # "#AIart", "Taylor Swift", "Get Ready With Me"
    trend_type: str               # hashtag | sound | topic | format | creator
    url: str
    first_seen: datetime
    last_seen: datetime
    score: float                  # platform-native score
    normalized_score: float       # 0-100, cross-platform comparable
    signals: list[TrendSignal]    # granular metrics
    metadata: dict                # platform-specific
    cross_platform_key: str       # join key for cross-platform grouping
```

## Orchestration
- **Collection cycle:** every 15 min default (configurable per platform)
- **Decay:** trends older than 7 days get score decay
- **Cross-platform join:** text similarity on normalized name + LLM-assisted
  topic grouping for non-obvious matches
- **Snapshot storage:** every cycle writes to `trends_snapshots` table for
  time-series analysis

## Status (2026-07-13 23:45)
- 🚧 **v0.3 alpha complete** — all 123 tests passing, live CLI + FastAPI verified
- Research: 4 platforms mapped (v0.1) + 2 new sources (Reddit, Apify) added v0.3
- Architecture: 12 ADRs locked (0001–0010 v0.1, 0011–0012 v0.3)
- Code: 4,800+ LOC across 40+ Python files
- TikTok: v1 limited (user-supplied hashtags only, oEmbed for metadata)
  **+ Apify bridge opt-in for discovery** (v0.3)
- X: v1 full (X API v2 trends, bearer token required)
- Instagram: v1 partial (oEmbed for user URLs) **+ Apify bridge opt-in** (v0.3)
- Facebook: v1 optional (Graph API for your own pages, no discovery)
- **Reddit (v0.3, NEW):** OAuth2 client_credentials via Reddit script app.
  Hits r/all/hot, r/popular, configurable niche subs, and trending-sub
  list. Public listings only. Disabled by default.
- **Apify (v0.3, NEW):** Vendor bridge to community Actors. Single
  collector, `platform="apify"`, trends tagged with `metadata["source_platform"]`.
  Free tier ($5/mo) gated by monthly + cycle cost guards. Disabled by default.
- **v2 additions carried over:**
  - LLM format extraction (Ollama, ADR-0010 implemented)
  - FastAPI read API on :8090
  - Semantic cross-platform grouping (Ollama embeddings + cosine similarity)
  - CLI: `groups`, `llm-formats`, `serve-api` subcommands

## Open Questions
1. Do we need real-time (< 5 min latency) or is 15 min polling fine?
2. Should the LLM format-extraction be a default-on feature or opt-in?
3. Storage retention: keep all snapshots forever or TTL (30/60/90 days)?
4. Multi-tenant: is this single-user or do we need auth from day 1?
5. Hosting: local-only, or deploy to a VPS (Render / Fly.io)?

## Next Steps (sequence)
1. ✅ Research — DONE
2. ✅ Architecture decisions doc (ADR-0001, 0002, 0003) — DONE v0.1
3. ✅ Collector base + TikTok Creative Center prototype — DONE v0.1
4. ✅ Normalizer + Trend schema — DONE v0.1, **+subreddit/post types v0.3**
5. ✅ SQLite storage + retention policy — DONE v0.1, **+apify_spend table v0.3**
6. ✅ X API v2 trends collector — DONE v0.1
7. ✅ Instagram + Facebook collectors (harder) — DONE v0.1, +Apify bridge v0.3
8. ✅ Scoring engine — DONE v0.1
9. ✅ FastAPI read API — DONE v0.1
10. ✅ Optional: LLM format extraction — DONE v0.1
11. ✅ **Reddit collector (OAuth, free)** — DONE v0.3
12. ✅ **Apify vendor bridge (opt-in, cost-guarded)** — DONE v0.3
13. ⏳ Snapshot retention/TTL job (storage has retention_days but no purge)
14. ⏳ Async run mode for Apify (heavy jobs, future)
15. ⏳ Cross-platform grouping UI in FastAPI (currently CLI only)
16. ⏳ Webhook on "trending above threshold" alert

---
*Maintained as the project's source of truth. Update on every meaningful
design change. Cross-reference from MEMORY.md and the vault entity page.*

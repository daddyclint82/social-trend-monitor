---
title: Social Trend Monitor — Architecture Decision Log
date: 2026-07-13
tags: [project, social-trend-monitor, architecture, adr]
status: current
---

# Architecture Decision Log

All significant design decisions for the Social Trend Monitor. Numbered,
dated, and immutable once accepted. New decisions append; old ones are
superseded in place (don't rewrite history).

---

## ADR-0001 — Python 3.11+ as the implementation language
**Date:** 2026-07-13
**Status:** Accepted

**Context:** Need a language with strong async HTTP support, fast iteration,
mature data libraries, and good LLM integration. Team expertise is
Python-first.

**Decision:** Python 3.11+.

**Consequences:**
- `httpx` for async HTTP, `selectolax` for parsing, `pydantic` for schemas
- 3.11+ gives us `asyncio.TaskGroup`, `tomllib`, `ExceptionGroup`, better
  error messages
- Matches the existing workspace stack (AIE ResuMaker, Praedixi, Sophos
  Voice all Python)

**Alternatives considered:**
- Node.js/TypeScript — rejected: weaker LLM ecosystem, less mature HTTP/2
- Go — rejected: too verbose for fast iteration, no LLM library maturity
- Rust — rejected: overkill for v1, slow dev velocity

---

## ADR-0002 — TikTok Creative Center as the primary TikTok data source
**Date:** 2026-07-13
**Status:** **REVISED 2026-07-13** — See "Revised strategy" below.

**Context:** TikTok is the most aggressive platform about blocking scrapers.
They have no public free API for trend data. The **TikTok Creative Center**
(`https://ads.tiktok.com/business/creativecenter/...`) is a public advertiser
tool that exposes trending hashtags, songs, creators, and videos without
login or API key. It's the same data advertisers pay for in TikTok Ads
Manager.

**Original decision:** Use the internal JSON API that powers the public React
SPA. (`/creative/api/hashtag/list/` and similar)

**Revised strategy (2026-07-13 same-day revision based on live probe):**
The internal JSON API is gated by TikTok's **Pumbaa** bot-mitigation system.
A direct `httpx` POST with browser-grade headers returns an HTML
"pumbaa-rule" page (sampled click rate 0.07), not JSON. This is not a rate
limit we can back off from; it's a fingerprint challenge.

This conflicts with our ethics posture (ADR-0004, no bypass of detection).
The honest options are:

1. **Headless browser with real interaction** (Playwright). Real browser,
   real fingerprint, polite cadence. Slower (1–2s per request minimum) but
   legitimate. May still hit Pumbaa challenges at volume.
2. **TikTok Research API** (apply for access, requires approval, free for
   academics and select commercial users). Official, supported, ethical.
3. **User-supplied URL harvesting** — the user pastes Creative Center URLs
   or hashtag names; we hit TikTok's oEmbed / public profile pages only.

**Revised decision:** For v1, ship **option 3 (user-supplied URL harvesting)**
as the TikTok collector. The collector takes a list of hashtag names and
returns a Trend record per name with metadata sourced from public oEmbed /
profile pages. Document the limitation honestly.

For v2, integrate the **TikTok Research API** (option 2) as the primary
source. The collector class is designed to swap in cleanly: replace the
HTTP path, keep the Trend[] return contract.

**Implementation strategy (revised v1):**
- User config: `collector_options.tiktok.hashtags: ["aiart", "booktok", ...]`
  OR `collector_options.tiktok.creator_urls: ["https://tiktok.com/@user", ...]`
- Use TikTok's public **oEmbed** endpoint
  (`https://www.tiktok.com/oembed?url=...`) — no auth, no anti-bot, no
  scrape. Returns metadata for a single public video/creator URL.
- Score = 0; metadata carries the public profile/video data.
- The list comes from the user (their target hashtags, their competitor
  watchlist). We don't try to *discover* what's trending.

**Consequences (revised):**
- v1 is honest about its limits: we can track *given* TikTok hashtags/
  creators, not discover *new* ones
- v2 Research API integration is well-scoped — same Trend[] contract
- Zero ban risk: oEmbed is documented and intended for third-party use
- No violation of ethics posture

**Alternatives considered (revised):**
- Direct JSON API scraping with proxy rotation — rejected: Pumbaa fingerprint
  challenge, requires CAPTCHA solving, violates guardrail 4 of ADR-0004
- Playwright with stealth plugin — rejected: stealth plugins are explicitly
  anti-detection bypasses, violates guardrail 4
- Apify / third-party data brokers — deferred: vendor dependency + cost;
  could integrate as v2 option alongside Research API

---

## ADR-0003 — X API v2 `/2/trends/by/woeid` as the only X data source
**Date:** 2026-07-13
**Status:** Accepted

**Context:** X (Twitter) killed their free API tier in 2023. Current access
is paid ($100–$5,000/mo). The official `GET /2/trends/by/woeid/:woeid`
endpoint returns the 50 trending topics for a Yahoo! Where On Earth ID
(WOEID) — a geographic location.

**Decision:** Use X API v2 trends endpoint exclusively for X data.

**Consequences:**
- Requires paid X API access — we budget for it in the project plan
- Returns clean, structured, officially supported data
- No scraper risk, no ban risk
- WOEIDs cover most regions (1 = worldwide, 23424977 = US, etc.)
- We poll the worldwide + 5–10 key region WOEIDs per cycle

**Alternatives considered:**
- Scraping `twitter.com/explore/tabs/trending` — rejected: high ban risk,
  unstable DOM, no official support
- Nitter mirrors — rejected: most are dead, X has been blocking them
- Third-party X scrapers — rejected: vendor dependency + cost

---

## ADR-0004 — Ethical collection: public data only, rate-limit by default
**Date:** 2026-07-13
**Status:** Accepted

**Context:** Scraping social platforms is a legal and ethical minefield.
User's guardrails are strict: public discovery pages only, no logins,
respectful delays, optional proxy.

**Decision:** Hard rules for the entire system:

1. **Public only.** No authenticated requests, ever. No session cookies, no
   bearer tokens from user accounts. Only first-party app credentials.
2. **Conservative rate limit by default.** 1 request per 2–5 seconds with
   ±50% jitter. Per-domain token-bucket limiter.
3. **Respect platform signals.** Honor `Retry-After` headers, `X-RateLimit-*`
   headers, robots.txt for static pages.
4. **No bypassing detection.** If a platform returns a captcha or block
   page, we stop, log, and back off for 1 hour. We do not use captcha
   solvers or residential proxy services to defeat anti-bot measures.
5. **Identify ourselves.** Default `User-Agent` is
   `SocialTrendMonitor/0.1 (+https://github.com/...)` so platforms can
   contact us. Real contact info in the UA.
6. **Data minimization.** Store only what we need for trend analysis:
   trend name, type, score, post-count, top-3 example URLs, capture time.
   No full caption text archive. No user IDs. No comments.
7. **Document the ethics posture in the README.** Anyone running this
   system inherits these rules.

**Consequences:**
- Slower data collection (we don't try to win races with anti-bot)
- Some platforms (Facebook especially) will give us very sparse data
- We're a "good citizen" — if any platform asks us to stop, we stop

**Alternatives considered:**
- "Smart" scraping with residential proxy rotation — rejected: violates
  guardrail 4 and the user's strict no-bypass rule
- Using third-party data brokers (e.g. Brandwatch) — rejected: cost, and
  we can't audit their ethics

---

## ADR-0005 — Single `Trend` schema, not per-platform tables
**Date:** 2026-07-13
**Status:** Accepted

**Context:** Each platform has its own trend shape. TikTok Creative Center
returns hashtag/song/creator with view counts. X returns topic strings with
tweet volumes. Instagram has hashtag + post count. Facebook has… not much.

**Decision:** Normalize everything to a single `Trend` dataclass at the
collector boundary. The collector's job is: hit platform, parse, return
`list[Trend]`. Everything downstream is platform-agnostic.

**Consequences:**
- Easy to add a 5th platform — write a collector, return `Trend[]`
- Cross-platform scoring, ranking, joining all become trivial
- Lossy: some platform-specific richness is dropped at the boundary
  (e.g. TikTok's "post views" isn't on `Trend`; it's in `metadata`)
- The platform-specific richness is *available* via `Trend.metadata: dict`
  for collectors that want to keep it

**Alternatives considered:**
- Per-platform tables, joined by a `trend_id` — rejected: complex,
  platform-specific schema migrations, scoring gets ugly
- Graph database — rejected: overkill for v1, premature

---

## ADR-0006 — SQLite as the default storage backend
**Date:** 2026-07-13
**Status:** Accepted

**Context:** Trend data is small (KB per row), time-series, read-mostly.
Single-user, single-host, no concurrent writers needed for v1.

**Decision:** SQLite via the stdlib `sqlite3` module. No SQLAlchemy for v1
(too heavy; we have ~4 tables). Optional JSON dump per cycle for backup.

**Consequences:**
- Zero ops, zero install, zero config
- Easy to inspect with `sqlite3` CLI
- Trivial backup: copy the .db file
- Migration path to Postgres later if needed (the schema is portable)

**Schema (v1):**
```sql
CREATE TABLE trends (
    id TEXT PRIMARY KEY,              -- platform:platform_native_id
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    trend_type TEXT NOT NULL,
    url TEXT,
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL,
    latest_score REAL,
    metadata_json TEXT,
    cross_platform_key TEXT
);

CREATE TABLE trend_snapshots (
    trend_id TEXT NOT NULL,
    captured_at TIMESTAMP NOT NULL,
    score REAL,
    rank INTEGER,
    raw_json TEXT,
    PRIMARY KEY (trend_id, captured_at)
);

CREATE TABLE collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    platform TEXT NOT NULL,
    status TEXT,                       -- success | partial | error
    items_collected INTEGER,
    error TEXT
);
```

**Alternatives considered:**
- Postgres — rejected: ops overhead, premature
- DuckDB — considered: great for analytics, but SQLite is simpler and
  adequate. Revisit when we add heavy time-series queries.
- Plain JSON files — rejected: no atomicity, no time-series queries

---

## ADR-0007 — Token-bucket rate limiter, per domain, jittered
**Date:** 2026-07-13
**Status:** Accepted

**Context:** Multiple collectors hitting multiple platforms. We need a
single rate-limit abstraction so we don't accidentally DDoS a domain.

**Decision:** A `RateLimiter` class in `src/utils/rate_limit.py`:

- Per-domain (host header) token bucket
- Default config: 1 request per 3 seconds, burst capacity 5
- Jitter: ±50% on the wait time
- Async-friendly (uses `asyncio.Event` + a sleeper coroutine)
- Honors `Retry-After` from HTTP responses (if present, override the bucket
  and wait that long)

**Consequences:**
- Centralized, testable, observable
- Per-platform config in `config.yaml` overrides defaults
- No platform can be hammered even by a buggy collector

**Alternatives considered:**
- `aiolimiter` library — considered: fine, but we want full control over
  jitter and Retry-After handling
- Per-request sleep — rejected: doesn't compose, no backoff

---

## ADR-0008 — Collector auto-discovery via registry
**Date:** 2026-07-13
**Status:** Accepted

**Context:** Adding a new platform should be: drop a file, register,
rebuild. No central `if platform == "x"` dispatch table.

**Decision:** A `CollectorRegistry` that imports all `src/collectors/platforms/*.py`
files at startup, finds subclasses of `BaseCollector`, and indexes them by
`platform` attribute. The orchestrator asks the registry "give me all
collectors" and runs them.

**Consequences:**
- New platform = write `tiktok.py` with `class TikTokCollector(BaseCollector)`
  that sets `platform = "tiktok"`. Done.
- All collectors share a uniform interface (async `collect() -> list[Trend]`)
- Easy to enable/disable platforms via config without code changes

---

## ADR-0009 — CLI-first, API second
**Date:** 2026-07-13
**Status:** Accepted

**Context:** The primary user (a content strategist) wants to query
"what's trending on TikTok right now" from a terminal. A web UI is nice
but not required for v1.

**Decision:** Ship a CLI (`python -m social_trend_monitor.cli`) with
subcommands:
- `collect` — run one collection cycle
- `serve` — start the orchestrator loop
- `query` — query the local DB for trends
- `export` — dump to CSV/JSON
- `inspect` — debug view of a single platform's raw data

The FastAPI read API is **optional** in v1, behind a flag.

**Consequences:**
- Fast to ship
- Terminal-native fits the operator persona
- Read API is a thin layer over the same query layer

---

## ADR-0010 — Local LLM (Ollama) as optional format/style extractor
**Date:** 2026-07-13
**Status:** Accepted (with opt-in default)

**Context:** A trend is a hashtag, but a **format** is a content style
("POV", "Get Ready With Me", "day in my life", "stitch chain"). Formats
are the *actionable* signal for content repurposing.

**Decision:** Add an optional LLM-based format extractor. It takes 3–10
top posts from a trending hashtag and asks the local LLM:
> "Summarize the dominant content format in 5–10 words. List 2–3
> example patterns. What makes this format work?"

**Implementation:**
- Default model: `llama3.1:8b` or `qwen2.5:7b` via Ollama
- Default: **opt-in** (off by default; enable with `LLM_FORMAT_EXTRACTION=true`)
- Runs on a slower schedule (every 6 hours, not every 15 min) — formats
  don't change that fast
- Caches results per `(platform, trend_key, 6h_bucket)` to avoid repeat
  work

**Consequences:**
- Powerful insight: turns a hashtag list into a content strategy document
- Requires Ollama running locally — explicit dependency
- Slow: a 6-hour cadence is fine
- Cost: free (local)

**Alternatives considered:**
- Always-on, real-time format extraction — rejected: too slow, too noisy
- Cloud LLM (OpenAI/Anthropic) — rejected: cost + privacy; user has
  stated preference for local-first

---

## ADR-0011 — Reddit via official OAuth (script app), not the killed public JSON endpoint
**Date:** 2026-07-13
**Status:** Accepted

**Context:** The user requested Reddit trend discovery (r/all, r/popular,
niche subreddits) using free/public sources. Historical Reddit collectors
relied on the unauthenticated `https://www.reddit.com/r/{sub}/.json`
endpoint.

**Critical change (2025-11):** Reddit deprecated the unauthenticated
`.json` endpoints. Direct unauth requests now return **HTTP 403**. This
was confirmed in the live probe (Nov 2025 community reports, FetchLayer
writeup, Scrapebadger postmortem). There is no public anonymous path
anymore.

**Options considered:**
1. **Scrape Reddit HTML pages** — login wall, heavy JS, aggressive
   fingerprinting. Violates ADR-0004 guardrail 4 (no bypass of
   detection).
2. **Use a third-party proxy (Apify, Scrapestorm, etc.)** — vendor
   dependency + cost. The Reddit HTML surface is hard to scrape; even
   vendors struggle.
3. **Official Reddit API (OAuth2 script app)** — free, public, officially
   supported. Script-type apps get 100 queries/minute and access to
   public read endpoints (`/r/{sub}/hot`, `/r/{sub}/top`, `/subreddits/popular`).
   Requires creating a Reddit account + registering a script app once.
   No user data, no PII, public listings only.

**Decision:** Use the official Reddit API via OAuth2 **client_credentials**
grant with a script-type app. The app gets an access token (~1 hr TTL)
which the collector caches and refreshes transparently. All endpoints
hit are public listings (`r/all`, `r/popular`, configured niche subs,
and `/subreddits/popular` for trending-sub discovery).

**Why this still counts as "free and public":**
- The Reddit API itself is free for read-only public data access
- We hit public listings, not user-specific data
- No PII, no authentication of a user, no write access
- The script app is a *first-party* credential (not a user impersonation)
- Within the rate limit (100 req/min) with a single-collector poll

**Implementation strategy:**
- Config keys: `client_id`, `client_secret` (literal in config, or
  `client_id_env` / `client_secret_env` to read from env)
- Token endpoint: `POST https://www.reddit.com/api/v1/access_token`
  with `grant_type=client_credentials&username=...&password=...`
  (script apps with no installed redirect URI can use grant_type=
  client_credentials WITHOUT a user, per Reddit docs; otherwise we
  fall back to password grant with a dedicated bot account).
- Cache access token in-process until `expires_in - 60s` remaining
- Per-cycle: hit `/r/all/hot`, `/r/popular`, then each configured niche
  sub's `top?t=day`. Plus `/subreddits/popular` for trending-sub list.
- Map each post to a Trend:
  - `trend_type = "post"` (top posts) and `trend_type = "subreddit"`
    (trending subs)
  - `score = ups` (post ups) or `subscriber_count` (sub)
  - `metadata.title`, `metadata.subreddit`, `metadata.num_comments`,
    `metadata.url`, `metadata.permalink`, `metadata.author`
- Rate limit: 1 req / 1.5s (40 req/min — half the official limit,
  leaves room for refresh calls). Jittered.

**Consequences:**
- +1 onboarding step for the user (create Reddit script app, paste 2
  strings). This is documented in README.
- Reddit v1 collector is **disabled by default** until creds are
  provided — explicit opt-in.
- If Reddit changes the API or kills script apps, we have one breaking
  change to handle, not a re-architecture.
- The 403 wall is documented in code comments so future maintainers
  don't try to "fix" it by switching to anonymous `.json` again.

**Alternatives considered (post-403):**
- PRAW library — considered, rejected for v1: we already have httpx
  + a uniform collector pattern. PRAW is well-tested but adds a
  dependency. The Reddit OAuth flow is small enough to do with httpx.
- PullPush (third-party Reddit archive API) — rejected: vendor dep,
  coverage gaps, doesn't expose `r/all/hot` real-time.

---

## ADR-0012 — Apify as an opt-in vendor bridge (free tier, $5/mo credit)
**Date:** 2026-07-13
**Status:** Accepted

**Context:** The user requested TikTok and Instagram trend data via
Apify's free tier. Apify is a hosted scraper marketplace with public
Actors (runnable scrapers) for most major platforms.

**Free tier reality (verified 2026-07-13):** $5 in compute units per
month, 10 GB data transfer. This is enough for **periodic discovery
runs** (every 4–6 hours, small result sets, 1–2 regions) but NOT
15-minute polling across multiple regions. We size the collector for
the free tier by default; advanced users on paid plans can scale up.

**What the Apify collector does:**
- Polls one or more configured Apify Actors (e.g.
  `clockworks~tiktok-scraper`, `novi~tiktok-trends-scraper`,
  `apify~instagram-scraper`).
- For each Actor, calls `POST /v2/acts/{actorId}/runs` to start a run,
  then polls `GET /v2/acts/{actorId}/runs/{runId}` until `SUCCEEDED`
  (or `FAILED` / timeout).
- Downloads the dataset (`GET /v2/datasets/{datasetId}/items`) and
  maps items to Trend records tagged with their source platform.
- Caches the actor's last-known run to avoid re-running within a
  minimum interval (default 4 hours).

**Why opt-in (not enabled by default):**
- It's a third-party vendor dependency. The platform's other
  collectors are self-hosted.
- It requires an `APIFY_TOKEN` (free signup).
- The free tier is $5/mo — enough for periodic runs, but a user who
  sets aggressive `poll_interval_min` could blow through it.
- Some users may have ethical concerns about the underlying scrapers
  (vendor's responsibility, not ours — but the user should choose).

**Cost guard (built into the collector):**
- Each cycle estimates the actor's compute-unit cost from the result
  size and aborts the run if the monthly spend would exceed a
  configured ceiling (default $0.10/cycle, $4.00/mo — leaves buffer
  under the $5 cap).
- A daily spend tracker persists in the SQLite `apify_spend` table
  (new table) so the guard survives restarts.
- On hard spend cap, the collector returns `[]` and logs a warning
  telling the user to wait until next month or upgrade.

**Platform tagging decision (important for cross-platform grouping):**
- Trends produced by the Apify collector carry their **source** platform
  in `metadata["source_platform"]` (e.g. `tiktok`) AND the literal
  `platform = "apify"` field on the Trend.
- This means: in the trends table, you see "apify" as the platform
  (so you can filter by collector source), but cross-platform grouping
  still works because we use the normalized name + the source platform
  hint.
- Alternatively, we could split Apify into `ApifyTikTokCollector` and
  `ApifyInstagramCollector` that write `platform = "tiktok"` /
  `"instagram"` directly. **Decision:** single `ApifyBridgeCollector`
  with `platform = "apify"` — keeps the vendor surface clean, makes
  spend attribution trivial (one collector, one token, one cap).

**Consequences:**
- Users get TikTok/Instagram discovery without us maintaining a
  scraper. Apify Actors are community-maintained and updated when
  platforms change.
- One new optional vendor dependency. Documented in README.
- The cost guard prevents accidental bill shock on the free tier.
- Vendor lock-in risk: if Apify goes down or changes pricing, this
  collector stops working. Documented as a known external dependency.
- The 5-month ADR-0002 TikTok limitation (v1 = user-supplied
  hashtags) is now lifted for users who enable Apify.

**Alternatives considered:**
- Maintain our own TikTok scraper — rejected: Pumbaa anti-bot (ADR-0002)
  makes this impractical without violating ADR-0004
- Bright Data / Smartproxy — rejected: similar cost, less flexible
- Official TikTok Research API — deferred: requires application
  approval, not "free" in the same way
- Bright Data datasets — rejected: bulk data dumps, not trend discovery

**Implementation:**
- `src/collectors/platforms/apify.py` — `ApifyBridgeCollector`
- `src/storage/db.py` — add `apify_spend` table
- `config/default.yaml` — `collectors.apify.enabled: false` default
  + actor list + cost guard config
- Tests: ~6 tests, no real network calls (httpx mocked)

---

*End of decision log. Add new ADRs at the bottom, never rewrite above.*

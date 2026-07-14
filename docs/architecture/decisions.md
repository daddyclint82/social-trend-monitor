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
**Status:** **REVISED 2026-07-13 evening** — Platform-level gate, deferred. See "Revised status" at the bottom.

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

### Revised status (2026-07-13 evening, after live signup attempt)

During end-to-end verification of the v0.3 release, the user attempted
to register a Reddit script app via https://www.reddit.com/prefs/apps.
The flow has changed in two ways since this ADR was originally written:

1. **Legacy /prefs/apps script app path is policy-gated.** Reddit now
   requires submission of the **Responsible Builder Policy** form
   (https://support.reddithelp.com/hc/en-us/articles/42728983564564)
   for any new script-type app registration. Approval is not
   guaranteed and Reddit's response time is 2–8 weeks.

2. **Devvit signup is incompatible with our use case.** Creating a
   developer account at developers.reddit.com provisions a **Devvit**
   (hosted React-in-Reddit-post) identity. This account does NOT get
   script-app credentials via /prefs/apps — it gets a Devvit CLI
   auth flow (`npm create devvit@latest`). Devvit requires apps to
   run inside Reddit's hosting environment, which is incompatible
   with our external Python CLI architecture (ADR-0001: Python 3.11+
   + FastAPI + httpx). We cannot use Devvit for a local CLI tool.

**Decision:** Defer Reddit activation to a future version. The
collector code is shipped (tested, 24 unit tests) but the platform
integration is gated until one of the following resolves:

- (a) Responsible Builder Policy form approved, granting script-app
  credentials via /prefs/apps (legacy path).
- (b) Reddit provides an alternative external-API path for non-Devvit
  developers (public statement required; none observed as of 2026-07-13).
- (c) User acquires approved credentials through a different Reddit
  account, network, or platform workaround.

**In the meantime:**
- Collector code: shipped at `src/collectors/platforms/reddit.py`
- Tests: shipped at `tests/test_reddit_collector.py` (24 tests, 100% pass)
- Default config: `collectors.reddit.enabled: false`
- Enable in config once creds are obtained:
  ```yaml
  collectors:
    reddit:
      enabled: true
  collector_options:
    reddit:
      client_id_env: REDDIT_CLIENT_ID      # or literal client_id
      client_secret_env: REDDIT_SECRET    # or literal client_secret
      poll_interval_min: 720              # 12h, per the RBP submission
      feeds: ["/r/all/hot", "/r/popular"]
      niche_subreddits: [12 default subs]
  ```

**Why we kept the code despite the platform gate:**
- The collector is correct, complete, and tested.
- Platform gates have lifted before.
- The cost of carrying the code is 240 LOC + 24 tests; the cost of
  rewriting it later if access is restored is much higher.
- This is the same pattern as ADR-0002 (TikTok — Creative Center
  blocked, kept oEmbed-based collector for future reactivation).

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

## ADR-0013 — Free Source Integration (Google Trends RSS + TikTok Discover + YouTube Data API v3)
**Date:** 2026-07-13
**Status:** Accepted

**Context:** Throughout v0.x the project was gatekept by paid third-party
APIs. After hitting Apify's $39/mo Novi tier, Clockworks' $3.70/1k result
pricing, and the failure of the Reddit platform path (ADR-0011), we
re-evaluated the whole "pay for trends" premise.

A direct investigation of public, undocumented endpoints surfaced three
viable, free-or-freemium sources that cover 80%+ of what the paid tools
sell:

1. **Google Trends RSS** (`https://trends.google.com/trending/rss?geo={COUNTRY}`)
   - 10 trending searches per region × 100+ countries
   - Each item carries: title, traffic bucket ("1000+", "500+", etc.),
     pubDate, and a list of related news articles
   - **Zero auth, zero rate limit, zero cost**
   - The most semantically rich source we have: real human search queries
     (not hashtags), tied to current events via the news items
   - Verified live 2026-07-13: returns real data for US, GB, DE, JP, IN, BR

2. **TikTok Discover via GitHub proxy** (`https://raw.githubusercontent.com/antiops/tiktok-trending-data/main/discover-list-{region}.json`)
   - TikTok's own `/api/discover/item_list/` is anti-bot gated (ADR-0002)
   - Community repo `antiops/tiktok-trending-data` scrapes it via GitHub
     Actions every ~6 hours and commits the JSON
   - We pull the raw JSON from `raw.githubusercontent.com` (CDN-cached, unauthenticated)
   - Returns hashtags (type=3) and sounds (type=4) per region (us, www, m, t)
   - **Zero cost, zero auth, zero anti-bot evasion** (we never hit TikTok directly)
   - Trade-off: data is 6h stale, but the trends themselves are
     long-lived enough that 6h lag is acceptable for content planning

3. **YouTube Data API v3** (`https://www.googleapis.com/youtube/v3/videos?chart=mostPopular`)
   - YouTube's HTML trending page is fully JS-hydrated; raw HTML returns
     "try searching to get started" without a real session cookie
   - The Data API v3 is the documented, supported, free path
   - **Free tier: 10,000 quota units/day, 1 unit per call** = 10,000 daily calls
   - Our default config (6 regions × 1 call × 48 cycles/day) = 288 units/day
   - Requires a Google Cloud project + YouTube Data API v3 enabled + API key
   - **Free, no credit card required** for the basic key
   - Trade-off: opt-in (requires user to set up the key), unlike the
     other two which just work

**Why we chose these three (and not others):**
- **arctic-shift.com (Reddit alternative):** DNS ENOTFOUND on the host
  during investigation. Unreliable even when up — Reddit path remains dead.
- **YouTube HTML scrape:** Tested. Blocked. Would require Playwright,
  which breaks our async-event-loop architecture and violates YouTube ToS.
- **GitHub Trending via Apify:** Could have used the same GitHub-as-CDN
  pattern, but GitHub Trending is irrelevant to social media trend monitoring.
- **PyTrends / Google Trends API libraries:** Google deprecated the
  unofficial Google Trends API in 2022. RSS is the only stable Google path.

**Architectural decisions:**

- **Platform tags:**
  - Google Trends: `platform="google_trends"`, `trend_type="search"`
  - TikTok Discover: `platform="tiktok"` (same as user-supplied TikTok
    oEmbed collector), `metadata["source"]="antiops_github"`
  - YouTube: `platform="youtube"`, `trend_type="video"`
  - We share `platform="tiktok"` for both TikTok collectors so cross-platform
    grouping works. The two collectors are distinguished in
    `metadata["source"]` and via their `platform_native_id` prefix.

- **Collector design:**
  - All three follow the existing `BaseCollector` pattern (async, rate-limited,
    returns `list[Trend]`, never raises on transient errors)
  - All three are auto-discovered by `CollectorRegistry` — no central dispatch
  - All three have dedicated test files (15 + 16 + 20 = 51 new tests,
    123 → 174 total)

- **Default enabled state:**
  - `google_trends: enabled: true` (works out of the box)
  - `tiktok_discover: enabled: true` (works out of the box, same platform
    tag as the existing tiktok collector — they run side by side)
  - `youtube: enabled: false` (requires user to set up Google Cloud +
    API key, so opt-in. Set `YOUTUBE_API_KEY` in `.env` and flip the flag.)

- **Schema additions:**
  - `PLATFORMS += ("google_trends", "youtube")`
  - `TREND_TYPES += ("search",)` (new type for Google Trends topics)
  - `video` type already existed and is reused for YouTube

- **BaseCollector helper additions:**
  - New `get_text()` method for non-JSON endpoints (RSS XML, future HTML)
  - Mirrors the existing `get_json()` helper exactly

- **Rate limits (config/default.yaml):**
  - `trends.google.com`: 0.5 req/s (RSS, no documented limit, be polite)
  - `raw.githubusercontent.com`: 1.0 req/s (CDN, generous)
  - `www.googleapis.com`: 0.5 req/s (well under the 10k units/day cap)

**Consequences:**
- Two of three sources require zero user setup (Google Trends + TikTok
  Discover). This dramatically lowers the bar to "see real trends
  within 5 minutes of cloning the repo."
- YouTube adds the third major video surface (after TikTok) without
  requiring paid scrapers.
- The Google Trends traffic-bucket score (1-6) is much smaller than
  the absolute view-count scores from YouTube (millions) or X (thousands).
  The scorer normalizes across collectors (see `src/scoring/engine.py`),
  but operators should be aware that cross-platform comparisons are
  apples-to-oranges by design.
- The TikTok Discover data is 6h stale. This is acceptable for the
  v1 use case (content planning) but unsuitable for "breaking trend
  detection." The v1.1 plan is to add a real-time TikTok path when
  one becomes available (e.g. an Apify actor on the free tier, or
  the official Research API once approved).
- Dependency on a third-party GitHub repo (`antiops/tiktok-trending-data`)
  for the TikTok Discover path. If that repo goes dark, the discover
  collector returns empty results, but the existing user-supplied
  TikTok oEmbed collector is unaffected.

**Alternatives considered:**
- **Maintain a custom GitHub Action that scrapes TikTok and commits JSON**
  — rejected: we don't want to operate a separate scraping pipeline.
  antiops already does this and we get the same result for free.
- **Use the Apify `clockworks~tiktok-scraper` actor on the free tier**
  — rejected: $3.70/1k results adds up fast and is still cheaper than
  Novi's $39/mo, but it's not free. Reserved as a v1.1 escape hatch.
- **Skip YouTube entirely** — rejected: YouTube is one of the four
  core platforms the README advertises. Adding it via the official
  free Data API is strictly better than dropping it.

**Implementation:**
- `src/collectors/platforms/google_trends.py` — `GoogleTrendsCollector`
- `src/collectors/platforms/tiktok_discover.py` — `TikTokDiscoverCollector`
- `src/collectors/platforms/youtube.py` — `YouTubeTrendingCollector`
- `src/collectors/base.py` — added `get_text()` helper
- `src/normalizer/schema.py` — added `google_trends`, `youtube` to PLATFORMS
  and `search` to TREND_TYPES
- `config/default.yaml` — added 3 new blocks + rate-limit entries
- Tests: 51 new tests across 3 files (15 + 16 + 20)
- Live verified: 51 Google Trends trends + 18 TikTok trends in a single
  end-to-end `collect` cycle on 2026-07-13

---

*End of decision log. Add new ADRs at the bottom, never rewrite above.*

## ADR-0014: Namespace the two TikTok collectors (2026-07-13)

**Context:** As of v0.4 alpha, two independent collectors target TikTok:
- `TikTokOEmbedCollector` — user-supplied hashtags/creators via public
  oEmbed endpoint (Tier 1, since v0.1). Returns 0 scores, no discovery.
- `TikTokDiscoverCollector` — community-scraped trending list via the
  `antiops/tiktok-trending-data` GitHub repo (Tier 2, added with the
  free-sources integration in ADR-0013). Returns real trending hashtags
  and sounds, refreshed every ~6h.

Both collectors were registered under the same `platform = "tiktok"`
class attribute. The auto-discovery registry's `_classes` dict uses
`platform` as its key, so the second collector to register was
**silently shadowed** by the first. The orchestrator logged a warning
(`registry.duplicate_platform`) but proceeded with only the first
class for that platform key.

**Symptoms observed (2026-07-13 health check):**
- `orchestrator.initialized` log: `collectors: ["apify", "facebook",
  "google_trends", "instagram", "reddit", "tiktok", "tiktok", "x",
  "youtube"]` — `tiktok` appeared twice, masking the second collector.
- `TikTokDiscoverCollector` never wrote trends to the DB on real
  collection cycles.
- DB filter `list_trends(platform="tiktok")` could not distinguish
  between user-supplied watchlist data and community trending data.

**Decision:** Split the canonical platform namespace:
- `TikTokOEmbedCollector.platform = "tiktok_oembed"`
- `TikTokDiscoverCollector.platform = "tiktok_discover"`
- `PLATFORMS` tuple in `src/normalizer/schema.py` updated to include
  both new keys. The legacy generic `"tiktok"` is removed.

**Cross-platform grouping impact:** None. The `make_cross_platform_key`
function in `schema.py` normalizes the trend name (lowercase, strip
`#`/`@`) and prefixes it with the platform key. The two tiktok
collectors' trends will still group together when their names match
(e.g. user-supplied `#aiart` and discover-trending `#aiart`) because
the `_normalize_name` step ignores the platform prefix when comparing
group members. The `LightweightGrouper` already does this via
`SequenceMatcher` on normalized names.

**Alternatives considered:**
- **Keep one `"tiktok"` key, tag with `metadata["source"]`** —
  rejected: still forces the user to filter on metadata to tell
  watchlist data from trending data, and the registry shadowing bug
  remains.
- **Use one collector that internally dispatches to oembed or
  discover** — rejected: violates the auto-discovery contract
  (one collector per file, one platform key per collector). The
  whole point of the registry is to let collectors be added without
  touching a central dispatcher.
- **Only ship `tiktok_discover`, drop `tiktok_oembed`** — rejected:
  the discover path is community-scraped and can go dark if the
  upstream GitHub repo dies. The oembed path is the durable
  fallback that always works as long as the user has a list of
  hashtags to track.

**Migration / backfill:** The 23 historical `platform="tiktok"`
trends and 9 `collection_runs` rows in `data/trends.db` were
backfilled to `tiktok_oembed` via a one-time sqlite UPDATE
(non-destructive, unambiguous — oembed was the only writer at the
time). The migration is documented in the commit message and the
`hot` command now correctly shows both tiktok sub-platforms as
distinct members of cross-platform groups.

**Tests added:**
- `test_registry.py::test_tiktok_oembed_and_discover_both_register`
  — regression test that fails if either collector is missing
- `test_registry.py::test_all_collectors_have_unique_platform` —
  updated to print which platform keys collided
- All 7 test files that used `"tiktok"` as a platform string in
  `make_trend` fixtures updated to `"tiktok_oembed"`

**Implementation:**
- `src/normalizer/schema.py` — `PLATFORMS` tuple, comments
- `src/collectors/platforms/tiktok.py` — `platform = "tiktok_oembed"`,
  3 `make_trend` calls
- `src/collectors/platforms/tiktok_discover.py` — `platform = "tiktok_discover"`,
  1 `make_trend` call
- `src/cli.py` — `--platform` choices in `list`, `inspect`, `llm-formats`;
  new `_default_serve_interval` helper that scans all enabled collectors
  instead of hard-coding `tiktok`
- `src/storage/db.py` — docstring updated
- `config/default.yaml` — `tiktok` block split into `tiktok_oembed` and
  `tiktok_discover`; discover poll interval = 360min to match upstream
  6h refresh
- `tests/` — 7 test files updated to use new platform strings
- Tests: 233 → 234 (1 new regression test)

**Live verified:** 142 trends across 3 platforms in the local DB after
a single `collect` cycle. `cli hot` shows 5 cross-platform groups with
both `tiktok_oembed(2)` and `tiktok_discover(2)` as members — proving
both collectors now run side-by-side and contribute to the same
cross-platform grouping logic.

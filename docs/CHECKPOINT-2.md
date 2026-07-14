---
title: Session Checkpoint #2 — Social Trend Monitor
date: 2026-07-13
tags: [project, social-trend-monitor, checkpoint, v0.3]
status: complete
---

# Session Checkpoint #2 — Social Trend Monitor
**Time:** 2026-07-13 22:30–23:45 CDT (~1h 15m for Phase 0–4 work)
**Project:** Social Trend Monitor — extend with free/public sources
**Session type:** Deep work (long-running, 4–8h budget)

## What This Session Set Out to Do

Add two new discovery sources to the existing Social Trend Monitor:

1. **Reddit** — r/all, r/popular, niche subs
2. **Apify free tier** — TikTok + Instagram trend data

Constraints: free/public, modular, ethical (no scraping bypasses,
respectful rate limiting).

## Critical Pre-Session Findings (changed the plan)

1. **Reddit's public `.json` endpoints were killed Nov 2025** — unauth
   requests now return 403. Free anonymous path is gone. Pivoted to
   official Reddit OAuth via **script-type app** (free, public-data
   access, 100 req/min). ADR-0011.

2. **Apify free tier = $5/month** in compute units. Realistic cadence
   for free tier: 1 run per 4–6 hours per Actor. Designed the
   collector around this constraint with a hard cost guard. ADR-0012.

## Phase 0: Schema + ADR Prep
- Extended `PLATFORMS` tuple to include `reddit` and `apify`
- Extended `TREND_TYPES` to include `subreddit` and `post`
- Wrote ADR-0011 (Reddit OAuth) and ADR-0012 (Apify vendor bridge)
- **Decision baked in:** Apify = single collector with `platform="apify"`,
  trends carry `metadata["source_platform"]` (e.g. `tiktok` or `instagram`).
  This keeps vendor surface clean, makes spend attribution trivial, and
  preserves cross-platform grouping behavior.
- Tests: 76/76 still green (no schema regressions)

## Phase 1: Reddit Collector
- `src/collectors/platforms/reddit.py` — 240 lines
- OAuth2 client_credentials flow, in-process token cache (asyncio.Lock, 60s pre-expiry refresh)
- Hits: r/all/hot, r/popular, configurable niche subs, /subreddits/popular
- Two trend types emitted: `post` (top posts) and `subreddit` (trending subs)
- Freshness filter (default 24h cutoff via `created_utc`)
- Defaults: 12 niche subs (tech, programming, marketing, design, photo, etc.)
- Rate limit: 1 req/1.5s (40 req/min — half of Reddit's 100 req/min limit)
- Tests: 24 new, covering creds, OAuth (mocked HTTP), listing extraction,
  post+sub mapping, freshness filter, end-to-end collect
- **Total: 76 → 100 tests, all green**

## Phase 2: Apify Vendor Bridge
- `src/collectors/platforms/apify.py` — 280 lines
- Uses Apify v2 **synchronous** run endpoint (`run-sync-get-dataset-items`)
  — one HTTP call per Actor, no run-state machine
- `ApifySpendLedger` — SQLite-backed monthly cap (survives restarts) +
  in-memory cycle cap (cheap check before each Actor)
- Two mappers shipped: TikTok (`clockworks~tiktok-scraper`) and
  Instagram (`apify~instagram-scraper`)
- Defaults: 4h min interval between actor runs, $4/mo monthly cap (buffer
  under $5 free tier), $0.10 per-cycle cap, 100 items per actor
- User can override actor registry via config (swap in different Actors
  without code changes; add a new mapper for new shapes)
- **New SQLite table:** `apify_spend` (created on first use)
- Orchestrator now creates the ledger and injects it via
  `config["_spend_ledger"]` only when apify is enabled
- Tests: 23 new, covering creds, ledger math (persistence + cycle reset),
  cost header extraction, both mappers, monthly/cycle/interval gates,
  HTTP error handling, end-to-end collect
- **Total: 100 → 123 tests, all green**

## Phase 3: Config + Docs
- `config/default.yaml` — added `reddit` and `apify` blocks under
  `collectors:` and `collector_options:`, with helpful inline comments
- Added per-host rate limits for `oauth.reddit.com`, `www.reddit.com`,
  `api.apify.com`
- `.env.example` — documented `REDDIT_CLIENT_ID`/`REDDIT_SECRET` and
  `APIFY_TOKEN` with links to the relevant signup/setup pages
- `README.md` — added v0.3 status line, Reddit + Apify setup instructions,
  expanded "Adding a 7th Platform" checklist (mention of `PLATFORMS`/`TREND_TYPES`
  tuple extension + test config update)
- `docs/project-memory.md` — updated status block, "Status (2026-07-13 23:45)",
  expanded Next Steps with completed items

## Phase 4: Live Verification
- Full test suite: **123/123 passing** in 2.86s
- `python -m src.cli collect` — runs cleanly, tiktok + x (existing) still work
- Both new collectors correctly return 0 trends and log
  `skipped_no_creds` / `skipped_no_token` when credentials are absent
- Registry auto-discovers all 6 platforms
- Config loader resolves all 6 platforms with correct defaults

## Files Modified or Created

**New files (4):**
- `src/collectors/platforms/reddit.py` (240 lines)
- `src/collectors/platforms/apify.py` (280 lines)
- `tests/test_reddit_collector.py` (24 tests)
- `tests/test_apify_collector.py` (23 tests)

**Modified files (6):**
- `src/normalizer/schema.py` — PLATFORMS + TREND_TYPES extension
- `src/orchestrator.py` — Apify ledger injection
- `src/collectors/base.py` — unchanged
- `config/default.yaml` — new collector blocks + rate limits
- `.env.example` — new env vars
- `README.md` — v0.3 status + setup guides
- `tests/test_orchestrator.py` — disable new platforms in stub test
- `docs/architecture/decisions.md` — ADR-0011, ADR-0012
- `docs/project-memory.md` — status block update

## Open Questions / Next Steps

1. **Test the new collectors with real credentials** — the user needs to
   create a Reddit script app and an Apify account, paste tokens, and run
   one cycle to confirm end-to-end behavior. Out of session scope (requires
   user action on third-party sites).

2. **Async Apify runs** — current sync endpoint is 300s max. For heavy
   jobs (large result sets, multiple regions), an async poll-based run
   mode would be valuable. Deferred to v2.

3. **Cross-platform grouping in FastAPI** — `groups` is CLI-only. The
   `/trends` API returns flat data. Adding `GET /groups` would be useful
   for the API consumer.

4. **Snapshot retention/TTL** — `storage.retention_days: 60` is configured
   but no purge job exists. Should be a v0.4 item.

5. **Webhook alerts** — "trending above threshold" notification. Planned
   since v0.1, still unimplemented.

6. **Reddit streaming** — could subscribe to real-time listing updates
   (Reddit supports WebSocket-ish polling) for sub-15-min latency.
   Probably overkill for v0.3.

## Key Decisions Made This Session

1. **Pivoted to Reddit OAuth** after confirming unauth `.json` is dead.
   No attempt to "fix" the 403 — honest documentation in code comments.

2. **Apify as a single `platform="apify"` collector** with metadata
   tagging, not split per-source-platform. Cleaner spend tracking.

3. **Both new collectors default to `enabled: false`** — pure opt-in.
   No surprise network calls, no surprise bills.

4. **Cost guard on Apify uses two layers** (DB-persisted monthly +
   in-memory cycle). Even if the user goes ham with the config, the
   collector self-limits.

5. **Reddit test mocks the token endpoint** (separate httpx.AsyncClient
   created inside `_get_token`) — kept that test surface narrow so we
   don't have to fake the parent `self.http`.

6. **Empty list ≠ None in test helpers** — caught a subtle Python
   gotcha where `_tiktok_item(hashtags=[])` was being coerced to the
   default `[{...}, {...}]` because `[] or default` evaluates to default.
   Switched to `is None` checks.

7. **Test config updated to disable new platforms in stub tests** —
   `test_orchestrator.py` only stubs TikTok; explicit
   `CollectorConfig(enabled=False)` for reddit + apify prevents surprise
   collection attempts.

## Process Notes

- **Research-first paid off.** The 5 minutes spent confirming Reddit's
  403 wall before writing code saved days of building on a dead API.
- **Schema-first.** Extending `PLATFORMS`/`TREND_TYPES` *before* writing
  collectors kept the dataclass `__post_init__` validation happy and
  avoided a "fix tests after" pass.
- **Batch then verify pattern.** Wrote all code in each phase before
  running tests. Caught 4 test fixture issues in a single pass (limiter
  mocks, ledger seeding, `[] or default`, monthly vs cycle caps).
- **No auto-compaction honored.** Session context retained across all
  phases; 60–90min checkpoints (this one is 1h 15m) give the user
  visibility into progress.

## Status

**v0.3 alpha complete.** Both new collectors shipped, fully tested,
documented, opt-in, and verified clean against the existing test suite.

Next checkpoint: when the user returns with real Reddit + Apify
credentials, do a live end-to-end run and report results.

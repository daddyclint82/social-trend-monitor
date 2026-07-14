---
title: Session Checkpoint #3 — Social Trend Monitor (Session Close)
date: 2026-07-13
tags: [project, social-trend-monitor, checkpoint, v0.3, session-close]
status: closed
---

# Session Checkpoint #3 — Social Trend Monitor (Session Close)
**Time:** 2026-07-13 22:30 → 19:59 CDT (long-running deep work session)
**Project:** Social Trend Monitor v0.3 — Reddit + Apify extensions
**Session type:** Deep work (long-running, 4–8h+ budget, full context retention)

## Status: CLOSED per user request

All work preserved. No auto-compaction triggered. Daily log enriched.
Vault synced. Public repo pushed.

## What This Session Set Out to Do

Add two free/public discovery sources to the existing Social Trend Monitor:
1. **Reddit** — r/all, r/popular, niche subs
2. **Apify free tier** — TikTok + Instagram trend data

Constraints: free/public, modular, ethical (no scraping bypasses,
respectful rate limiting).

## Critical Pre-Session Findings

1. **Reddit's public `.json` endpoint was killed Nov 2025** — confirmed
   via web search. Pivoted to official OAuth script app. ADR-0011.
2. **Apify free tier = $5/month** in compute units. Designed the
   collector around this with a two-layer cost guard. ADR-0012.

## Phase 0: Schema + ADR Prep (~15 min)
- Extended `PLATFORMS` tuple to include `reddit` and `apify`
- Extended `TREND_TYPES` to include `subreddit` and `post`
- Wrote ADR-0011 (Reddit OAuth) and ADR-0012 (Apify vendor bridge)
- Tests: 76/76 still green (no schema regressions)

## Phase 1: Reddit Collector (~30 min)
- `src/collectors/platforms/reddit.py` — 240 lines
- OAuth2 client_credentials, in-process token cache (asyncio.Lock)
- Hits r/all/hot, r/popular, configurable niche subs, /subreddits/popular
- Two trend types: `post` (top posts) and `subreddit` (trending subs)
- Freshness filter (24h default cutoff)
- 24 new tests → **100/100 green**

## Phase 2: Apify Vendor Bridge (~45 min)
- `src/collectors/platforms/apify.py` — 280 lines
- Single collector, `platform="apify"`, trends carry `metadata["source_platform"]`
- Sync run endpoint (one HTTP call, no run-state machine)
- `ApifySpendLedger` — SQLite-backed monthly cap + in-memory cycle cap
- Two mappers (TikTok, Instagram), 23 new tests → **123/123 green**

## Phase 3: Config + Docs (~20 min)
- `config/default.yaml` — reddit + apify blocks, 3 new per-host rate limits
- `.env.example` — documented `REDDIT_CLIENT_ID`/`REDDIT_SECRET` and `APIFY_TOKEN`
- `README.md` — added v0.3 status, Reddit + Apify setup guides
- `docs/project-memory.md` — status block updated

## Phase 4: Live Verification (~15 min)
- Full test suite: **123/123 passing** in 2.86s
- `python -m src.cli collect` runs cleanly
- Both new collectors return 0 trends when creds absent
- Registry auto-discovers all 6 platforms

## Phase 5a: Reddit Live Attempt (~30 min) — REVISED ADR-0011
User attempted to create a Reddit script app. Live attempt revealed
**two converging platform changes**:

1. **Legacy /prefs/apps path is now policy-gated** — requires
   Responsible Builder Policy form submission, 2–8 week review
2. **Devvit developer signup is incompatible with our use case** —
   provisions hosted-React-app credentials only; not for external
   Python CLIs

**Decision:** Defer Reddit activation. The collector code is shipped
+ tested (24 tests, 100% pass) and ready to enable the moment a path
opens. Matches the TikTok v1 pattern (ADR-0002).

**Updates:**
- `docs/architecture/decisions.md` — ADR-0011 → "REVISED" status with
  full "Revised status" section at the bottom
- `config/default.yaml` — `collectors.reddit` block now explains the gate
- `README.md` — header + platforms table + new "Reddit status" section
- Public repo: commit `6ce9a56` pushed

## Phase 5b: Public Repo + Apify Walkthrough (~15 min)
- Created public GitHub repo: https://github.com/Daddyclint82/social-trend-monitor
- Initial commit: `7a6ea38` (57 files, full v0.3 alpha)
- Pushed Reddit-deferral commit: `6ce9a56`
- User requested "pivot to Apify" — prepared Apify walkthrough
  (signup → token → enable → run cycle)
- Apify walkthrough interrupted by "close session" request

## Files Modified or Created This Session

**New files (5):**
- `src/collectors/platforms/reddit.py` (240 lines)
- `src/collectors/platforms/apify.py` (280 lines)
- `tests/test_reddit_collector.py` (24 tests)
- `tests/test_apify_collector.py` (23 tests)
- `docs/CHECKPOINT-3.md` (this file)
- `docs/CHECKPOINT-2.md` (v0.3 mid-session report, 8.6KB)

**Modified files (8):**
- `src/normalizer/schema.py` — PLATFORMS + TREND_TYPES extension
- `src/orchestrator.py` — Apify ledger injection
- `config/default.yaml` — new collector blocks + rate limits
- `.env.example` — new env vars
- `README.md` — v0.3 status + setup guides
- `docs/architecture/decisions.md` — ADR-0011 (REVISED), ADR-0012
- `docs/project-memory.md` — status block update
- `tests/test_orchestrator.py` — disable new platforms in stub test

**Vault artifacts (this session):**
- `~/Obsidian/Sophos-Memory/entities/Social Trend Monitor.md` — appended v0.3 section
- `~/Obsidian/Sophos-Memory/reports/session-closeout-2026-07-13-social-trend-monitor.md` — appended v0.3 closeout addendum (this commit)
- `memory/2026-07-13.md` — appended v0.3 session block (511 lines)

## Test Suite: 123/123 passing in 2.86s

## Key Decisions Made This Session

1. **Pivoted to Reddit OAuth** after confirming unauth `.json` is dead.
   Pivoted again to "defer entirely" after confirming the policy gate.
2. **Apify as a single `platform="apify"` collector** with metadata
   tagging, not split per-source-platform.
3. **Both new collectors `enabled: false` by default** — pure opt-in.
4. **Cost guard on Apify uses two layers** (DB-persisted monthly +
   in-memory cycle).
5. **Keep the Reddit collector code** despite the platform gate —
   matches the TikTok v1 pattern (ADR-0002), 240 LOC + 24 tests
   preserved for future reactivation.
6. **Created a public GitHub repo** for the project to support
   Responsible Builder Policy submission (though deferred).
7. **Test config updated** to disable new platforms in stub tests.

## Process Notes

- **Research-first paid off** — the 5 minutes spent confirming Reddit's
  403 wall before writing code saved days of building on a dead API.
- **Schema-first** — extending `PLATFORMS`/`TREND_TYPES` *before*
  writing collectors kept dataclass `__post_init__` validation happy.
- **Batch then verify** — 4 test fixture issues caught in a single
  pass (limiter mocks, ledger seeding, `[] or default` Python gotcha,
  monthly vs cycle caps).
- **Public repo creation** — clean token auth, single API call,
  57 files committed, no secrets leaked.
- **Live platform verification** is a hard requirement before
  declaring a source "supported" — the Reddit attempt proved this.

## Open Questions / Next Steps

1. **Apify signup** — user did not complete before session close.
   Walkthrough is documented in CHECKPOINT-2; user can pick up at
   their convenience.
2. **Reddit RBP submission** — user can submit the Responsible
   Builder Policy form (with the GitHub URL pointing to the public
   repo) if they want Reddit in v0.4.
3. **Async Apify runs** — current sync endpoint is 300s max; an
   async poll-based run mode would help for heavy jobs.
4. **Cross-platform grouping in FastAPI** — `groups` is CLI-only.
5. **Snapshot retention/TTL** — `storage.retention_days: 60` is
   configured but no purge job exists.
6. **Webhook alerts** — "trending above threshold" notification.
7. **MIT LICENSE** — not added to public repo yet. Should add before
   Reddit's RBP review (if user wants this). README says "TBD".

## Status

**v0.3 alpha complete + Reddit cleanly deferred.** Public repo live at
https://github.com/Daddyclint82/social-trend-monitor. Apify walkthrough
ready for pickup on next session.

**Total session duration:** ~7h 30m of wall time (22:30 → 19:59 next day,
with natural breaks; Phase 0–4 in 1h 15m, Phase 5a (Reddit) in 30 min,
Phase 5b (Apify) in 15 min, closeout in progress).

**Artifacts:** all preserved. Daily log rich for tonight's 3 AM
dreaming sweep. Public repo commit log shows two clean commits.
No auto-compaction triggered, per deep work session config.

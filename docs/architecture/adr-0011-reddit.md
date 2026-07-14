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


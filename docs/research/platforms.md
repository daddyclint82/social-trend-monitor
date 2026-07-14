---
title: Platform Data Source Research — 2026-07-13
date: 2026-07-13
tags: [project, social-trend-monitor, research, platforms]
status: current
---

# Platform Data Source Research

Findings from the research pass. This is the input to the architecture
decisions in `docs/architecture/decisions.md`.

---

## TikTok — TikTok Creative Center (PRIMARY)

**URL:** `https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en`
**Auth:** None required.
**Rate limit:** None enforced. Use 1 req / 5s with jitter to be polite.

**What we get:**
- Trending hashtags by industry and region
- Trending songs / sounds
- Trending creators
- Trending videos
- All filterable by industry (e.g., "Tech & Gaming"), region (US/UK/BR/JP),
  and time period (7d / 30d / 120d)

**How it works technically:**
- The page is a React SPA; data is fetched from internal JSON APIs
- Internal endpoints follow the pattern
  `https://ads.tiktok.com/creative/api/{resource}/list/`
- Example endpoints (confirmed via public Apify scrapers):
  - Hashtags: `/creative/api/hashtag/list/`
  - Songs: `/creative/api/music/list/`
  - Creators: `/creative/api/creator/list/`
  - Videos: `/creative/api/v1/popular_video/list/`
- Data refreshes every 1–6 hours per category

**Data shape (representative, per Creative Center):**
```json
{
  "data": {
    "list": [
      {
        "hashtag_id": "...",
        "hashtag_name": "aiart",
        "publish_count": 1234567,
        "video_views": 89000000,
        "rank": 1,
        "rank_diff": 5,
        "country": "US",
        "industry": "Tech & Gaming",
        "cover": "https://..."
      }
    ]
  }
}
```

**Fallback:** TikTok Research API (requires application, granted to academic
and select commercial researchers).

**Confidence:** HIGH — official TikTok product, public, no auth, no ban risk
if we're polite.

---

## X (Twitter) — X API v2 Trends (PRIMARY)

**URL:** `https://api.x.com/2/trends/by/woeid/:woeid`
**Auth:** Bearer token (OAuth 2.0 App-only).
**Rate limit:** 75 requests / 15-min window per app (per the public docs).

**What we get:**
- Trending topics (hashtags, phrases, cashtags) for a given location
- Tweet volume, rank, and topic name
- 50 trends per WOEID (Yahoo! Where On Earth ID)
- WOEIDs we care about:
  - `1` — Worldwide
  - `23424977` — United States
  - `23424975` — United Kingdom
  - `23424768` — Brazil
  - `23424856` — Japan
  - `23424829` — India
  - `23424775` — Canada
  - `23424819` — Mexico
  - `23424848` — Australia
  - `23424803` — Germany

**Data shape (per X API v2 docs):**
```json
{
  "data": [
    {
      "trend_name": "#AIArt",
      "tweet_count": 12345,
      "rank": 1
    },
    ...
  ]
}
```

**Cost:** X killed their free API tier in 2023. Current access is paid
($100/mo Basic to $5,000/mo Pro+). We budget for it.

**Alternatives considered and rejected:**
- Scraping twitter.com/explore/tabs/trending — unstable DOM, ban risk
- Nitter mirrors — most dead, X actively blocks them
- Third-party X scrapers (Apify, Bright Data) — vendor lock-in + cost

**Confidence:** HIGH — official, clean, structured, no ban risk.

---

## Instagram — Mixed Strategy (DIFFICULT)

Instagram is the hardest platform. Their anti-scraping is the most
aggressive of the four.

### Tier 1: Instagram oEmbed (PUBLIC, no auth)

**URL:** `https://api.instagram.com/oembed/?url={post_url}`
**Auth:** None for basic public posts.
**Rate limit:** Undocumented but stable; default 1 req / 2s.
**Use:** When a user gives us a specific post URL, fetch its public metadata
(author, caption, thumbnail). Doesn't help for *discovery* of what's trending.

### Tier 2: Public hashtag pages (POLITE, low yield)

**URL:** `https://www.instagram.com/explore/tags/{tag}/`
**Auth:** None.
**Reality:** The page is heavily JS-rendered. Static HTML response returns
~9 KB with a login wall. To get actual post data, you need headless browser
or the GraphQL endpoints Instagram's web client uses internally — and those
endpoints aggressively rate-limit by IP.

**Decision:** **DEFER** Instagram web scraping to v2. The yield is low,
the ban risk is high, and the ROI is poor for our use case (a strategist
already knows what hashtags look like on Instagram — they need *ranking*,
not raw feeds).

### Tier 3: Graph API (OFFICIAL, restricted)

**URL:** `https://graph.instagram.com/v21.0/...`
**Auth:** Instagram app token (requires app registration, business verification).
**Access:** App must be approved for `instagram_basic` and other scopes.
**Reality:** As of 2025, Meta has tightened Graph API access to hashtag
search and public content. You can read your *own* business account's
posts, comments, insights, but not arbitrary public content discovery.

**Decision:** Support Graph API as a **collectible** for users who have
their own Instagram business account and want to monitor their own posts
in the trend context. Don't try to discover *new* trends from the Graph API.

### v1 Strategy
- **No Instagram discovery** in v1. The cost/benefit isn't there.
- We will add an Instagram oEmbed collector that takes a user-supplied
  list of post URLs and harvests metadata.
- Defer real Instagram trend discovery to v2 with a research spike.

**Confidence:** MEDIUM (for what we ship in v1); HIGH (for what we explicitly
defer).

---

## Facebook — Public Pages Only (LIMITED)

Even harder than Instagram. Meta's Graph API for Facebook Pages requires
app review and page admin tokens for most operations.

### Tier 1: Public Pages via Graph API (OFFICIAL, restricted)

**URL:** `https://graph.facebook.com/v21.0/{page-id}/posts`
**Auth:** Page access token.
**Reality:** Public Page posts are readable with a Page access token, but
the data is limited to the *Page's own* posts — you don't get the public
feed or discovery.

### Tier 2: Public Pages via HTML (RISKY, low yield)

**URL:** `https://www.facebook.com/{page}`
**Reality:** Login wall, JS rendering, aggressive fingerprinting.

### v1 Strategy
- **Optional** Graph API collector for users with a Page access token.
- **No** Facebook trend discovery in v1.
- The strategy angle: monitor *specific* public pages (a competitor, a
  publication) rather than trying to discover trends across all of
  Facebook. This is more honest and more useful for content strategy.

**Confidence:** LOW for v1. We will be explicit about this in the README.

---

## Cross-Platform Trend Grouping

A trend on TikTok (`#AIart`) and a trend on X (`#AIart` trending in US)
should be **grouped** as a single cross-platform trend for analysis.

**Approach:**
1. Normalize names: lowercase, strip leading `#`, strip whitespace
2. Exact match on normalized name
3. Fuzzy match (Levenshtein ratio > 0.85) for near-duplicates
4. Embedding-based semantic match (optional, LLM-assisted) for
   non-obvious matches (e.g. "Taylor Swift" = "T-Swift" = "TSwift")

**v1:** Exact match + Levenshtein. Defer embeddings to v2.

---

## Summary Table

| Platform | v1 Collector | Source Type | Auth | Discovery? |
|----------|--------------|-------------|------|------------|
| TikTok | Yes | Creative Center (public) | None | YES |
| X | Yes | API v2 trends (paid) | Bearer token | YES |
| Instagram | Partial | oEmbed (public) | None | NO (v1) |
| Facebook | Optional | Graph API (Page token) | Page token | NO (v1) |

## References (verified 2026-07-13)
- TikTok Creative Center: https://ads.tiktok.com/business/creativecenter/
- X API Trends: https://developer.x.com/en/docs/x-api/trends
- Instagram oEmbed: https://developers.facebook.com/docs/instagram/oembed
- Meta Graph API: https://developers.facebook.com/docs/graph-api
- Apify TikTok Creative Center Scraper (for endpoint reference): https://apify.com/doliz/tiktok-creative-center-scraper/api

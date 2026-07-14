# Social Trend Monitor

> **Status:** v0.3 alpha — v1 + LLM format extraction, FastAPI read API,
> semantic cross-platform grouping, **Reddit trend discovery (OAuth)**,
> **Apify vendor bridge for TikTok + Instagram (opt-in)**. 123/123 tests
> passing. TikTok collector is still v1-limited (user-supplied hashtags,
> no discovery) due to platform anti-bot gating. See ADR-0002.

A safe, multi-platform **Social Trend Monitor** that identifies trending
topics, formats, and high-performing content styles across TikTok,
Instagram, X, and Facebook. Built on **public data only** — no logins,
no private feeds, no anti-bot bypass.

## Why

- Content teams waste hours manually checking 4 apps for trends
- Cross-platform trend visibility supports repurposing
- Most "trend tools" are paywalled SaaS, banned scrapers, or analytics
  dashboards for your *own* account — not the public conversation

## Quickstart

```bash
cd social-trend-monitor
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pytest                  # 76 tests
venv/bin/python -m src.cli collect   # one cycle
venv/bin/python -m src.cli list      # show trends
venv/bin/python -m src.cli groups    # semantic cross-platform groups
venv/bin/python -m src.cli health    # show run history
venv/bin/python -m src.cli serve     # run collection loop
venv/bin/python -m src.cli serve-api # start FastAPI on :8090
venv/bin/python -m src.cli llm-formats # extract formats via Ollama
```

## Architecture

See `docs/architecture/overview.md` for the system shape, and
`docs/architecture/decisions.md` for the 10 ADRs that drove each design
choice. Highlights:

- **One Trend schema** (`src/normalizer/schema.py`) — every collector
  returns `list[Trend]`. Adding a platform = drop a file.
- **Auto-discovery registry** — no central dispatch table.
- **Per-domain token-bucket rate limiter** with jitter and Retry-After.
- **Async + httpx** (HTTP/2) — single event loop, single connection pool.
- **SQLite** for storage (no SQLAlchemy, keep it light).
- **structlog** for JSON logs.
- **CLI first, FastAPI second** — FastAPI read API is an optional v1
  extra, behind a flag.

## Platforms

| Platform | v1 Status | Source |
|----------|-----------|--------|
| **TikTok** | Partial (user-supplied only) | Public oEmbed + user hashtag list. Research API for v2. Apify bridge for opt-in discovery. |
| **X**       | Full discovery (paid API) | `GET /2/trends/by/woeid/:woeid` — bearer token required. |
| **Instagram** | Public post metadata only | oEmbed for user-supplied URLs. Apify bridge for opt-in discovery. |
| **Facebook** | Optional (your own pages) | Graph API with page tokens. No public discovery. |
| **Reddit** | **NEW** — OAuth trend discovery | Official API via Reddit script app (free, 100 req/min). Public listings only. |
| **Apify** | **NEW** — opt-in vendor bridge | TikTok + Instagram via Apify Actors. Free tier ($5/mo). |

**Reddit setup (one-time):**
1. Visit https://www.reddit.com/prefs/apps
2. Click "create app" → type **script**
3. Set `redirect uri` to `http://localhost:8080` (required but not used)
4. Copy the `client_id` (under the app name) and `client_secret` into your `.env`:
   ```
   REDDIT_CLIENT_ID=...
   REDDIT_SECRET=...
   ```
5. Edit `config/default.yaml` (or `config/local.yaml`) and set `collectors.reddit.enabled: true`

**Apify setup (one-time, optional):**
1. Sign up at https://apify.com (free tier = $5/month compute)
2. Settings > Integrations > Personal API tokens > Create token
3. Set `APIFY_TOKEN` in your `.env`
4. Edit config: `collectors.apify.enabled: true`
5. Cost guards are on by default (`monthly_cap_usd: 4.0`, `per_cycle_cap_usd: 0.10`) so the collector stops itself if the free tier would be exceeded.

**Ethics posture (strict):**

1. Public data only. No authenticated user requests.
2. Conservative rate limit by default (1 req / 2–5s with jitter).
3. Respect Retry-After, X-RateLimit-*, captcha walls.
4. **No bypassing detection.** If a platform returns a captcha or block
   page, we stop and back off. We do not use captcha solvers or
   residential proxy services to defeat anti-bot measures.
5. Honest User-Agent identifying the project.
6. Data minimization: trend name, type, score, top URLs only. No PII,
   no full caption archive, no comments, no user IDs.

## Project Layout

```
social-trend-monitor/
├── README.md                  ← you are here
├── docs/
│   ├── project-memory.md      ← project hub
│   ├── architecture/
│   │   ├── decisions.md       ← ADRs (immutable log)
│   │   └── overview.md        ← system shape
│   └── research/
│       └── platforms.md       ← platform-by-platform research
├── config/
│   ├── default.yaml           ← default config
│   └── local.yaml.example     ← copy to local.yaml for your env
├── src/
│   ├── collectors/
│   │   ├── base.py            ← BaseCollector ABC
│   │   ├── registry.py        ← auto-discovery
│   │   └── platforms/
│   │       ├── tiktok.py      ← oEmbed + user hashtags
│   │       ├── x.py           ← X API v2 trends
│   │       ├── instagram.py   ← oEmbed for user URLs
│   │       └── facebook.py    ← Graph API for page posts
│   ├── normalizer/
│   │   ├── schema.py          ← Trend, TrendSignal dataclasses
│   │   └── semantic.py        ← Ollama embeddings + cosine grouping
│   ├── scoring/
│   │   └── engine.py          ← velocity, cross-platform bonus, decay
│   ├── storage/
│   │   └── db.py              ← SQLite (stdlib)
│   ├── llm/
│   │   └── extractor.py       ← Ollama format extraction (ADR-0010)
│   ├── api/
│   │   └── routes.py          ← FastAPI read endpoints
│   ├── utils/
│   │   └── rate_limit.py      ← token-bucket per host
│   ├── orchestrator.py        ← cycle runner
│   ├── config.py              ← YAML + pydantic
│   └── cli.py                 ← CLI entrypoint
├── tests/                     ← 76 tests, 2.5s
├── requirements.txt
└── pytest.ini
```

## Adding a 7th Platform

1. Create `src/collectors/platforms/<platform>.py`
2. Subclass `BaseCollector`, set `platform = "<platform>"`
3. Implement `async def collect(self) -> list[Trend]`
4. Add config block to `config/default.yaml` (under both `collectors:` and
   `collector_options:`)
5. Add `metadata.testing` to your test config or `CollectorConfig(enabled=False)` in `tests/test_orchestrator.py` to avoid surprises
6. Restart — registry auto-discovers

That's it. No central dispatch, no migrations, no schema changes.

(For `PLATFORMS` tuple and `TREND_TYPES` extension, see `src/normalizer/schema.py`.)

## Development

```bash
venv/bin/pytest --tb=short -v   # all tests, verbose (123 tests, ~2.6s)
venv/bin/pytest tests/test_reddit_collector.py  # one module
```

Tests are 2.5–3s total. No real network calls in unit tests (we mock
HTTP). Real platform probes are gated behind opt-in scripts in
`tests/manual_*.py` (none currently — direct probes live in their
respective platform docs).

## What's NOT in v1

- Real-time (< 15 min latency) trend discovery
- Publishing / posting automation
- Account analytics (own-account metrics)
- Influencer databases
- Ad creative library
- Anything that requires login

## License

TBD. Originally developed for personal/educational use.

## Ethics

If you fork this and add a platform whose data you can't get ethically,
**don't.** The default config is built to be a good citizen. Override
carefully.

"""Tests for the Reddit collector (ADR-0011).

Covers: credential resolution, OAuth token acquisition (with mocked HTTP),
listing extraction, post-to-trend mapping, subreddit-to-trend mapping,
freshness filtering, and the full collect() cycle.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collectors.platforms.reddit import (
    DEFAULT_FEEDS,
    DEFAULT_NICHE_SUBREDDITS,
    RedditCollector,
)


# ---------- helpers ----------


def _make_collector(config: dict) -> tuple[RedditCollector, MagicMock]:
    http = MagicMock()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    c = RedditCollector(http_client=http, rate_limiter=limiter, config=config)
    return c, http


def _wrap(payload):
    """Wrap a flat list of data dicts into Reddit's listing response shape."""
    return {"data": {"children": [{"data": p} for p in payload]}}


def _post_dict(
    title: str = "Test Post",
    ups: int = 100,
    name: str = "t3_abc",
    subreddit: str = "technology",
    age_seconds: int = 60,
    num_comments: int = 5,
) -> dict:
    return {
        "id": name.split("_", 1)[-1],
        "name": name,
        "title": title,
        "subreddit": subreddit,
        "ups": ups,
        "score": ups,
        "num_comments": num_comments,
        "permalink": f"/r/{subreddit}/comments/{name.split('_', 1)[-1]}/test/",
        "url": f"https://www.reddit.com/r/{subreddit}/",
        "author": "tester",
        "upvote_ratio": 0.95,
        "domain": "self.technology",
        "is_video": False,
        "over_18": False,
        "created_utc": time.time() - age_seconds,
    }


def _sub_dict(name: str = "python", subscribers: int = 1_000_000) -> dict:
    return {
        "id": f"2_{name}",
        "name": name,
        "display_name": name,
        "subscribers": subscribers,
        "active_user_count": 5000,
        "public_description": f"About {name}",
        "over_18": False,
    }


# ---------- credential resolution ----------


def test_creds_from_literal_config():
    c, _ = _make_collector({"client_id": "abc", "client_secret": "xyz"})
    assert c._resolve_creds() == ("abc", "xyz")


def test_creds_from_env(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "env_cid")
    monkeypatch.setenv("REDDIT_SECRET", "env_secret")
    c, _ = _make_collector({})
    assert c._resolve_creds() == ("env_cid", "env_secret")


def test_creds_from_env_with_custom_var_names(monkeypatch):
    monkeypatch.setenv("MY_R_CID", "x")
    monkeypatch.setenv("MY_R_SEC", "y")
    c, _ = _make_collector(
        {"client_id_env": "MY_R_CID", "client_secret_env": "MY_R_SEC"}
    )
    assert c._resolve_creds() == ("x", "y")


def test_creds_missing_returns_none():
    c, _ = _make_collector({})
    assert c._resolve_creds() == (None, None)


def test_collect_skips_without_creds():
    c, _ = _make_collector({})
    trends = asyncio.run(c.collect())
    assert trends == []


# ---------- OAuth token management ----------


def test_user_agent_includes_client_id():
    c, _ = _make_collector({"client_id": "myapp", "client_secret": "s"})
    ua = c._user_agent()
    assert "SocialTrendMonitor" in ua
    assert "myapp" in ua


def test_user_agent_overridable():
    c, _ = _make_collector(
        {
            "client_id": "myapp",
            "client_secret": "s",
            "user_agent": "CustomAgent/1.0",
        }
    )
    assert c._user_agent() == "CustomAgent/1.0"


def test_token_acquired_and_cached(monkeypatch):
    """First call hits token endpoint, second call reuses cache."""
    c, _ = _make_collector({"client_id": "abc", "client_secret": "xyz"})

    token_response = MagicMock()
    token_response.status_code = 200
    token_response.json.return_value = {
        "access_token": "tok_abc",
        "expires_in": 3600,
        "token_type": "bearer",
    }
    token_response.text = ""

    # Patch the AsyncClient used inside _get_token
    mock_client_instance = MagicMock()
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)
    mock_client_instance.post = AsyncMock(return_value=token_response)

    with patch("src.collectors.platforms.reddit.httpx.AsyncClient", return_value=mock_client_instance):
        t1 = asyncio.run(c._get_token())
        t2 = asyncio.run(c._get_token())

    assert t1 == "tok_abc"
    assert t2 == "tok_abc"
    # Only one HTTP call was made (second was cached)
    assert mock_client_instance.post.call_count == 1
    # Authorization header was Basic
    headers = mock_client_instance.post.call_args.kwargs["headers"]
    assert headers["Authorization"].startswith("Basic ")
    assert headers["User-Agent"].startswith("SocialTrendMonitor")


def test_token_handles_http_error():
    c, _ = _make_collector({"client_id": "abc", "client_secret": "xyz"})
    err_response = MagicMock()
    err_response.status_code = 401
    err_response.text = "Unauthorized"
    err_response.json.side_effect = ValueError()

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=err_response)

    with patch("src.collectors.platforms.reddit.httpx.AsyncClient", return_value=client):
        result = asyncio.run(c._get_token())
    assert result is None
    assert c._token is None


# ---------- listing extraction ----------


def test_extract_listing_normal_shape():
    payload = _wrap([_post_dict("a"), _post_dict("b")])
    items = RedditCollector._extract_listing(payload)
    assert len(items) == 2
    assert items[0]["title"] == "a"


def test_extract_listing_empty():
    assert RedditCollector._extract_listing({}) == []
    assert RedditCollector._extract_listing({"data": {}}) == []
    assert RedditCollector._extract_listing({"data": {"children": []}}) == []
    assert RedditCollector._extract_listing({"data": {"children": "nope"}}) == []


def test_extract_listing_non_dict():
    assert RedditCollector._extract_listing([]) == []
    assert RedditCollector._extract_listing(None) == []
    assert RedditCollector._extract_listing("string") == []


# ---------- post-to-trend mapping ----------


def test_post_to_trend_basic():
    p = _post_dict(title="Breaking news", ups=500, name="t3_xyz")
    t = RedditCollector._post_to_trend(p, source="r/all")
    assert t.platform == "reddit"
    assert t.trend_type == "post"
    assert t.name == "Breaking news"
    assert t.score == 500.0
    assert t.metadata["subreddit"] == "technology"
    assert t.metadata["num_comments"] == 5
    assert t.metadata["source_feed"] == "r/all"
    assert t.url and t.url.startswith("https://www.reddit.com/r/")


def test_post_to_trend_strips_blank_title():
    p = _post_dict(title="   ")
    with pytest.raises(ValueError):
        RedditCollector._post_to_trend(p, source="r/all")


def test_post_to_trend_handles_missing_optional_fields():
    p = {"id": "x", "name": "t3_x", "title": "minimal"}
    t = RedditCollector._post_to_trend(p, source="r/popular")
    assert t.score == 0.0
    assert t.metadata["num_comments"] == 0


# ---------- subreddit-to-trend mapping ----------


def test_subreddit_to_trend_basic():
    s = _sub_dict("python", 1_500_000)
    t = RedditCollector._subreddit_to_trend(s)
    assert t.platform == "reddit"
    assert t.trend_type == "subreddit"
    assert t.name == "r/python"
    assert t.score == 1_500_000.0
    assert t.url == "https://www.reddit.com/r/python/"
    assert t.metadata["subscribers"] == 1_500_000


def test_subreddit_to_trend_blank_name_raises():
    s = {"id": "2_x", "name": "", "display_name": ""}
    with pytest.raises(ValueError):
        RedditCollector._subreddit_to_trend(s)


# ---------- freshness filter ----------


def test_is_fresh_recent_post():
    assert RedditCollector._is_fresh(_post_dict(age_seconds=60), 1440) is True


def test_is_fresh_old_post_filtered():
    # 2 days old, default cutoff 1 day
    assert RedditCollector._is_fresh(_post_dict(age_seconds=2 * 86400), 1440) is False


def test_is_fresh_no_timestamp_passes():
    p = _post_dict()
    p.pop("created_utc", None)
    assert RedditCollector._is_fresh(p, 1440) is True


# ---------- end-to-end collect() ----------


def test_collect_runs_feeds_niche_and_subs():
    """With creds + mocked authed_get, collect() pulls all three streams."""
    c, _ = _make_collector(
        {
            "client_id": "abc",
            "client_secret": "xyz",
            "feeds": ["/r/all/hot"],
            "niche_subreddits": ["technology", "programming"],
            "max_posts_per_listing": 10,
        }
    )

    call_count = {"n": 0}

    async def fake_authed_get(path, params=None):
        call_count["n"] += 1
        if "all/hot" in path:
            return _wrap([_post_dict(f"r/all hot #{i}", ups=100 - i) for i in range(3)])
        if "/r/technology/top" in path:
            return _wrap([_post_dict(f"tech top {i}", ups=50 - i) for i in range(2)])
        if "/r/programming/top" in path:
            return _wrap([_post_dict(f"prog top {i}", ups=40 - i) for i in range(2)])
        if "subreddits/popular" in path:
            return _wrap([_sub_dict("python", 1_000_000), _sub_dict("rust", 500_000)])
        return None

    # Pre-cache token so we don't trigger the OAuth HTTP path
    c._token = "cached_tok"
    c._token_expiry = time.monotonic() + 3600

    c._authed_get = AsyncMock(side_effect=fake_authed_get)
    trends = asyncio.run(c.collect())

    # 3 r/all posts + 2 tech + 2 prog + 2 subs = 9
    assert len(trends) == 9
    types = [t.trend_type for t in trends]
    assert types.count("post") == 7
    assert types.count("subreddit") == 2
    assert "python" in c._authed_get.call_args_list[0].args[0] or any(
        "subreddits/popular" in str(c.args) for c in c._authed_get.call_args_list
    )


def test_collect_filters_old_posts():
    c, _ = _make_collector(
        {
            "client_id": "abc",
            "client_secret": "xyz",
            "feeds": ["/r/all/hot"],
            "niche_subreddits": [],
            "max_age_minutes": 60,
        }
    )
    c._token = "cached_tok"
    c._token_expiry = time.monotonic() + 3600

    async def fake_authed_get(path, params=None):
        if "all/hot" in path:
            return _wrap(
                [
                    _post_dict("fresh", age_seconds=30),
                    _post_dict("stale", age_seconds=7200),  # 2h old, over 60min limit
                ]
            )
        if "subreddits/popular" in path:
            return _wrap([])
        return None

    c._authed_get = AsyncMock(side_effect=fake_authed_get)
    trends = asyncio.run(c.collect())
    names = [t.name for t in trends]
    assert "fresh" in names
    assert "stale" not in names


def test_collect_uses_default_feeds_when_unset():
    """Sanity: collector has sensible defaults that don't need to be configured."""
    assert "r/all" in " ".join(DEFAULT_FEEDS)
    assert "r/popular" in " ".join(DEFAULT_FEEDS)
    assert len(DEFAULT_NICHE_SUBREDDITS) >= 5


# ---------- registry auto-discovery ----------


def test_reddit_collector_registered():
    from src.collectors.registry import CollectorRegistry

    r = CollectorRegistry()
    r.discover()
    assert "reddit" in r.available()
    assert r.get("reddit") is RedditCollector

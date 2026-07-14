"""Tests for the per-domain rate limiter."""
from __future__ import annotations

import asyncio
import time

import pytest

from src.utils.rate_limit import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_default_capacity():
    limiter = RateLimiter(default_rate=10.0, default_burst=5)
    # First 5 should be immediate (burst)
    t0 = time.monotonic()
    for _ in range(5):
        await limiter.acquire("api.example.com")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"burst should be near-instant, got {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_rate_limiter_throttles():
    limiter = RateLimiter(default_rate=10.0, default_burst=2, jitter_pct=0.0)
    t0 = time.monotonic()
    # 2 burst + 2 more = 4 total at 10/s, so ~0.2s for the 3rd and 4th
    for _ in range(4):
        await limiter.acquire("api.example.com")
    elapsed = time.monotonic() - t0
    assert 0.15 < elapsed < 0.5, f"expected ~0.2s, got {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_rate_limiter_separate_hosts():
    limiter = RateLimiter(default_rate=10.0, default_burst=2, jitter_pct=0.0)
    # First 2 on host A, then 2 on host B should each be fast
    t0 = time.monotonic()
    for _ in range(2):
        await limiter.acquire("api.a.com")
    for _ in range(2):
        await limiter.acquire("api.b.com")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3, f"different hosts shouldn't share a bucket, got {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_rate_limiter_retry_after():
    limiter = RateLimiter(default_rate=10.0, default_burst=1, jitter_pct=0.0)
    # Consume the burst
    await limiter.acquire("api.example.com")
    # Apply 0.5s retry-after
    limiter.apply_retry_after("api.example.com", 0.5)
    t0 = time.monotonic()
    await limiter.acquire("api.example.com")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.45, f"Retry-After should hold the bucket, got {elapsed:.2f}s"


def test_host_from_url():
    assert RateLimiter.host_from_url("https://api.x.com/2/trends/by/woeid/1") == "api.x.com"
    assert RateLimiter.host_from_url("api.x.com") == "api.x.com"
    assert RateLimiter.host_from_url("http://localhost:8000/foo") == "localhost:8000"

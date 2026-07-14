"""Per-domain token-bucket rate limiter with jitter and Retry-After support.

A single RateLimiter instance owns a separate bucket per hostname. Each
acquire() returns once the bucket has capacity; if a Retry-After hint is
known, the next acquire waits that long instead.

Designed for `httpx` async clients. Thread-safe within a single event loop.
"""
from __future__ import annotations

import asyncio
import structlog
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = structlog.get_logger(__name__)


@dataclass
class _Bucket:
    """One token bucket per host."""

    capacity: float
    refill_rate: float  # tokens per second
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # External cooldown (e.g. from Retry-After header). None = no override.
    cooldown_until: float | None = None

    def __post_init__(self) -> None:
        self.tokens = self.capacity  # start full

    async def acquire(self, jitter_pct: float) -> None:
        """Wait until 1 token is available, then consume it."""
        async with self.lock:
            # Cooldown gate (Retry-After)
            now = time.monotonic()
            if self.cooldown_until is not None:
                wait = self.cooldown_until - now
                if wait > 0:
                    logger.debug("rate_limit.cooldown", wait_s=round(wait, 2))
                    await asyncio.sleep(wait)
                self.cooldown_until = None

            # Refill
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return

            # Need to wait for a token
            deficit = 1.0 - self.tokens
            base_wait = deficit / self.refill_rate
            jitter = base_wait * jitter_pct * (2 * random.random() - 1)
            wait = max(0.0, base_wait + jitter)

        # Sleep outside the lock so other waiters can also wake up
        await asyncio.sleep(wait)
        # Re-enter to consume
        async with self.lock:
            # Recompute (someone else may have taken the token)
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            if self.tokens < 1.0:
                # Extremely rare: re-enter and wait again
                deficit = 1.0 - self.tokens
                base_wait = deficit / self.refill_rate
                jitter = base_wait * jitter_pct * (2 * random.random() - 1)
                wait = max(0.0, base_wait + jitter)
                # Release lock for sleep
                logger.debug("rate_limit.reenter_wait", wait_s=round(wait, 3))
            self.tokens = max(0.0, self.tokens - 1.0)

    def apply_retry_after(self, seconds: float) -> None:
        """Apply a server-provided Retry-After hint to future acquires."""
        if seconds <= 0:
            return
        with_time = time.monotonic() + seconds
        # Coalesce with existing cooldown
        if self.cooldown_until is None or with_time > self.cooldown_until:
            self.cooldown_until = with_time
            logger.warning("rate_limit.retry_after", seconds=seconds)

    def snapshot(self) -> dict:
        return {
            "tokens": round(self.tokens, 3),
            "capacity": self.capacity,
            "refill_rate": self.refill_rate,
            "cooldown_until": self.cooldown_until,
        }


class RateLimiter:
    """Per-host token-bucket rate limiter with jitter and Retry-After support.

    Usage:
        limiter = RateLimiter(default_rate=0.2, default_burst=5, jitter_pct=0.5)
        limiter.set_host_rate("api.x.com", rate=1.0, burst=10)
        async with httpx.AsyncClient() as client:
            for url in urls:
                await limiter.acquire("api.x.com")
                resp = await client.get(url)
                if resp.status_code == 429:
                    limiter.apply_retry_after(url, resp.headers.get("Retry-After"))
    """

    def __init__(
        self,
        default_rate: float = 0.2,
        default_burst: int = 5,
        jitter_pct: float = 0.5,
    ) -> None:
        self.default_rate = default_rate
        self.default_burst = default_burst
        self.jitter_pct = max(0.0, min(1.0, jitter_pct))
        self._buckets: dict[str, _Bucket] = {}
        self._configs: dict[str, tuple[float, int]] = {}

    def set_host_rate(self, host: str, rate: float, burst: int | None = None) -> None:
        """Configure rate/burst for a specific host. host should be a bare
        hostname (no scheme, no path) — use the helper to extract from a URL."""
        if burst is None:
            burst = max(1, int(rate * 10))
        self._configs[host] = (rate, burst)
        # Recreate bucket if it exists
        if host in self._buckets:
            self._buckets[host] = _Bucket(capacity=burst, refill_rate=rate)

    @staticmethod
    def host_from_url(url: str) -> str:
        # Cheap extraction; no need to import urllib for a simple split
        if "://" in url:
            url = url.split("://", 1)[1]
        return url.split("/", 1)[0].lower()

    def _get_bucket(self, host: str) -> _Bucket:
        if host not in self._buckets:
            rate, burst = self._configs.get(host, (self.default_rate, self.default_burst))
            self._buckets[host] = _Bucket(capacity=burst, refill_rate=rate)
        return self._buckets[host]

    async def acquire(self, host_or_url: str) -> None:
        host = (
            self.host_from_url(host_or_url)
            if "://" in host_or_url or "/" in host_or_url
            else host_or_url
        )
        bucket = self._get_bucket(host)
        await bucket.acquire(self.jitter_pct)

    def apply_retry_after(self, host_or_url: str, retry_after: str | float | None) -> None:
        if retry_after is None:
            return
        host = (
            self.host_from_url(host_or_url)
            if "://" in host_or_url or "/" in host_or_url
            else host_or_url
        )
        try:
            seconds = float(retry_after)
        except (TypeError, ValueError):
            return
        bucket = self._get_bucket(host)
        bucket.apply_retry_after(seconds)

    def snapshot(self) -> dict[str, dict]:
        return {host: b.snapshot() for host, b in self._buckets.items()}

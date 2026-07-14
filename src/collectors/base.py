"""Abstract base class for platform collectors.

Each platform is a single subclass that knows:
- Its platform identifier
- How to talk to that platform's public data source
- How to map the platform's response into list[Trend]

Collectors are async, rate-limit themselves via the injected limiter,
and return clean normalized data. They do not touch storage or scoring.
"""
from __future__ import annotations

import structlog
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import httpx

from ..normalizer.schema import Trend

if TYPE_CHECKING:
    from ..utils.rate_limit import RateLimiter

logger = structlog.get_logger(__name__)


# Default User-Agent. Honest identification. Update with real contact info
# when you publish this project. The platforms can and do block generic
# scraper UAs.
DEFAULT_USER_AGENT = (
    "SocialTrendMonitor/0.1 "
    "(+https://github.com/DaddyClint82/social-trend-monitor; "
    "ethical-public-data-collection)"
)


class BaseCollector(ABC):
    """Subclass this to add a new platform.

    Required:
        platform: str  — must be one of the canonical PLATFORMS

        async def collect(self) -> list[Trend]: ...

    Optional overrides:
        user_agent: str
        timeout_s: float
    """

    platform: str = ""  # subclasses MUST set
    user_agent: str = DEFAULT_USER_AGENT
    timeout_s: float = 30.0

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        rate_limiter: "RateLimiter",
        config: dict | None = None,
    ) -> None:
        if not self.platform:
            raise ValueError(
                f"{type(self).__name__} must set class attribute `platform`"
            )
        self.http = http_client
        self.limiter = rate_limiter
        self.config = config or {}

    @abstractmethod
    async def collect(self) -> list[Trend]:
        """Fetch and normalize the latest trends from this platform.

        Implementations must:
        - Acquire rate-limit tokens via self.limiter.acquire(host)
        - Return list[Trend] (possibly empty)
        - Never raise on transient errors; log and return [] instead
        - On hard failures, raise — the orchestrator will mark the run as
          failed for this platform but other collectors will continue
        """
        raise NotImplementedError

    # ---- helpers subclasses can use ----

    async def get_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> dict | list | None:
        """Rate-limited GET that returns parsed JSON.

        Returns None on non-2xx, logs the status. Handles 429 by applying
        Retry-After to the limiter.
        """
        await self.limiter.acquire(url)
        merged_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        try:
            resp = await self.http.get(
                url, params=params, headers=merged_headers, timeout=self.timeout_s
            )
        except httpx.HTTPError as e:
            logger.warning("collector.http_error", platform=self.platform, url=url, error=str(e))
            return None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            self.limiter.apply_retry_after(url, retry_after)
            logger.warning(
                "collector.rate_limited",
                platform=self.platform,
                url=url,
                retry_after=retry_after,
            )
            return None

        if resp.status_code >= 400:
            logger.warning(
                "collector.http_status",
                platform=self.platform,
                url=url,
                status=resp.status_code,
            )
            return None

        try:
            return resp.json()
        except (ValueError, TypeError) as e:
            logger.warning("collector.parse_error", platform=self.platform, error=str(e))
            return None

    async def get_text(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> str | None:
        """Rate-limited GET that returns response text.

        For non-JSON endpoints (RSS, HTML scraping). Returns None on non-2xx.
        Caller is responsible for parsing. Handles 429 via limiter.
        """
        await self.limiter.acquire(url)
        merged_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/xml, text/xml, application/rss+xml, */*",
        }
        if headers:
            merged_headers.update(headers)
        try:
            resp = await self.http.get(
                url, params=params, headers=merged_headers, timeout=self.timeout_s
            )
        except httpx.HTTPError as e:
            logger.warning("collector.http_error", platform=self.platform, url=url, error=str(e))
            return None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            self.limiter.apply_retry_after(url, retry_after)
            logger.warning(
                "collector.rate_limited",
                platform=self.platform,
                url=url,
                retry_after=retry_after,
            )
            return None

        if resp.status_code >= 400:
            logger.warning(
                "collector.http_status",
                platform=self.platform,
                url=url,
                status=resp.status_code,
            )
            return None

        return resp.text

"""Unified Trend schema.

Every collector returns list[Trend]. Downstream code never sees a
platform-native object. This is the single contract between collection
and the rest of the system.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# Canonical platform identifiers. Add a new one when you add a collector.
# - "apify" is a vendor bridge; trends it produces carry their source platform
#   in metadata["source_platform"] so cross-platform grouping still works.
PLATFORMS: tuple[str, ...] = (
    "tiktok",
    "x",
    "instagram",
    "facebook",
    "reddit",
    "apify",
    "google_trends",  # Free RSS feed. No auth, no rate limit. ADR-0013.
    "youtube",        # Free trending HTML scrape. No auth. ADR-0013.
)
TREND_TYPES: tuple[str, ...] = (
    "hashtag",
    "sound",
    "topic",
    "format",
    "creator",
    "video",
    "subreddit",
    "post",
    "search",  # Google Trends topic. ADR-0013.
)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _normalize_name(name: str) -> str:
    """Normalize a trend name for cross-platform joining.

    - Strip leading '#' or '@'
    - Lowercase
    - Collapse whitespace
    - Strip surrounding whitespace
    """
    n = name.strip()
    if n.startswith("#") or n.startswith("@"):
        n = n[1:]
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


def make_cross_platform_key(platform: str, name: str) -> str:
    """Generate a stable cross-platform join key.

    The key is platform-specific on purpose — exact matches across
    platforms happen in the normalizer, not in this function. Downstream
    code that wants cross-platform trends should group on the *normalized*
    name, not the key itself.
    """
    norm = _normalize_name(name)
    return f"{platform}::{norm}"


def make_trend_id(platform: str, platform_native_id: str) -> str:
    """Stable primary key in the trends table."""
    h = hashlib.sha1(platform_native_id.encode("utf-8")).hexdigest()[:16]
    return f"{platform}:{h}"


@dataclass
class TrendSignal:
    """A single observation of a trend's metric.

    The collector writes one signal per collection cycle. The scorer reads
    the history of signals to compute velocity and decay.
    """

    captured_at: datetime
    score: float  # platform-native score (e.g. tweet count, post count)
    rank: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["captured_at"] = self.captured_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrendSignal:
        d = dict(d)
        if isinstance(d.get("captured_at"), str):
            d["captured_at"] = datetime.fromisoformat(d["captured_at"])
        return cls(**d)


@dataclass
class Trend:
    """A single trending entity from one platform.

    A hashtag, a sound, a creator — anything the platform calls "trending".
    """

    id: str
    platform: str
    name: str
    trend_type: str
    url: str | None
    first_seen: datetime
    last_seen: datetime
    score: float
    signals: list[TrendSignal] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    cross_platform_key: str | None = None

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError(
                f"unknown platform {self.platform!r}; expected one of {PLATFORMS}"
            )
        if self.trend_type not in TREND_TYPES:
            raise ValueError(
                f"unknown trend_type {self.trend_type!r}; expected one of {TREND_TYPES}"
            )
        if self.cross_platform_key is None:
            self.cross_platform_key = make_cross_platform_key(self.platform, self.name)

    @property
    def normalized_name(self) -> str:
        return _normalize_name(self.name)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["first_seen"] = self.first_seen.isoformat()
        d["last_seen"] = self.last_seen.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Trend:
        d = dict(d)
        for k in ("first_seen", "last_seen"):
            if isinstance(d.get(k), str):
                d[k] = datetime.fromisoformat(d[k])
        d["signals"] = [TrendSignal.from_dict(s) for s in d.get("signals", [])]
        return cls(**d)


def make_trend(
    *,
    platform: str,
    name: str,
    trend_type: str,
    platform_native_id: str,
    url: str | None,
    score: float,
    metadata: dict[str, Any] | None = None,
) -> Trend:
    """Convenience constructor.

    Generates id, first/last_seen, and cross_platform_key automatically.
    Use this in collectors.
    """
    now = _utcnow()
    return Trend(
        id=make_trend_id(platform, platform_native_id),
        platform=platform,
        name=name,
        trend_type=trend_type,
        url=url,
        first_seen=now,
        last_seen=now,
        score=score,
        metadata=metadata or {},
    )

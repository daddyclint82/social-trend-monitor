"""Trend scoring: velocity, cross-platform bonus, decay.

Pure functions over Trend history. No I/O. The storage layer provides
the history; the scorer turns it into a single float per trend.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from ..normalizer.schema import Trend

# ---- velocity ----


def velocity(history: list[dict], now: datetime | None = None) -> float:
    """Rate of change of score.

    history: list of {captured_at, score} dicts, oldest-first.
    Returns a unitless velocity score. Positive = growing, negative = dying.
    """
    if len(history) < 2:
        return 0.0
    now = now or datetime.now(tz=timezone.utc)
    # Use first and last in window
    first = history[0]
    last = history[-1]
    try:
        t0 = datetime.fromisoformat(first["captured_at"])
        t1 = datetime.fromisoformat(last["captured_at"])
    except (KeyError, ValueError):
        return 0.0
    hours = max(0.1, (t1 - t0).total_seconds() / 3600.0)
    delta = float(last["score"]) - float(first["score"])
    if delta <= 0 or float(first["score"]) <= 0:
        # Use ratio against most recent non-zero baseline
        base = max(1.0, float(last["score"]))
        return math.tanh(delta / base)
    growth_ratio = delta / float(first["score"])
    return math.tanh(growth_ratio / max(1.0, hours / 24.0))


# ---- cross-platform bonus ----


def cross_platform_groups(trends: list[Trend]) -> dict[str, list[Trend]]:
    """Group trends by normalized name. Returns dict[normalized_name -> [Trend]]."""
    groups: dict[str, list[Trend]] = defaultdict(list)
    for t in trends:
        groups[t.normalized_name].append(t)
    return groups


def cross_platform_bonus(normalized_name: str, all_trends: list[Trend]) -> float:
    """Bonus for trends that appear on multiple platforms.

    Returns a multiplier in [1.0, 1.5]. The bonus applies if the same
    normalized name appears on 2+ distinct platforms.
    """
    platforms = {t.platform for t in all_trends if t.normalized_name == normalized_name}
    n = len(platforms)
    if n <= 1:
        return 1.0
    return min(1.5, 1.0 + 0.15 * (n - 1))


# ---- decay ----


def decay(last_seen: datetime, now: datetime | None = None, half_life_h: float = 72.0) -> float:
    """Exponential decay — trends lose score the longer they go without
    being seen. half_life_h: hours until score halves."""
    now = now or datetime.now(tz=timezone.utc)
    hours = max(0.0, (now - last_seen).total_seconds() / 3600.0)
    if hours <= 0:
        return 1.0
    return 0.5 ** (hours / half_life_h)


# ---- top-level ----


def score(
    trend: Trend,
    history: list[dict],
    all_trends: list[Trend],
    now: datetime | None = None,
    normalized_base: float | None = None,
) -> float:
    """Compute the final score for a trend.

    score = base * decay * cross_platform_bonus * (1 + velocity)

    `base` defaults to the raw `trend.score` for backward compat, but
    callers should pass `normalized_base` (from `scoring.normalize.normalize_trends`)
    to make cross-platform ranking meaningful. With raw scores, a
    YouTube 100K-view video (score=100000) outranks a Google Trends
    peak (score=6) trivially. With normalized_base, both are in [0, 1].
    """
    now = now or datetime.now(tz=timezone.utc)
    base = float(normalized_base) if normalized_base is not None else float(trend.score)
    d = decay(trend.last_seen, now=now)
    bonus = cross_platform_bonus(trend.normalized_name, all_trends)
    v = velocity(history, now=now)
    return base * d * bonus * (1.0 + v)

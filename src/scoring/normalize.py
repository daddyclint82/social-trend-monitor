"""Per-platform score normalization (Phase 0.1 — Trend Aggregator Improvement Ledger).

Problem:
    Each platform uses a different score scale:
    - google_trends: 1..6 (traffic bucket index)
    - youtube: 0..billions (raw view count)
    - tiktok: 0 (no native signal in oEmbed/discover collectors)
    - x: 0..millions (tweet_volume)
    - apify: 0..thousands (per-actor result count)

    Comparing a "1000+" Google Trends peak (score=6) with a "100K views"
    YouTube video (score=100000) is apples-to-oranges. Cross-platform
    ranking needs a normalized scale.

Solution: per-platform median + MAD → robust z-score → [0, 1] sigmoid.
    - Median + MAD is robust to outliers (a single viral 1B-view video
      doesn't crush the scale).
    - Sigmoid bounds the output to a comparable range across platforms.
    - A trend's normalized score is "how far above the platform's
      median is this trend, in MAD units, mapped to a 0..1 scale".

Output:
    For each trend, we expose:
    - normalized_score: float in [0, 1]   # cross-platform comparable
    - z_score: float (positive = above median)  # raw MAD-based
    - platform_stats: dict[platform, {median, mad, n, ...}]  # the basis

This is **pure functions over Trend lists** — no I/O. The CLI / API
layers feed it the trend list and the storage pulls platform stats
from prior cycles for stability across runs.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Iterable

from ..normalizer.schema import Trend


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def _mad(values: list[float], med: float) -> float:
    """Median Absolute Deviation — robust measure of spread.

    MAD is the median of |x_i - median|. It is robust to outliers
    (a single viral hit doesn't blow up the scale). For a normal
    distribution, MAD ≈ 0.6745 * sigma, so we multiply by 1.4826 to
    make it a consistent estimator of sigma. With <5 samples we
    skip the multiplier and use raw MAD to avoid underflow.
    """
    if not values:
        return 0.0
    deviations = [abs(v - med) for v in values]
    raw = float(statistics.median(deviations))
    if len(values) < 5:
        return max(raw, 1e-6)
    return max(raw * 1.4826, 1e-6)


def compute_platform_stats(trends: Iterable[Trend]) -> dict[str, dict[str, float]]:
    """Compute median + MAD per platform from a list of trends.

    Returns dict[platform, {median, mad, n, min, max}] so callers
    can either reuse it for normalization or surface it in the
    read API as "how does this trend compare to the platform average".
    """
    by_platform: dict[str, list[float]] = defaultdict(list)
    for t in trends:
        by_platform[t.platform].append(float(t.score))
    out: dict[str, dict[str, float]] = {}
    for platform, scores in by_platform.items():
        med = _median(scores)
        mad = _mad(scores, med)
        out[platform] = {
            "median": med,
            "mad": mad,
            "n": float(len(scores)),
            "min": min(scores) if scores else 0.0,
            "max": max(scores) if scores else 0.0,
        }
    return out


def _robust_z(value: float, median: float, mad: float) -> float:
    """Modified z-score using MAD: z = 0.6745 * (x - median) / MAD.

    Output is in MAD units. Positive = above median. For a normal
    distribution, |z| > 3.5 is an outlier. Our usage is comparative,
    not statistical, so the absolute scale matters less than the
    relative ranking within a platform.
    """
    if mad <= 0:
        return 0.0
    return 0.6745 * (value - median) / mad


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid mapping (-inf, inf) -> (0, 1).

    0 maps to 0.5 (at-the-median). 1 maps to ~0.73. 2 -> ~0.88. -1 -> ~0.27.
    We pick a soft cap at z=2 to avoid one viral outlier dominating.
    """
    # Clip extreme values to avoid overflow
    x = max(-6.0, min(6.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def normalize_score(
    value: float, median: float, mad: float
) -> tuple[float, float]:
    """Return (normalized_0_1, z_score) for one value.

    The normalized score is interpretable: 0.5 = exactly median,
    0.73 = 1 MAD above median, 0.88 = 2 MAD above (very high).
    """
    z = _robust_z(value, median, mad)
    return _sigmoid(z), z


def normalize_trends(
    trends: list[Trend],
    stats: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of dicts with original Trend fields + normalized values.

    Each result:
        trend: the original Trend object
        normalized_score: float in [0, 1]
        z_score: float (raw MAD-based)
        rank_within_platform: int (1 = highest z-score on this platform)
        platform_stats: {median, mad, n, min, max} for context

    If `stats` is None, they're computed from `trends` (in-memory).
    Pass a pre-computed stats dict for stable normalization across
    multiple queries.
    """
    if stats is None:
        stats = compute_platform_stats(trends)
    out: list[dict[str, Any]] = []
    # First pass: compute z per trend
    per_platform_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trends:
        ps = stats.get(t.platform)
        if ps is None:
            # No stats for this platform — emit with neutral 0.5
            norm, z = 0.5, 0.0
            ps_out = {"median": 0.0, "mad": 0.0, "n": 0.0, "min": 0.0, "max": 0.0}
        else:
            norm, z = normalize_score(float(t.score), ps["median"], ps["mad"])
            ps_out = ps
        item = {
            "trend": t,
            "normalized_score": norm,
            "z_score": z,
            "platform_stats": ps_out,
        }
        out.append(item)
        per_platform_items[t.platform].append(item)
    # Second pass: rank within platform (1 = highest z)
    for platform, items in per_platform_items.items():
        items.sort(key=lambda x: x["z_score"], reverse=True)
        for rank, item in enumerate(items, start=1):
            item["rank_within_platform"] = rank
    return out

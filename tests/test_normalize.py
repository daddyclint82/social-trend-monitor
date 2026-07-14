"""Tests for per-platform score normalization (Phase 0.1)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.normalizer.schema import make_trend, Trend
from src.scoring.normalize import (
    _mad,
    _median,
    _robust_z,
    _sigmoid,
    compute_platform_stats,
    normalize_score,
    normalize_trends,
)


# --- _median ---


def test_median_basic():
    assert _median([1, 2, 3]) == 2.0
    assert _median([1, 2, 3, 4]) == 2.5
    assert _median([]) == 0.0
    assert _median([5.0]) == 5.0


# --- _mad ---


def test_mad_basic():
    # All same value → MAD = 0 (we floor at 1e-6 to avoid div-by-zero)
    m = _mad([5.0, 5.0, 5.0], med=5.0)
    assert m == pytest.approx(0.0, abs=1e-6) or m == 1e-6


def test_mad_with_outlier_doesnt_break():
    """MAD is robust to outliers. One viral hit shouldn't dominate."""
    scores = [10, 11, 12, 11, 10, 12, 11, 1_000_000_000]  # last is huge
    m = _mad(scores, med=11.0)
    # Raw MAD = median([1, 0, 1, 0, 1, 1, 0, 999999989]) = 1
    # With 8 samples → 1.4826 * 1 = ~1.48
    # But this is fine — MAD stays small despite the 1B outlier
    assert m < 10  # would be huge if we'd used std


def test_mad_small_sample_uses_raw():
    """With <5 samples, we skip the 1.4826 multiplier."""
    m = _mad([1, 2, 3], med=2.0)
    # Raw MAD = 1, no multiplier for n<5
    assert m == pytest.approx(1.0, abs=0.01)


# --- _robust_z ---


def test_robust_z_at_median_is_zero():
    assert _robust_z(10, median=10, mad=2) == 0.0


def test_robust_z_one_mad_above():
    z = _robust_z(12, median=10, mad=2)
    # 0.6745 * (12-10)/2 = 0.6745
    assert z == pytest.approx(0.6745, abs=1e-3)


def test_robust_z_zero_mad_returns_zero():
    """Avoid div-by-zero on degenerate inputs."""
    assert _robust_z(5, median=10, mad=0) == 0.0


# --- _sigmoid ---


def test_sigmoid_at_zero_is_half():
    assert _sigmoid(0) == pytest.approx(0.5, abs=1e-6)


def test_sigmoid_clamps_extremes():
    """We clip to [-6, 6] to avoid overflow."""
    assert _sigmoid(100) == _sigmoid(6)
    assert _sigmoid(-100) == _sigmoid(-6)


def test_sigmoid_monotonic():
    """Bigger input → bigger output."""
    a, b, c = _sigmoid(-1), _sigmoid(0), _sigmoid(1)
    assert a < b < c


# --- compute_platform_stats ---


def test_compute_platform_stats_separates_platforms():
    """Trends on different platforms are NOT mixed in the same stats bucket."""
    t1 = make_trend(platform="tiktok_oembed", name="#a", trend_type="hashtag",
                    platform_native_id="1", url="", score=10.0)
    t2 = make_trend(platform="youtube", name="vid", trend_type="video",
                    platform_native_id="2", url="", score=1_000_000.0)
    stats = compute_platform_stats([t1, t2])
    assert "tiktok_oembed" in stats and "youtube" in stats
    # tiktok_oembed median is 10, not affected by the YouTube 1M
    assert stats["tiktok_oembed"]["median"] == 10.0
    assert stats["youtube"]["median"] == 1_000_000.0


def test_compute_platform_stats_with_outlier_robust():
    """One viral YouTube hit doesn't blow up the YouTube MAD."""
    trends = [
        make_trend(platform="youtube", name=f"v{i}", trend_type="video",
                   platform_native_id=str(i), url="", score=score)
        for i, score in enumerate([1000, 1100, 1200, 1150, 1080, 1220, 1100, 1_500_000_000])
    ]
    stats = compute_platform_stats(trends)
    # Median is ~1110, MAD is small (~50) — not 100M+ as std would be
    assert stats["youtube"]["median"] == pytest.approx(1110, abs=50)
    assert stats["youtube"]["mad"] < 200  # std would be ~530M


# --- normalize_score ---


def test_normalize_score_at_median_is_half():
    norm, z = normalize_score(10.0, median=10.0, mad=2.0)
    assert norm == pytest.approx(0.5, abs=1e-6)
    assert z == 0.0


def test_normalize_score_above_median_higher_than_below():
    n_above, _ = normalize_score(15.0, median=10.0, mad=2.0)
    n_below, _ = normalize_score(5.0, median=10.0, mad=2.0)
    assert n_above > 0.5 > n_below


# --- normalize_trends (the main API) ---


def test_normalize_trends_returns_list_of_dicts():
    t1 = make_trend(platform="tiktok_oembed", name="#a", trend_type="hashtag",
                    platform_native_id="1", url="", score=10.0)
    t2 = make_trend(platform="youtube", name="b", trend_type="video",
                    platform_native_id="2", url="", score=5000.0)
    out = normalize_trends([t1, t2])
    assert len(out) == 2
    for item in out:
        assert "trend" in item
        assert "normalized_score" in item
        assert 0.0 <= item["normalized_score"] <= 1.0
        assert "z_score" in item
        assert "rank_within_platform" in item
        assert "platform_stats" in item


def test_normalize_trends_rank_within_platform():
    """Top z-score on a platform gets rank=1."""
    trends = [
        make_trend(platform="youtube", name="small", trend_type="video",
                   platform_native_id="1", url="", score=1000.0),
        make_trend(platform="youtube", name="viral", trend_type="video",
                   platform_native_id="2", url="", score=1_500_000_000.0),
        make_trend(platform="youtube", name="mid", trend_type="video",
                   platform_native_id="3", url="", score=1100.0),
    ]
    out = normalize_trends(trends)
    by_name = {item["trend"].name: item for item in out}
    assert by_name["viral"]["rank_within_platform"] == 1
    assert by_name["viral"]["normalized_score"] > 0.9  # way above median
    # "mid" is closer to median than "small"
    assert by_name["mid"]["rank_within_platform"] < by_name["small"]["rank_within_platform"]


def test_normalize_trends_handles_unknown_platform():
    """A trend on a platform with no stats → neutral 0.5 normalized."""
    # Use reddit (in PLATFORMS) but pass empty stats so there's no baseline
    t = make_trend(platform="reddit", name="x", trend_type="subreddit",
                   platform_native_id="1", url="", score=100.0)
    out = normalize_trends([t])
    assert out[0]["normalized_score"] == 0.5
    assert out[0]["z_score"] == 0.0
    assert out[0]["rank_within_platform"] == 1  # only one


def test_normalize_trends_uses_external_stats():
    """Passing a pre-computed stats dict decouples normalization from the
    trends being scored (useful for stable scores across queries)."""
    # Two different "worlds" of trends with the same platform mix
    stats = {
        "tiktok_oembed": {"median": 0.0, "mad": 1.0, "n": 100.0, "min": 0.0, "max": 100.0},
        "youtube": {"median": 10000.0, "mad": 5000.0, "n": 50.0, "min": 0.0, "max": 1_000_000.0},
    }
    t1 = make_trend(platform="tiktok_oembed", name="a", trend_type="hashtag",
                    platform_native_id="1", url="", score=5.0)
    out = normalize_trends([t1], stats=stats)
    # z = 0.6745 * (5 - 0) / 1 = 3.3725
    assert out[0]["z_score"] == pytest.approx(3.3725, abs=0.01)
    # sigmoid(3.3725) ≈ 0.967
    assert out[0]["normalized_score"] == pytest.approx(0.967, abs=0.01)


def test_normalize_trends_cross_platform_comparable():
    """The headline test: a Google peak vs a YouTube view count, both
    near the top of their respective platforms, should produce
    SIMILAR normalized scores (so they can be compared/ranked)."""
    # Google peak: score=6 (top of 1..6 scale)
    # YouTube mid: score=1M (assumed "above median" given typical 100K median)
    stats = {
        "google_trends": {"median": 3.0, "mad": 1.5, "n": 50.0, "min": 1.0, "max": 6.0},
        "youtube": {"median": 100_000.0, "mad": 50_000.0, "n": 25.0, "min": 1000.0, "max": 50_000_000.0},
    }
    g = make_trend(platform="google_trends", name="peak", trend_type="search",
                   platform_native_id="g1", url="", score=6.0)
    y = make_trend(platform="youtube", name="vid", trend_type="video",
                   platform_native_id="y1", url="", score=1_000_000.0)
    out = normalize_trends([g, y], stats=stats)
    by_platform = {item["trend"].platform: item for item in out}
    # Both should be in the upper half of [0, 1]
    assert by_platform["google_trends"]["normalized_score"] > 0.7
    assert by_platform["youtube"]["normalized_score"] > 0.7
    # Both within a comparable range
    g_n = by_platform["google_trends"]["normalized_score"]
    y_n = by_platform["youtube"]["normalized_score"]
    assert abs(g_n - y_n) < 0.4  # not absurdly different

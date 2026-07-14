"""Tests for the scoring engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.normalizer.schema import make_trend
from src.scoring.engine import (
    cross_platform_bonus,
    cross_platform_groups,
    decay,
    score,
    velocity,
)


def test_velocity_growing():
    now = datetime.now(tz=timezone.utc)
    history = [
        {"captured_at": (now - timedelta(days=1)).isoformat(), "score": 100},
        {"captured_at": now.isoformat(), "score": 200},
    ]
    v = velocity(history, now=now)
    assert v > 0


def test_velocity_declining():
    now = datetime.now(tz=timezone.utc)
    history = [
        {"captured_at": (now - timedelta(days=1)).isoformat(), "score": 200},
        {"captured_at": now.isoformat(), "score": 100},
    ]
    v = velocity(history, now=now)
    assert v <= 0


def test_velocity_short_history():
    assert velocity([{"captured_at": "x", "score": 1}], now=None) == 0.0
    assert velocity([], now=None) == 0.0


def test_decay_zero_when_fresh():
    now = datetime.now(tz=timezone.utc)
    assert decay(now, now=now) == 1.0


def test_decay_halves_at_half_life():
    now = datetime.now(tz=timezone.utc)
    then = now - timedelta(hours=72)
    assert 0.45 < decay(then, now=now, half_life_h=72.0) < 0.55


def test_cross_platform_groups():
    t1 = make_trend(platform="tiktok", name="#AIart", trend_type="hashtag",
                    platform_native_id="1", url=None, score=100)
    t2 = make_trend(platform="x", name="#aiart", trend_type="topic",
                    platform_native_id="2", url=None, score=200)
    t3 = make_trend(platform="tiktok", name="#cats", trend_type="hashtag",
                    platform_native_id="3", url=None, score=50)
    groups = cross_platform_groups([t1, t2, t3])
    assert "aiart" in groups
    assert len(groups["aiart"]) == 2
    assert "cats" in groups
    assert len(groups["cats"]) == 1


def test_cross_platform_bonus_solo():
    t = make_trend(platform="tiktok", name="x", trend_type="topic",
                   platform_native_id="1", url=None, score=1)
    assert cross_platform_bonus("x", [t]) == 1.0


def test_cross_platform_bonus_multi():
    t1 = make_trend(platform="tiktok", name="aiart", trend_type="topic",
                    platform_native_id="1", url=None, score=1)
    t2 = make_trend(platform="x", name="aiart", trend_type="topic",
                    platform_native_id="2", url=None, score=1)
    bonus = cross_platform_bonus("aiart", [t1, t2])
    assert bonus > 1.0
    assert bonus <= 1.5


def test_score_combines_factors():
    now = datetime.now(tz=timezone.utc)
    t = make_trend(platform="tiktok", name="aiart", trend_type="topic",
                   platform_native_id="1", url=None, score=100.0)
    history = [
        {"captured_at": (now - timedelta(days=1)).isoformat(), "score": 50},
        {"captured_at": now.isoformat(), "score": 100},
    ]
    s = score(t, history, all_trends=[t], now=now)
    # score = 100 * 1.0 (fresh) * 1.0 (solo) * (1 + positive_velocity)
    assert s > 100

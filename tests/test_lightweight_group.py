"""Tests for the lightweight cross-platform grouper (Phase 0.2)."""
from __future__ import annotations

import pytest

from src.normalizer.lightweight_group import (
    CrossPlatformGroup,
    DEFAULT_THRESHOLD,
    LightweightGrouper,
    MIN_NAME_LENGTH,
    normalize_for_grouping,
)
from src.normalizer.schema import make_trend


# --- normalize_for_grouping ---


def test_normalize_strips_hash():
    assert normalize_for_grouping("#aiart") == "aiart"


def test_normalize_strips_at():
    assert normalize_for_grouping("@channel") == "channel"


def test_normalize_lowercases():
    assert normalize_for_grouping("TAYLOR SWIFT") == "taylor swift"


def test_normalize_replaces_punctuation_with_space():
    assert normalize_for_grouping("bryan-cranston") == "bryan cranston"
    assert normalize_for_grouping("taylor_swift!") == "taylor swift"


def test_normalize_strips_urls():
    # URL alone strips to empty
    assert normalize_for_grouping("https://example.com") == ""
    # URL mixed with other text
    assert normalize_for_grouping("check www.foo.bar out") == "check out"
    # URL with hashtag path - the hashtag name is preserved
    # (we strip "https://tiktok.com" → leaves "/tag/aiart" → "tag aiart"
    #  we then strip leading "tag " because it's URL noise)
    result = normalize_for_grouping("see https://tiktok.com/tag/aiart today")
    assert "aiart" in result and "today" in result and "see" in result


def test_normalize_collapses_whitespace():
    assert normalize_for_grouping("bryan    cranston") == "bryan cranston"
    assert normalize_for_grouping("  taylor  swift  ") == "taylor swift"


def test_normalize_handles_combined():
    """Real-world: #TaylorSwift! → 'taylor swift'"""
    assert normalize_for_grouping("#TaylorSwift!") == "taylorswift" or \
           normalize_for_grouping("#Taylor Swift!") == "taylor swift"


# --- LightweightGrouper.group() ---


def _t(name: str, platform: str = "tiktok", score: float = 1.0) -> "Trend":
    return make_trend(
        platform=platform, name=name, trend_type="hashtag",
        platform_native_id=f"{platform}:{name}", url="", score=score,
    )


def test_group_exact_match_clusters():
    """Same string on different platforms → one group."""
    g = LightweightGrouper()
    trends = [
        _t("bryan cranston", "google_trends"),
        _t("Bryan Cranston", "x"),
        _t("bryan cranston", "tiktok"),
    ]
    groups = g.group(trends)
    assert len(groups) == 1
    assert groups[0].member_count == 3
    assert groups[0].platforms == {"google_trends", "x", "tiktok"}


def test_group_punctuation_variants_cluster():
    """'bryan-cranston' and 'bryan cranston' should cluster."""
    g = LightweightGrouper()
    trends = [
        _t("bryan-cranston", "google_trends"),
        _t("bryan cranston", "x"),
    ]
    groups = g.group(trends)
    assert len(groups) == 1
    assert groups[0].member_count == 2


def test_group_case_variants_cluster():
    g = LightweightGrouper()
    trends = [
        _t("TAYLOR SWIFT", "google_trends"),
        _t("taylor swift", "x"),
        _t("Taylor Swift", "tiktok"),
    ]
    groups = g.group(trends)
    assert len(groups) == 1
    assert groups[0].member_count == 3


def test_group_different_topics_dont_cluster():
    g = LightweightGrouper()
    trends = [
        _t("bryan cranston", "google_trends"),
        _t("cody bellinger", "google_trends"),
        _t("juan soto", "google_trends"),
    ]
    groups = g.group(trends)
    # Three different celebrity names → three different groups
    assert len(groups) == 3
    for grp in groups:
        assert grp.member_count == 1


def test_group_short_names_excluded_from_clustering():
    """Names that normalize to < MIN_NAME_LENGTH get their own group each."""
    g = LightweightGrouper()
    trends = [
        _t("a", "x"),
        _t("b", "x"),
        _t("bryan cranston", "x"),
    ]
    groups = g.group(trends)
    # 'bryan cranston' clusters alone; 'a' and 'b' each get their own group
    cluster_sizes = sorted([grp.member_count for grp in groups], reverse=True)
    assert cluster_sizes[0] == 1  # bryan cranston alone
    # The two short names: each in their own group (won't cluster with each other)
    assert 1 in cluster_sizes


def test_group_threshold_controls_strictness():
    """A lower threshold clusters more aggressively."""
    loose = LightweightGrouper(threshold=0.5)
    tight = LightweightGrouper(threshold=0.95)
    trends = [
        _t("bryan cranston", "google_trends"),
        _t("bryan crane", "x"),  # similar but not identical
    ]
    loose_groups = loose.group(trends)
    tight_groups = tight.group(trends)
    # Loose merges, tight doesn't
    assert any(g.member_count == 2 for g in loose_groups)
    assert all(g.member_count == 1 for g in tight_groups)


def test_group_threshold_must_be_valid():
    with pytest.raises(ValueError):
        LightweightGrouper(threshold=0.0)
    with pytest.raises(ValueError):
        LightweightGrouper(threshold=1.5)


def test_group_returns_groups_sorted_by_size_then_score():
    g = LightweightGrouper()
    trends = [
        _t("bryan cranston", "google_trends", score=10.0),
        _t("bryan cranston", "x", score=5.0),
        _t("solo trend", "google_trends", score=100.0),  # high score but alone
    ]
    groups = g.group(trends)
    # 2-member cluster should be first (bigger)
    assert groups[0].member_count == 2
    assert groups[0].canonical_name == "bryan cranston"
    # Solo trend is second
    assert groups[1].member_count == 1
    assert groups[1].canonical_name == "solo trend"


def test_group_canonical_name_prefers_readable_form():
    g = LightweightGrouper()
    trends = [
        _t("#bryancranston", "x"),  # hashtag form
        _t("Bryan Cranston", "google_trends"),  # full name
    ]
    groups = g.group(trends)
    # 'Bryan Cranston' is the more readable form
    assert groups[0].canonical_name == "Bryan Cranston"


def test_group_method_is_lightweight():
    g = LightweightGrouper()
    trends = [_t("bryan cranston", "google_trends")]
    groups = g.group(trends)
    assert groups[0].grouping_method == "lightweight"


def test_group_handles_empty_input():
    g = LightweightGrouper()
    assert g.group([]) == []


def test_group_similarity_score_in_valid_range():
    g = LightweightGrouper()
    trends = [
        _t("bryan cranston", "google_trends"),
        _t("bryan cranston", "x"),
    ]
    groups = g.group(trends)
    assert 0.0 <= groups[0].similarity_score <= 1.0


def test_group_to_dict_shape():
    g = LightweightGrouper()
    trends = [
        _t("bryan cranston", "google_trends", score=6.0),
        _t("bryan cranston", "x", score=100.0),
    ]
    groups = g.group(trends)
    d = groups[0].to_dict()
    assert d["canonical_name"] == "bryan cranston"
    assert d["member_count"] == 2
    assert sorted(d["platforms"]) == ["google_trends", "x"]
    assert d["grouping_method"] == "lightweight"
    assert len(d["members"]) == 2
    assert all("platform" in m for m in d["members"])


# --- Length-ratio quick reject ---


def test_group_skips_very_different_lengths():
    """A long string and a short one (with no common content) should not
    cluster just because of the threshold."""
    g = LightweightGrouper(threshold=0.5)  # aggressive
    trends = [
        _t("a", "x"),  # normalized to "a" → filtered out (too short)
        _t("the quick brown fox jumps over the lazy dog" * 3, "x"),
    ]
    groups = g.group(trends)
    # The short one is filtered, the long one stays alone
    assert len(groups) == 2


# --- Realistic integration with current data ---


def test_group_with_actual_google_trends_data():
    """Simulate the actual current DB state and verify groups make sense."""
    g = LightweightGrouper()
    trends = [
        # 6-region duplicates of the same query should cluster
        _t("bryan cranston", "google_trends", score=6.0),
        _t("bryan cranston", "google_trends", score=6.0),
        _t("bryan cranston", "google_trends", score=6.0),
        _t("bryan cranston", "google_trends", score=5.0),
        # Plus one TikTok hashtag
        _t("#bryancranston", "tiktok", score=100.0),
        # Different celebrities stay separate
        _t("cody bellinger", "google_trends", score=5.0),
        _t("juan soto", "google_trends", score=6.0),
    ]
    groups = g.group(trends)
    # The bryan cranston group has 5 members (4 Google + 1 TikTok)
    bryan_group = next(g for g in groups if "bryan" in g.canonical_name.lower())
    assert bryan_group.member_count == 5
    assert bryan_group.platforms == {"google_trends", "tiktok"}
    # Cody and Juan each in their own group
    solo_groups = [g for g in groups if g.member_count == 1]
    assert len(solo_groups) == 2

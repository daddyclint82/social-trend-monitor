"""Tests for the semantic cross-platform grouper.

Tests cover:
- Cosine similarity math
- Exact-match fallback (no Ollama)
- Embedding-based grouping (mocked Ollama)
- Graceful degradation
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.normalizer.schema import make_trend
from src.normalizer.semantic import SemanticGrouper, TrendGroup


def test_cosine_similarity_identical():
    a = [1.0, 2.0, 3.0]
    assert SemanticGrouper.cosine_similarity(a, a) == pytest.approx(1.0, abs=1e-6)


def test_cosine_similarity_orthogonal():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert SemanticGrouper.cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-6)


def test_cosine_similarity_opposite():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert SemanticGrouper.cosine_similarity(a, b) == pytest.approx(-1.0, abs=1e-6)


def test_cosine_similarity_empty():
    assert SemanticGrouper.cosine_similarity([], []) == 0.0


def test_cosine_similarity_different_lengths():
    assert SemanticGrouper.cosine_similarity([1.0], [1.0, 2.0]) == 0.0


@pytest.mark.asyncio
async def test_group_fallback_exact_when_ollama_unavailable():
    """When Ollama is not running, should fall back to exact match."""
    grouper = SemanticGrouper(base_url="http://localhost:99999")
    grouper._available = False  # skip the check

    t1 = make_trend(platform="tiktok", name="#AIart", trend_type="hashtag",
                    platform_native_id="1", url=None, score=100)
    t2 = make_trend(platform="x", name="#aiart", trend_type="topic",
                    platform_native_id="2", url=None, score=200)
    t3 = make_trend(platform="tiktok", name="#cats", trend_type="hashtag",
                    platform_native_id="3", url=None, score=50)

    groups = await grouper.group([t1, t2, t3])
    # aiart should group together (exact normalized match), cats solo
    assert len(groups) == 2
    aiart_group = next(g for g in groups if "aiart" in g.canonical_name.lower())
    assert len(aiart_group.members) == 2
    assert aiart_group.grouping_method == "exact"
    cats_group = next(g for g in groups if "cat" in g.canonical_name.lower())
    assert len(cats_group.members) == 1


@pytest.mark.asyncio
async def test_group_by_embedding_with_mock():
    """Simulate Ollama embeddings and verify semantic clustering."""
    grouper = SemanticGrouper(base_url="http://localhost:11434")
    grouper._available = True  # pretend Ollama is running

    # Mock embed to return controlled vectors
    # "aiart" and "ai art" are similar, "cats" is different
    embeds = {
        "aiart": [0.9, 0.1, 0.0],
        "ai art": [0.85, 0.15, 0.0],
        "cats": [0.0, 0.1, 0.9],
    }

    async def mock_embed(text):
        return embeds.get(text)

    grouper.embed = AsyncMock(side_effect=mock_embed)

    t1 = make_trend(platform="tiktok", name="#AIart", trend_type="hashtag",
                    platform_native_id="1", url=None, score=100)
    t2 = make_trend(platform="x", name="AI art", trend_type="topic",
                    platform_native_id="2", url=None, score=200)
    t3 = make_trend(platform="tiktok", name="#cats", trend_type="hashtag",
                    platform_native_id="3", url=None, score=50)

    groups = await grouper.group([t1, t2, t3], threshold=0.75)
    # aiart and ai art should cluster (cosine ~0.99), cats solo
    assert len(groups) == 2
    ai_group = next(g for g in groups if "ai" in g.canonical_name.lower() and "cat" not in g.canonical_name.lower())
    assert len(ai_group.members) == 2
    assert ai_group.grouping_method == "embedding"
    assert ai_group.similarity_score > 0.75


@pytest.mark.asyncio
async def test_group_empty_list():
    grouper = SemanticGrouper(base_url="http://localhost:99999")
    grouper._available = False
    groups = await grouper.group([])
    assert groups == []


def test_trend_group_to_dict():
    t1 = make_trend(platform="tiktok", name="#test", trend_type="hashtag",
                    platform_native_id="1", url=None, score=100)
    g = TrendGroup(
        canonical_name="#test",
        members=[t1],
        platforms={"tiktok"},
        similarity_score=1.0,
        grouping_method="exact",
    )
    d = g.to_dict()
    assert d["canonical_name"] == "#test"
    assert d["member_count"] == 1
    assert d["platforms"] == ["tiktok"]
    assert d["grouping_method"] == "exact"
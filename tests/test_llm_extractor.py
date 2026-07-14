"""Tests for the LLM format extractor (ADR-0010, refined in ADR-0013).

Tests cover:
- Response parsing (no network)
- Cache key generation (6h bucket)
- Graceful degradation when Ollama is unavailable
- Prompt construction for all 4 trend_types (hashtag, sound, search, video)
- Backward compat with the old `hashtag` kwarg
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.llm.extractor import (
    FormatExtraction,
    LLMFormatExtractor,
    _HASHTAG_PROMPT,
    _PROMPTS,
    _SEARCH_PROMPT,
    _SOUND_PROMPT,
    _VIDEO_PROMPT,
)


def test_parse_response_full():
    raw = """FORMAT: POV storytelling with text overlay
PATTERNS: First-person hook, Quick cut reveal, Text-to-speech narration
WHY_IT_WORKS: Creates instant emotional connection in 3 seconds"""
    parsed = LLMFormatExtractor._parse_response(raw)
    assert "POV storytelling" in parsed["format"]
    assert "First-person hook" in parsed["patterns"]
    assert "emotional connection" in parsed["why_it_works"]


def test_parse_response_empty():
    parsed = LLMFormatExtractor._parse_response("")
    assert parsed == {"format": "", "patterns": "", "why_it_works": ""}


def test_parse_response_fallback_first_line():
    raw = "This is a short format summary.\nSecond line."
    parsed = LLMFormatExtractor._parse_response(raw)
    assert parsed["format"] == "This is a short format summary."


def test_parse_response_case_insensitive_keys():
    """FORMAT: and format: both work (LLMs sometimes vary case)."""
    raw = """format: lowercase key
patterns: a, b
why_it_works: lowkey"""
    parsed = LLMFormatExtractor._parse_response(raw)
    assert parsed["format"] == "lowercase key"
    assert parsed["patterns"] == "a, b"
    assert parsed["why_it_works"] == "lowkey"


def test_cache_key_format():
    extractor = LLMFormatExtractor()
    key1 = extractor._cache_key("tiktok:abc")
    key2 = extractor._cache_key("tiktok:abc")
    assert key1 == key2
    # Different trend → different key
    key3 = extractor._cache_key("tiktok:xyz")
    assert key1 != key3


# --- Backward-compat test: old _build_prompt with `hashtag` kwarg ---

def test_build_prompt_hashtag_includes_name():
    """The new keyword-only signature uses `name`. hashtag= keyword still works
    because the extract() method has the alias, but _build_prompt takes `name`."""
    extractor = LLMFormatExtractor()
    prompt = extractor._build_prompt(
        platform="tiktok_oembed",
        name="#aiart",
        trend_type="hashtag",
        post_descriptions=["AI sunset painting", "AI portrait video"],
        context={},
    )
    assert "#aiart" in prompt
    assert "tiktok" in prompt
    assert "AI sunset painting" in prompt
    assert "AI portrait video" in prompt
    # The hashtag prompt references the entity as "Hashtag:"
    assert "Hashtag:" in prompt


# --- New prompt tests for each trend_type (ADR-0013) ---

def test_prompts_table_has_all_four_types():
    assert set(_PROMPTS.keys()) == {"hashtag", "sound", "search", "video"}


def test_build_prompt_sound_includes_sound_label():
    """Sound prompt uses 'Sound:' label, not 'Hashtag:'."""
    extractor = LLMFormatExtractor()
    prompt = extractor._build_prompt(
        platform="tiktok_oembed",
        name="Quinceañera - Banda Machos",
        trend_type="sound",
        post_descriptions=["Wedding dance", "Birthday slideshow"],
        context={},
    )
    assert "Sound:" in prompt
    assert "Quinceañera" in prompt
    assert "Wedding dance" in prompt
    # Should NOT have the hashtag-specific label
    assert "Hashtag:" not in prompt


def test_build_prompt_search_includes_news_label():
    extractor = LLMFormatExtractor()
    prompt = extractor._build_prompt(
        platform="google_trends",
        name="bryan cranston",
        trend_type="search",
        post_descriptions=["Actor donates blood at event"],
        context={"region": "US", "pub_date": "Mon, 13 Jul 2026 18:10:00 -0700"},
    )
    assert "Search query:" in prompt
    assert "bryan cranston" in prompt
    assert "Region: US" in prompt
    assert "Trending since:" in prompt
    assert "Actor donates blood" in prompt


def test_build_prompt_video_includes_channel_and_views():
    extractor = LLMFormatExtractor()
    prompt = extractor._build_prompt(
        platform="youtube",
        name="Never Gonna Give You Up",
        trend_type="video",
        post_descriptions=[],
        context={
            "channel": "Rick Astley",
            "category": "Music",
            "region": "US",
            "views": "1500000000",
            "pub_date": "2009-10-25T06:57:33Z",
        },
    )
    assert "Video title:" in prompt
    assert "Never Gonna Give You Up" in prompt
    assert "Channel: Rick Astley" in prompt
    assert "Category: Music" in prompt
    assert "View count: 1500000000" in prompt


def test_build_prompt_handles_missing_context():
    """If context is empty, the prompt still renders without crashing."""
    extractor = LLMFormatExtractor()
    prompt = extractor._build_prompt(
        platform="youtube",
        name="x",
        trend_type="video",
        post_descriptions=[],
        context={},
    )
    # Empty defaults → "Channel: " etc., but no KeyError
    assert "Channel:" in prompt
    assert "View count:" in prompt


def test_build_prompt_handles_empty_descriptions():
    extractor = LLMFormatExtractor()
    prompt = extractor._build_prompt(
        platform="tiktok_oembed",
        name="#fyp",
        trend_type="hashtag",
        post_descriptions=[],
        context={},
    )
    # Should have a placeholder line, not crash
    assert "(no descriptions available)" in prompt


def test_build_prompt_falls_back_to_hashtag_for_unknown_type():
    """Unknown trend_type falls back to the hashtag prompt (safe default)."""
    extractor = LLMFormatExtractor()
    prompt = extractor._build_prompt(
        platform="tiktok_oembed",
        name="weird",
        trend_type="alien_megastructure",
        post_descriptions=[],
        context={},
    )
    # Falls back to the hashtag prompt which uses "Hashtag:"
    assert "Hashtag:" in prompt


def test_build_prompt_truncates_descriptions_to_10():
    extractor = LLMFormatExtractor()
    descs = [f"desc {i}" for i in range(20)]
    prompt = extractor._build_prompt(
        platform="tiktok_oembed",
        name="#x",
        trend_type="hashtag",
        post_descriptions=descs,
        context={},
    )
    assert "desc 0" in prompt
    assert "desc 9" in prompt
    assert "desc 19" not in prompt


# --- Backward compat: hashtag kwarg alias ---

@pytest.mark.asyncio
async def test_extract_accepts_hashtag_kwarg_as_alias():
    """If `name` is omitted but `hashtag` is provided, it's used as name."""
    extractor = LLMFormatExtractor(base_url="http://localhost:99999")
    result = await extractor.extract(
        trend_id="tiktok:abc",
        platform="tiktok_oembed",
        hashtag="#legacy",
        post_descriptions=["d1"],
    )
    assert result.hashtag == "#legacy"
    assert result.trend_type == "hashtag"  # default
    assert result.format_summary == "(LLM unavailable)"


@pytest.mark.asyncio
async def test_extract_requires_name_or_hashtag():
    extractor = LLMFormatExtractor(base_url="http://localhost:99999")
    with pytest.raises(ValueError, match="`name` is required"):
        await extractor.extract(
            trend_id="x:1",
            platform="x",
            post_descriptions=[],
        )


@pytest.mark.asyncio
async def test_extract_takes_precedence_when_both_passed():
    """If both `name` and `hashtag` are passed, `name` wins."""
    extractor = LLMFormatExtractor(base_url="http://localhost:99999")
    result = await extractor.extract(
        trend_id="x:1",
        platform="x",
        name="#newstyle",
        hashtag="#oldstyle",
        post_descriptions=[],
    )
    assert result.hashtag == "#newstyle"


# --- Available / unavailable paths ---

@pytest.mark.asyncio
async def test_ollama_unavailable_returns_placeholder():
    extractor = LLMFormatExtractor(base_url="http://localhost:99999")
    result = await extractor.extract(
        trend_id="tiktok:abc",
        platform="tiktok_oembed",
        hashtag="#test",
        post_descriptions=["desc 1", "desc 2"],
    )
    assert result.format_summary == "(LLM unavailable)"
    assert result.cached is False
    assert result.trend_type == "hashtag"


@pytest.mark.asyncio
async def test_ollama_unavailable_search_trend_type_preserved():
    """Even on unavailable, the trend_type is preserved in the result."""
    extractor = LLMFormatExtractor(base_url="http://localhost:99999")
    result = await extractor.extract(
        trend_id="google_trends:abc",
        platform="google_trends",
        name="bryan cranston",
        trend_type="search",
        post_descriptions=["news1", "news2"],
    )
    assert result.trend_type == "search"
    assert result.hashtag == "bryan cranston"


@pytest.mark.asyncio
async def test_cache_returns_cached_result():
    extractor = LLMFormatExtractor()
    # Pre-populate cache
    key = extractor._cache_key("tiktok:abc")
    cached_result = FormatExtraction(
        trend_id="tiktok:abc",
        platform="tiktok_oembed",
        hashtag="#test",
        trend_type="hashtag",  # NEW: required field
        format_summary="Cached format",
        patterns="",
        why_it_works="",
        raw_response="",
        extracted_at="2026-01-01T00:00:00+00:00",
        model="test",
    )
    extractor._cache[key] = cached_result

    result = await extractor.extract(
        trend_id="tiktok:abc",
        platform="tiktok_oembed",
        hashtag="#test",
        post_descriptions=["desc"],
    )
    assert result.cached is True
    assert result.format_summary == "Cached format"


@pytest.mark.asyncio
async def test_extract_batch_parallel():
    extractor = LLMFormatExtractor(base_url="http://localhost:99999")
    items = [
        {"trend_id": "tiktok:1", "platform": "tiktok", "hashtag": "#a", "post_descriptions": []},
        {"trend_id": "tiktok:2", "platform": "tiktok", "hashtag": "#b", "post_descriptions": []},
        {"trend_id": "google_trends:1", "platform": "google_trends", "name": "topic1", "trend_type": "search", "post_descriptions": []},
    ]
    results = await extractor.extract_batch(items)
    assert len(results) == 3
    assert all(r.format_summary == "(LLM unavailable)" for r in results)
    # Trend type is preserved
    types = [r.trend_type for r in results]
    assert types == ["hashtag", "hashtag", "search"]


def test_format_extraction_to_dict():
    fe = FormatExtraction(
        trend_id="x:1",
        platform="x",
        hashtag="#test",
        trend_type="hashtag",
        format_summary="POV video",
        patterns="hook, reveal",
        why_it_works="engaging",
        raw_response="...",
        extracted_at="2026-07-13T22:00:00+00:00",
        model="qwen3.5:latest",
    )
    d = fe.to_dict()
    assert d["format_summary"] == "POV video"
    assert d["platform"] == "x"
    assert d["trend_type"] == "hashtag"
    # Backward compat: 'hashtag' key still present
    assert d["hashtag"] == "#test"
    # New alias
    assert d["name"] == "#test"


def test_format_extraction_to_dict_search_type():
    fe = FormatExtraction(
        trend_id="google_trends:1",
        platform="google_trends",
        hashtag="bryan cranston",
        trend_type="search",
        format_summary="Celebrity PR moment",
        patterns="",
        why_it_works="",
        raw_response="",
        extracted_at="2026-07-13T22:00:00+00:00",
        model="qwen3.5:latest",
    )
    d = fe.to_dict()
    assert d["trend_type"] == "search"
    assert d["name"] == "bryan cranston"

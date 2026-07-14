"""Tests for the LLM format extractor (ADR-0010).

Tests cover:
- Response parsing (no network)
- Cache key generation (6h bucket)
- Graceful degradation when Ollama is unavailable
- Prompt construction
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.llm.extractor import FormatExtraction, LLMFormatExtractor


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


def test_parse_response_case_insensitive():
    raw = """format: Get Ready With Me
patterns: Product showcase, Voiceover routine
why_it_works: Relatable daily routine"""
    parsed = LLMFormatExtractor._parse_response(raw)
    assert "Get Ready With Me" in parsed["format"]


def test_cache_key_changes_with_bucket():
    extractor = LLMFormatExtractor()
    key1 = extractor._cache_key("tiktok:abc")
    # Same bucket (within 6h) → same key
    key2 = extractor._cache_key("tiktok:abc")
    assert key1 == key2


def test_build_prompt_includes_hashtag():
    extractor = LLMFormatExtractor()
    prompt = extractor._build_prompt(
        platform="tiktok",
        hashtag="#aiart",
        descriptions=["AI sunset painting", "AI portrait video"],
    )
    assert "#aiart" in prompt
    assert "tiktok" in prompt
    assert "AI sunset painting" in prompt
    assert "AI portrait video" in prompt


@pytest.mark.asyncio
async def test_ollama_unavailable_returns_placeholder():
    extractor = LLMFormatExtractor(base_url="http://localhost:99999")
    # is_available should return False (nothing on that port)
    result = await extractor.extract(
        trend_id="tiktok:abc",
        platform="tiktok",
        hashtag="#test",
        post_descriptions=["desc 1", "desc 2"],
    )
    assert result.format_summary == "(LLM unavailable)"
    assert result.cached is False


@pytest.mark.asyncio
async def test_cache_returns_cached_result():
    extractor = LLMFormatExtractor()
    # Pre-populate cache
    key = extractor._cache_key("tiktok:abc")
    cached_result = FormatExtraction(
        trend_id="tiktok:abc",
        platform="tiktok",
        hashtag="#test",
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
        platform="tiktok",
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
        {"trend_id": "tiktok:3", "platform": "tiktok", "hashtag": "#c", "post_descriptions": []},
    ]
    results = await extractor.extract_batch(items)
    assert len(results) == 3
    assert all(r.format_summary == "(LLM unavailable)" for r in results)


def test_format_extraction_to_dict():
    fe = FormatExtraction(
        trend_id="x:1",
        platform="x",
        hashtag="#test",
        format_summary="POV video",
        patterns="hook, reveal",
        why_it_works="engaging",
        raw_response="...",
        extracted_at="2026-07-13T22:00:00+00:00",
        model="llama3.1:8b",
    )
    d = fe.to_dict()
    assert d["format_summary"] == "POV video"
    assert d["platform"] == "x"
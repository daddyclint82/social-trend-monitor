"""LLM-based format/style extractor (ADR-0010).

Takes a trending hashtag + 3–10 top post captions/descriptions and asks
the local LLM (Ollama) to summarize the dominant content format.

Runs on a slow schedule (every 6 hours by default) — formats don't
change fast. Caches results per (platform, trend_key, 6h_bucket).

If Ollama is not running, gracefully degrades: returns a placeholder
string and logs a warning. The system works without the LLM.
"""
from __future__ import annotations

import asyncio
import json
import structlog
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = structlog.get_logger(__name__)

# Prompt template — kept short to minimize tokens
_FORMAT_PROMPT = """You are a content strategy analyst. Given a trending hashtag and several top post descriptions from {platform}, summarize the dominant content format.

Hashtag: {hashtag}
Platform: {platform}
Top posts (descriptions):
{posts}

Respond in this exact format:
FORMAT: <5-10 word summary of the dominant format>
PATTERNS: <2-3 example patterns, comma-separated>
WHY_IT_WORKS: <1-2 sentences on why this format resonates>

Keep it concise. No preamble."""


@dataclass
class FormatExtraction:
    """Result of an LLM format extraction call."""

    trend_id: str
    platform: str
    hashtag: str
    format_summary: str
    patterns: str
    why_it_works: str
    raw_response: str
    extracted_at: str
    model: str
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "trend_id": self.trend_id,
            "platform": self.platform,
            "hashtag": self.hashtag,
            "format_summary": self.format_summary,
            "patterns": self.patterns,
            "why_it_works": self.why_it_works,
            "raw_response": self.raw_response,
            "extracted_at": self.extracted_at,
            "model": self.model,
            "cached": self.cached,
        }


class LLMFormatExtractor:
    """Extract content format summaries from trending hashtags using Ollama.

    Usage:
        extractor = LLMFormatExtractor(
            base_url="http://localhost:11434",
            model="llama3.1:8b",
        )
        result = await extractor.extract(
            trend_id="tiktok:abc",
            platform="tiktok",
            hashtag="#aiart",
            post_descriptions=["AI-generated painting of a sunset", ...],
        )
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1:8b",
        timeout_s: float = 60.0,
        cache: dict[str, FormatExtraction] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._cache: dict[str, FormatExtraction] = cache or {}
        self._available: bool | None = None  # lazy-check

    async def is_available(self) -> bool:
        """Check if Ollama is running and the model is available."""
        if self._available is not None:
            return self._available
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(f"{self.base_url}/api/tags")
                if resp.status_code != 200:
                    self._available = False
                    return False
                tags = resp.json()
                models = {m.get("name", "") for m in tags.get("models", [])}
                # Check exact match or base name match (e.g. "llama3.1:8b" matches "llama3.1")
                model_base = self.model.split(":")[0]
                self._available = any(
                    self.model in m or m.startswith(model_base) for m in models
                )
                if not self._available:
                    logger.warning(
                        "llm.model_not_found",
                        model=self.model,
                        available=sorted(models)[:10],
                    )
                return self._available
        except (httpx.HTTPError, Exception) as e:
            logger.warning("llm.ollama_unavailable", error=str(e), base_url=self.base_url)
            self._available = False
            return False

    def _cache_key(self, trend_id: str, bucket_h: int = 6) -> str:
        """Cache key: trend_id + 6-hour bucket. Same bucket = same result."""
        now = datetime.now(tz=timezone.utc)
        bucket = int(now.timestamp() // (bucket_h * 3600))
        return f"{trend_id}:{bucket}"

    async def extract(
        self,
        trend_id: str,
        platform: str,
        hashtag: str,
        post_descriptions: list[str],
        force: bool = False,
    ) -> FormatExtraction:
        """Extract format summary for a trending hashtag.

        Returns a FormatExtraction. If Ollama is down, returns a
        placeholder with format_summary="(LLM unavailable)".
        """
        key = self._cache_key(trend_id)
        if not force and key in self._cache:
            cached = self._cache[key]
            cached.cached = True
            logger.info("llm.cache_hit", trend_id=trend_id)
            return cached

        if not await self.is_available():
            result = FormatExtraction(
                trend_id=trend_id,
                platform=platform,
                hashtag=hashtag,
                format_summary="(LLM unavailable)",
                patterns="",
                why_it_works="",
                raw_response="",
                extracted_at=datetime.now(tz=timezone.utc).isoformat(),
                model=self.model,
            )
            self._cache[key] = result
            return result

        prompt = self._build_prompt(platform, hashtag, post_descriptions)
        raw = await self._call_ollama(prompt)
        parsed = self._parse_response(raw)

        result = FormatExtraction(
            trend_id=trend_id,
            platform=platform,
            hashtag=hashtag,
            format_summary=parsed.get("format", ""),
            patterns=parsed.get("patterns", ""),
            why_it_works=parsed.get("why_it_works", ""),
            raw_response=raw,
            extracted_at=datetime.now(tz=timezone.utc).isoformat(),
            model=self.model,
        )
        self._cache[key] = result
        logger.info(
            "llm.extracted",
            trend_id=trend_id,
            format=result.format_summary[:50],
        )
        return result

    def _build_prompt(
        self, platform: str, hashtag: str, descriptions: list[str]
    ) -> str:
        posts_text = "\n".join(f"- {d}" for d in descriptions[:10])
        return _FORMAT_PROMPT.format(
            platform=platform,
            hashtag=hashtag,
            posts=posts_text,
        )

    async def _call_ollama(self, prompt: str) -> str:
        """Call Ollama's /api/generate endpoint."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as http:
                resp = await http.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.3,  # low temp for factual summary
                            "top_p": 0.9,
                        },
                    },
                )
                if resp.status_code != 200:
                    logger.warning(
                        "llm.ollama_http_error",
                        status=resp.status_code,
                        body=resp.text[:200],
                    )
                    return ""
                data = resp.json()
                return data.get("response", "")
        except (httpx.HTTPError, Exception) as e:
            logger.warning("llm.ollama_call_failed", error=str(e))
            return ""

    @staticmethod
    def _parse_response(raw: str) -> dict[str, str]:
        """Parse the structured LLM response.

        Expected format:
        FORMAT: <summary>
        PATTERNS: <patterns>
        WHY_IT_WORKS: <explanation>
        """
        result: dict[str, str] = {"format": "", "patterns": "", "why_it_works": ""}
        if not raw:
            return result
        for line in raw.strip().splitlines():
            line = line.strip()
            if line.upper().startswith("FORMAT:"):
                result["format"] = line[len("FORMAT:"):].strip()
            elif line.upper().startswith("PATTERNS:"):
                result["patterns"] = line[len("PATTERNS:"):].strip()
            elif line.upper().startswith("WHY_IT_WORKS:"):
                result["why_it_works"] = line[len("WHY_IT_WORKS:"):].strip()
        # Fallback: if no structured parse, use first line as format
        if not result["format"] and raw:
            result["format"] = raw.strip().split("\n")[0][:100]
        return result

    async def extract_batch(
        self,
        items: list[dict[str, Any]],
        force: bool = False,
    ) -> list[FormatExtraction]:
        """Extract formats for multiple trends in parallel.

        Each item should have: trend_id, platform, hashtag,
        post_descriptions (list[str]).
        """
        coros = [
            self.extract(
                trend_id=item["trend_id"],
                platform=item["platform"],
                hashtag=item["hashtag"],
                post_descriptions=item.get("post_descriptions", []),
                force=force,
            )
            for item in items
        ]
        return await asyncio.gather(*coros)
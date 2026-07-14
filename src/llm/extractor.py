"""LLM-based format/style extractor (ADR-0010, refined in ADR-0013).

Takes a trending entity (hashtag, sound, search topic, or video) and
asks the local LLM (Ollama) to summarize the dominant content format.

Runs on a slow schedule (every 6 hours by default) — formats don't
change fast. Caches results per (platform, trend_key, 6h_bucket).

If Ollama is not running, gracefully degrades: returns a placeholder
string and logs a warning. The system works without the LLM.

**Trend-type-aware (ADR-0013):**
We now support extraction for 4 distinct trend kinds:
- hashtag (TikTok, X, Instagram) — the original use case
- sound   (TikTok) — music/audio format
- search  (Google Trends) — search query, often news-driven
- video   (YouTube) — trending video with view count

Each type gets its own prompt template (different framing, different
post context) but the response shape is uniform: FORMAT / PATTERNS /
WHY_IT_WORKS lines.
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


# --- Per-trend-type prompt templates (ADR-0013) ---

# Hashtag: original prompt. A hashtag groups many posts; we want the
# dominant format/pattern of the posts using it.
_HASHTAG_PROMPT = """You are a content strategy analyst. Given a trending hashtag and several top post descriptions from {platform}, summarize the dominant content format.

Hashtag: {name}
Platform: {platform}
Top posts (descriptions):
{posts}

Respond in this exact format:
FORMAT: <5-10 word summary of the dominant format>
PATTERNS: <2-3 example patterns, comma-separated>
WHY_IT_WORKS: <1-2 sentences on why this format resonates>

Keep it concise. No preamble."""


# Sound: a trending song/audio. We want to know what kind of content is
# typically paired with it (dance, lip-sync, storytime, etc.).
_SOUND_PROMPT = """You are a content strategy analyst. Given a trending sound/audio clip and several top video descriptions that use it from {platform}, summarize the dominant content format.

Sound: {name}
Platform: {platform}
Top videos (descriptions):
{posts}

Respond in this exact format:
FORMAT: <5-10 word summary of how this sound is typically used>
PATTERNS: <2-3 example video patterns paired with this sound, comma-separated>
WHY_IT_WORKS: <1-2 sentences on why this sound is trending>

Keep it concise. No preamble."""


# Search: a Google Trends search query. Tied to current events. The
# "posts" are news article titles from the RSS feed's news_item list.
_SEARCH_PROMPT = """You are a content strategy analyst. Given a trending search query and several related news headlines from {platform}, summarize why this topic is trending and what content angle is gaining traction.

Search query: {name}
Region: {region}
Trending since: {pub_date}
Top news (headlines):
{posts}

Respond in this exact format:
FORMAT: <5-10 word summary of the content angle gaining traction>
PATTERNS: <2-3 example content hooks creators can use, comma-separated>
WHY_IT_WORKS: <1-2 sentences on the underlying news cycle or cultural moment>

Keep it concise. No preamble."""


# Video: a trending YouTube video. Already a piece of content; we want
# to know why it's winning (title pattern, length, category) so creators
# can replicate the format.
_VIDEO_PROMPT = """You are a content strategy analyst. Given a trending video and its context from {platform}, summarize why this video is performing well and what format/style pattern is working.

Video title: {name}
Channel: {channel}
Category: {category}
Region: {region}
View count: {views}
Published: {pub_date}

Respond in this exact format:
FORMAT: <5-10 word summary of the video format or style>
PATTERNS: <2-3 replicable patterns (title structure, hook, length), comma-separated>
WHY_IT_WORKS: <1-2 sentences on why this format is winning views>

Keep it concise. No preamble."""


# Lookup table: trend_type → prompt template
_PROMPTS: dict[str, str] = {
    "hashtag": _HASHTAG_PROMPT,
    "sound": _SOUND_PROMPT,
    "search": _SEARCH_PROMPT,
    "video": _VIDEO_PROMPT,
}


# Backward compat: the old prompt is kept under the same constant name
# for any caller that referenced it directly. Aliases to hashtag prompt.
_FORMAT_PROMPT = _HASHTAG_PROMPT


@dataclass
class FormatExtraction:
    """Result of an LLM format extraction call."""

    trend_id: str
    platform: str
    hashtag: str  # display name (kept for backward compat; populated from `name`)
    trend_type: str  # NEW (ADR-0013): hashtag | sound | search | video
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
            "hashtag": self.hashtag,  # legacy field name
            "name": self.hashtag,     # alias for clarity
            "trend_type": self.trend_type,
            "format_summary": self.format_summary,
            "patterns": self.patterns,
            "why_it_works": self.why_it_works,
            "raw_response": self.raw_response,
            "extracted_at": self.extracted_at,
            "model": self.model,
            "cached": self.cached,
        }


class LLMFormatExtractor:
    """Extract content format summaries from trending entities using Ollama.

    Usage:
        extractor = LLMFormatExtractor(
            base_url="http://localhost:11434",
            model="llama3.1:8b",
        )
        result = await extractor.extract(
            trend_id="tiktok:abc",
            platform="tiktok",
            name="#aiart",
            trend_type="hashtag",
            post_descriptions=["AI-generated painting", ...],
        )

    Backward compat: `hashtag` kwarg is accepted as an alias for `name`.
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
        name: str | None = None,
        *,
        hashtag: str | None = None,
        trend_type: str = "hashtag",
        post_descriptions: list[str] | None = None,
        context: dict[str, Any] | None = None,
        force: bool = False,
    ) -> FormatExtraction:
        """Extract format summary for a trending entity.

        Args:
            trend_id: stable primary key
            platform: tiktok | x | google_trends | youtube | ...
            name: display name (the new canonical kwarg)
            hashtag: DEPRECATED alias for `name`. Kept for backward compat.
                     If both passed, `name` wins.
            trend_type: hashtag | sound | search | video (default: hashtag)
            post_descriptions: list of post/news descriptions (used for
                               hashtag/sound/search prompts)
            context: extra per-type fields (used by search and video
                     prompts — region, pub_date, channel, category, views).
                     See _build_prompt for expected keys.
            force: bypass cache
        """
        # Backward compat: `hashtag` aliases to `name`
        if name is None and hashtag is not None:
            name = hashtag
        if name is None:
            raise ValueError("`name` is required (or pass `hashtag` for backward compat)")
        post_descriptions = post_descriptions or []
        context = context or {}

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
                hashtag=name,
                trend_type=trend_type,
                format_summary="(LLM unavailable)",
                patterns="",
                why_it_works="",
                raw_response="",
                extracted_at=datetime.now(tz=timezone.utc).isoformat(),
                model=self.model,
            )
            self._cache[key] = result
            return result

        prompt = self._build_prompt(
            platform=platform,
            name=name,
            trend_type=trend_type,
            post_descriptions=post_descriptions,
            context=context,
        )
        raw = await self._call_ollama(prompt)
        parsed = self._parse_response(raw)

        result = FormatExtraction(
            trend_id=trend_id,
            platform=platform,
            hashtag=name,
            trend_type=trend_type,
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
            trend_type=trend_type,
            format=result.format_summary[:50],
        )
        return result

    def _build_prompt(
        self,
        *,
        platform: str,
        name: str,
        trend_type: str,
        post_descriptions: list[str],
        context: dict[str, Any],
    ) -> str:
        """Select prompt template by trend_type and format with context.

        All templates use the same {posts} field name (news headlines or
        post descriptions) and {platform} field. Type-specific fields
        (region, pub_date, channel, category, views) default to empty
        strings so a missing context value doesn't crash the .format().
        """
        template = _PROMPTS.get(trend_type, _HASHTAG_PROMPT)
        posts_text = "\n".join(f"- {d}" for d in (post_descriptions or [])[:10])
        if not posts_text:
            posts_text = "- (no descriptions available)"
        return template.format(
            platform=platform,
            name=name,
            posts=posts_text,
            region=context.get("region", ""),
            pub_date=context.get("pub_date", ""),
            channel=context.get("channel", ""),
            category=context.get("category", ""),
            views=context.get("views", ""),
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
        if not result["format"] and raw:
            result["format"] = raw.strip().split("\n")[0][:100]
        return result

    async def extract_batch(
        self,
        items: list[dict[str, Any]],
        force: bool = False,
    ) -> list[FormatExtraction]:
        """Extract formats for multiple trends in parallel.

        Each item should have: trend_id, platform, name (or hashtag for
        backward compat), trend_type (default 'hashtag'),
        post_descriptions (list[str]), context (dict, optional).
        """
        coros = [
            self.extract(
                trend_id=item["trend_id"],
                platform=item["platform"],
                name=item.get("name") or item.get("hashtag"),
                hashtag=item.get("hashtag"),
                trend_type=item.get("trend_type", "hashtag"),
                post_descriptions=item.get("post_descriptions") or [],
                context=item.get("context") or {},
                force=force,
            )
            for item in items
        ]
        return await asyncio.gather(*coros, return_exceptions=False)

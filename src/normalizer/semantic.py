"""Semantic cross-platform grouping using Ollama embeddings.

Problem: exact match + Levenshtein can't detect that
  "Taylor Swift" = "T-Swift" = "TSwift" = "taylor swift eras tour"

Solution: embed each trend name using Ollama's /api/embeddings endpoint,
then cluster by cosine similarity. Trends above a similarity threshold
get grouped as a single cross-platform trend.

If Ollama is not running, falls back to exact-match + Levenshtein
(the v1 grouping strategy).
"""
from __future__ import annotations

import asyncio
import structlog
import math
from dataclasses import dataclass
from typing import Any

import httpx

from .schema import Trend

logger = structlog.get_logger(__name__)

# Default similarity threshold (cosine similarity, 0-1)
DEFAULT_THRESHOLD = 0.75

# Minimum name length to bother embedding (too short = noisy)
MIN_NAME_LENGTH = 3


@dataclass
class TrendGroup:
    """A group of semantically similar trends across platforms."""

    canonical_name: str
    members: list[Trend]
    platforms: set[str]
    similarity_score: float  # average pairwise similarity
    grouping_method: str  # "embedding" | "exact" | "levenshtein"

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "member_count": len(self.members),
            "platforms": sorted(self.platforms),
            "similarity_score": round(self.similarity_score, 4),
            "grouping_method": self.grouping_method,
            "members": [
                {"platform": t.platform, "name": t.name, "score": t.score}
                for t in self.members
            ],
        }


class SemanticGrouper:
    """Group trends by semantic similarity using Ollama embeddings.

    Usage:
        grouper = SemanticGrouper(base_url="http://localhost:11434")
        groups = await grouper.group(trends, threshold=0.75)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._available: bool | None = None
        self._embedding_cache: dict[str, list[float]] = {}

    async def is_available(self) -> bool:
        """Check if Ollama is running and the embedding model is available."""
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
                    logger.info(
                        "semantic.embed_model_not_found",
                        model=self.model,
                        hint=f"Run: ollama pull {self.model}",
                    )
                return self._available
        except Exception as e:
            logger.info("semantic.ollama_unavailable", error=str(e))
            self._available = False
            return False

    async def embed(self, text: str) -> list[float] | None:
        """Get embedding for a text string. Returns None on failure."""
        if text in self._embedding_cache:
            return self._embedding_cache[text]
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as http:
                resp = await http.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                emb = data.get("embedding", [])
                if emb:
                    self._embedding_cache[text] = emb
                return emb
        except Exception:
            return None

    async def embed_many(
        self, texts: list[str], concurrency: int = 5
    ) -> dict[str, list[float] | None]:
        """Embed multiple texts in parallel with limited concurrency."""
        sem = asyncio.Semaphore(concurrency)

        async def _one(text: str) -> tuple[str, list[float] | None]:
            async with sem:
                return text, await self.embed(text)

        results = await asyncio.gather(*[_one(t) for t in texts])
        return dict(results)

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(y * y for y in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    async def group(
        self,
        trends: list[Trend],
        threshold: float = DEFAULT_THRESHOLD,
    ) -> list[TrendGroup]:
        """Group trends by semantic similarity.

        If Ollama is available, uses embeddings + cosine similarity.
        Otherwise falls back to exact normalized name match.
        """
        if not trends:
            return []

        if await self.is_available():
            return await self._group_by_embedding(trends, threshold)
        return self._group_by_exact(trends)

    async def _group_by_embedding(
        self, trends: list[Trend], threshold: float
    ) -> list[TrendGroup]:
        """Embed each trend name, cluster by cosine similarity."""
        # Deduplicate names to embed (many trends share names)
        unique_names = list({t.normalized_name for t in trends if len(t.normalized_name) >= MIN_NAME_LENGTH})
        embeddings = await self.embed_many(unique_names)

        # Build name -> embedding map
        name_to_emb: dict[str, list[float] | None] = {}
        for name in unique_names:
            name_to_emb[name] = embeddings.get(name)

        # Greedy clustering: pick a seed, absorb all similar, repeat
        assigned: set[str] = set()  # normalized names already grouped
        groups: list[TrendGroup] = []

        for name in unique_names:
            if name in assigned:
                continue
            emb = name_to_emb.get(name)
            if emb is None:
                # Embedding failed — treat as solo
                members = [t for t in trends if t.normalized_name == name]
                if members:
                    groups.append(TrendGroup(
                        canonical_name=members[0].name,
                        members=members,
                        platforms={t.platform for t in members},
                        similarity_score=1.0,
                        grouping_method="embedding",
                    ))
                    assigned.add(name)
                continue

            # Find all similar names
            cluster_names = [name]
            for other in unique_names:
                if other == name or other in assigned:
                    continue
                other_emb = name_to_emb.get(other)
                if other_emb is None:
                    continue
                sim = self.cosine_similarity(emb, other_emb)
                if sim >= threshold:
                    cluster_names.append(other)

            # Collect all trends in this cluster
            members: list[Trend] = []
            for cn in cluster_names:
                members.extend(t for t in trends if t.normalized_name == cn)
                assigned.add(cn)

            if members:
                # Average pairwise similarity
                sims: list[float] = []
                for i, n1 in enumerate(cluster_names):
                    e1 = name_to_emb.get(n1)
                    if e1 is None:
                        continue
                    for n2 in cluster_names[i + 1:]:
                        e2 = name_to_emb.get(n2)
                        if e2 is None:
                            continue
                        sims.append(self.cosine_similarity(e1, e2))
                avg_sim = sum(sims) / len(sims) if sims else 1.0

                groups.append(TrendGroup(
                    canonical_name=members[0].name,
                    members=members,
                    platforms={t.platform for t in members},
                    similarity_score=avg_sim,
                    grouping_method="embedding",
                ))

        logger.info(
            "semantic.grouped_by_embedding",
            trends_in=len(trends),
            groups_out=len(groups),
            threshold=threshold,
        )
        return groups

    def _group_by_exact(self, trends: list[Trend]) -> list[TrendGroup]:
        """Fallback: group by exact normalized name match (v1 strategy)."""
        from collections import defaultdict

        name_map: dict[str, list[Trend]] = defaultdict(list)
        for t in trends:
            name_map[t.normalized_name].append(t)

        groups: list[TrendGroup] = []
        for name, members in name_map.items():
            groups.append(TrendGroup(
                canonical_name=members[0].name,
                members=members,
                platforms={t.platform for t in members},
                similarity_score=1.0 if len(members) == 1 else 0.85,
                grouping_method="exact",
            ))

        logger.info(
            "semantic.grouped_by_exact",
            trends_in=len(trends),
            groups_out=len(groups),
        )
        return groups
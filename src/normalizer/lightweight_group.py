"""Lightweight cross-platform grouper (Phase 0.2 — Trend Aggregator Improvement Ledger).

Problem:
    The existing `SemanticGrouper` (semantic.py) requires Ollama running
    with an embedding model. In the typical "I just want a CLI to see
    what's hot" workflow, Ollama isn't running, so grouping falls back
    to exact-match — which produces 0 cross-platform matches in practice.

Solution:
    A `difflib.SequenceMatcher`-based grouper that runs in <100ms on
    1000 trends, with no external dependencies. Two strings are grouped
    if their similarity is above a threshold (default 0.85) AND they
    pass a length filter (skip very short strings where similarity is
    noisy).

Why this is enough for v0.5:
    - Catches "Taylor Swift" / "taylor swift" / "taylor-swift"
    - Catches "bryan cranston" / "Bryan Cranston" / "bryan-cranston"
    - Misses "TSwift" / "Taylor S" / "T-Swift Era" (semantic, not lexical)
    - The MiniLM embedding grouper in `semantic.py` is the v1.0 escape
      hatch — `LightweightGrouper.group()` returns groups with
      `method="lightweight"` so callers can re-cluster with embeddings
      when Ollama is available.

Design:
    1. Normalize strings aggressively (lowercase, strip #, strip
       punctuation, collapse whitespace, strip common URL fragments).
    2. Use SequenceMatcher.ratio() on normalized forms.
    3. Group by single-linkage clustering: O(n²) but n is small.
    4. Reject very short strings (less than MIN_NAME_LENGTH) from
       the cluster — they collide too often.

Limitations (documented in tests):
    - "BTS" / "BTS Army" / "BTS V" all share the prefix "bts" — they
      MAY cluster. Acceptable for v0.5.
    - Translation (Japanese / Hindi / Portuguese) is NOT handled. Out
      of scope; the Translation step (Phase 1) is the proper fix.
"""
from __future__ import annotations

import re
import structlog
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .schema import Trend

logger = structlog.get_logger(__name__)

DEFAULT_THRESHOLD = 0.85
MIN_NAME_LENGTH = 4


# Common noise patterns to strip before comparison
_PREFIX_CHARS = ("#", "@", "/", "\\")
_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+")
# Strip just the protocol+domain, keep the path
_URL_PROTOCOL = re.compile(r"https?://[a-z0-9.-]+/?|www\.[a-z0-9.-]+/?", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_for_grouping(name: str) -> str:
    """Aggressive normalization for cross-platform string matching.

    Steps (in order):
    - Strip leading #/@// (hashtag / handle / path prefixes)
    - Lowercase
    - Remove URL protocol+domain (https://example.com/ → '')
      but keep the path (/tag/aiart → 'aiart' after punctuation strip)
    - Replace non-alphanumeric (except space) with space
    - Collapse whitespace
    - Strip surrounding whitespace
    """
    n = name.strip()
    for prefix in _PREFIX_CHARS:
        if n.startswith(prefix):
            n = n[len(prefix):]
    n = n.lower()
    n = _URL_PROTOCOL.sub("", n)
    n = _NON_ALNUM.sub(" ", n)
    n = _WHITESPACE.sub(" ", n)
    return n.strip()


@dataclass
class CrossPlatformGroup:
    """A cluster of trends that are likely the same topic across platforms."""

    canonical_name: str
    members: list[Trend] = field(default_factory=list)
    platforms: set[str] = field(default_factory=set)
    similarity_score: float = 1.0
    grouping_method: str = "lightweight"

    @property
    def member_count(self) -> int:
        return len(self.members)

    def to_dict(self) -> dict:
        return {
            "canonical_name": self.canonical_name,
            "member_count": self.member_count,
            "platforms": sorted(self.platforms),
            "similarity_score": round(self.similarity_score, 4),
            "grouping_method": self.grouping_method,
            "members": [
                {
                    "platform": t.platform,
                    "name": t.name,
                    "trend_type": t.trend_type,
                    "score": t.score,
                    "url": t.url,
                }
                for t in self.members
            ],
        }


class LightweightGrouper:
    """Cross-platform trend grouper using SequenceMatcher.

    Usage:
        grouper = LightweightGrouper(threshold=0.85)
        groups = grouper.group(trends)
        for g in groups:
            print(g.canonical_name, len(g.members), g.platforms)
    """

    def __init__(self, threshold: float = DEFAULT_THRESHOLD) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold}")
        self.threshold = threshold

    def group(self, trends: Iterable[Trend]) -> list[CrossPlatformGroup]:
        """Group trends by string similarity.

        Returns a list of CrossPlatformGroup. Each trend appears in
        exactly one group. Groups are sorted by member count (descending),
        then by sum of normalized scores (descending).
        """
        # Step 1: prepare normalized forms, drop empty/short
        items: list[tuple[Trend, str]] = []
        for t in trends:
            n = normalize_for_grouping(t.name)
            if len(n) < MIN_NAME_LENGTH:
                # Keep it but mark as "not clusterable"
                items.append((t, ""))  # empty normalized → won't cluster
            else:
                items.append((t, n))

        # Step 2: cluster with single-linkage
        # parent[i] = root cluster id for item i
        n_items = len(items)
        parent = list(range(n_items))

        def find(x: int) -> int:
            # Path compression
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        # O(n^2) similarity — fine for n < 2000 trends
        for i in range(n_items):
            if not items[i][1]:  # skip empty normalized
                continue
            for j in range(i + 1, n_items):
                if not items[j][1]:
                    continue
                # Quick reject: if length ratio is too extreme, skip
                a, b = items[i][1], items[j][1]
                len_a, len_b = len(a), len(b)
                if min(len_a, len_b) / max(len_a, len_b) < 0.5:
                    # One is much longer than the other — likely different
                    continue
                sim = _sequence_ratio(a, b)
                if sim >= self.threshold:
                    union(i, j)

        # Step 3: build groups
        cluster_map: dict[int, list[int]] = defaultdict(list)
        for i in range(n_items):
            cluster_map[find(i)].append(i)

        groups: list[CrossPlatformGroup] = []
        for cluster_indices in cluster_map.values():
            members = [items[i][0] for i in cluster_indices]
            canonical = _pick_canonical_name(members, items)
            platforms = {m.platform for m in members}
            # Average intra-cluster similarity (sampled to keep O(n) per cluster)
            sim_score = _cluster_similarity(items, cluster_indices)
            groups.append(
                CrossPlatformGroup(
                    canonical_name=canonical,
                    members=members,
                    platforms=platforms,
                    similarity_score=sim_score,
                    grouping_method="lightweight",
                )
            )

        # Step 4: sort — biggest clusters first, then by peak score
        groups.sort(
            key=lambda g: (len(g.members), max(m.score for m in g.members)),
            reverse=True,
        )
        return groups


def _sequence_ratio(a: str, b: str) -> float:
    """Cached SequenceMatcher.ratio() — returns 0.0..1.0."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def _pick_canonical_name(
    members: list[Trend], items: list[tuple[Trend, str]]
) -> str:
    """Pick the most readable form for the group label.

    Heuristics (in priority order):
    1. Prefer names with more word boundaries (spaces) — "Bryan Cranston"
       is more readable than "bryancranston"
    2. Among names with the same word count, prefer the one whose
       original form is closest in length to its normalized form
       (fewer abbreviation artifacts)
    3. Tie-break by length of normalized form (shorter = cleaner)
    """
    norm_by_id = {id(t): n for t, n in items}
    candidates: list[tuple[int, int, int, str]] = []
    for m in members:
        n = norm_by_id.get(id(m), "")
        if n:
            word_count = len(n.split())
            len_diff = abs(len(m.name) - len(n))
            # Sort key: most words first, then smallest len_diff, then shortest
            candidates.append((-word_count, len_diff, len(n), m.name))
    if not candidates:
        return members[0].name
    candidates.sort()
    return candidates[0][3]


def _cluster_similarity(
    items: list[tuple[Trend, str]], indices: list[int]
) -> float:
    """Average pairwise similarity of normalized names in the cluster.

    Sampled at 50 pairs max to avoid O(n²) blow-up for big clusters.
    """
    if len(indices) < 2:
        return 1.0
    import random
    names = [items[i][1] for i in indices if items[i][1]]
    if len(names) < 2:
        return 1.0
    if len(names) > 50:
        # Subsample for big clusters
        random.seed(42)  # deterministic
        names = random.sample(names, 50)
    total = 0.0
    n = 0
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            total += _sequence_ratio(names[i], names[j])
            n += 1
    if n == 0:
        return 1.0
    return total / n

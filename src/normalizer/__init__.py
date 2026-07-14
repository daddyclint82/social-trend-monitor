"""Normalizer package: Trend schema, dedupe, cross-platform join, semantic grouping."""
from .schema import (
    PLATFORMS,
    TREND_TYPES,
    Trend,
    TrendSignal,
    make_cross_platform_key,
    make_trend,
    make_trend_id,
)
from .semantic import SemanticGrouper, TrendGroup

__all__ = [
    "PLATFORMS",
    "TREND_TYPES",
    "Trend",
    "TrendSignal",
    "make_cross_platform_key",
    "make_trend",
    "make_trend_id",
    "SemanticGrouper",
    "TrendGroup",
]

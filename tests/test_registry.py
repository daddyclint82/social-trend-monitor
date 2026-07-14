"""Tests for the auto-discovery collector registry."""
from __future__ import annotations

from src.collectors.base import BaseCollector
from src.collectors.registry import CollectorRegistry


def test_registry_discovers_all_platforms():
    r = CollectorRegistry()
    r.discover()
    available = r.available()
    # All expected platforms are registered (TikTok now split into 2 — ADR-0014)
    assert "tiktok_oembed" in available
    assert "tiktok_discover" in available
    assert "x" in available
    assert "instagram" in available
    assert "facebook" in available
    # The legacy generic "tiktok" key is gone — it would have shadowed one collector
    assert "tiktok" not in available


def test_registry_get_returns_class():
    r = CollectorRegistry()
    r.discover()
    cls = r.get("tiktok_oembed")
    assert cls is not None
    assert issubclass(cls, BaseCollector)
    assert cls.platform == "tiktok_oembed"


def test_registry_get_unknown_returns_none():
    r = CollectorRegistry()
    r.discover()
    assert r.get("myspace") is None


def test_all_collectors_have_unique_platform():
    """The registry must never silently shadow one collector with another.
    This is the regression test for the 2026-07-13 TikTok collision — see ADR-0014.
    """
    r = CollectorRegistry()
    r.discover()
    platforms = [c.platform for c in r.all()]
    assert len(platforms) == len(set(platforms)), (
        f"Duplicate platform keys: "
        f"{[p for p in platforms if platforms.count(p) > 1]}"
    )


def test_tiktok_oembed_and_discover_both_register():
    """ADR-0014 — both tiktok collectors must be available simultaneously."""
    r = CollectorRegistry()
    r.discover()
    oembed = r.get("tiktok_oembed")
    discover = r.get("tiktok_discover")
    assert oembed is not None
    assert discover is not None
    assert oembed is not discover

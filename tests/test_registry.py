"""Tests for the auto-discovery collector registry."""
from __future__ import annotations

from src.collectors.base import BaseCollector
from src.collectors.registry import CollectorRegistry


def test_registry_discovers_all_platforms():
    r = CollectorRegistry()
    r.discover()
    available = r.available()
    # All 4 expected platforms are registered
    assert "tiktok" in available
    assert "x" in available
    assert "instagram" in available
    assert "facebook" in available


def test_registry_get_returns_class():
    r = CollectorRegistry()
    r.discover()
    cls = r.get("tiktok")
    assert cls is not None
    assert issubclass(cls, BaseCollector)
    assert cls.platform == "tiktok"


def test_registry_get_unknown_returns_none():
    r = CollectorRegistry()
    r.discover()
    assert r.get("myspace") is None


def test_all_collectors_have_unique_platform():
    r = CollectorRegistry()
    r.discover()
    platforms = [c.platform for c in r.all()]
    assert len(platforms) == len(set(platforms))

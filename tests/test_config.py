"""Test the AppConfig YAML loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config import AppConfig, load_config


def test_load_config_defaults(tmp_path: Path):
    # Point at a non-existent file → defaults
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert isinstance(cfg, AppConfig)
    assert cfg.storage.db_path == "./data/trends.db"
    assert cfg.rate_limits.jitter_pct == 0.5


def test_load_config_real_default(tmp_path: Path):
    # Load the project's actual default config
    cfg = load_config()
    # TikTok is namespaced into 2 collectors as of 2026-07-13 (ADR-0014)
    assert "tiktok_oembed" in cfg.collectors
    assert cfg.collectors["tiktok_oembed"].enabled is True
    assert "tiktok_discover" in cfg.collectors
    assert cfg.collectors["tiktok_discover"].enabled is True
    assert cfg.collectors["x"].enabled is True
    assert "api.x.com" in cfg.rate_limits.per_host


def test_collector_options_loaded():
    cfg = load_config()
    # TikTok oembed — user-supplied hashtags/creators (ADR-0002 revised)
    assert "hashtags" in cfg.collector_options["tiktok_oembed"]
    assert "creator_urls" in cfg.collector_options["tiktok_oembed"]
    # TikTok discover — community-scraped trending (ADR-0013)
    assert "regions" in cfg.collector_options["tiktok_discover"]
    assert cfg.collector_options["x"]["bearer_token_env"] == "X_BEARER_TOKEN"

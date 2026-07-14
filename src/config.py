"""Pydantic settings + YAML loader."""
from __future__ import annotations

import structlog
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

logger = structlog.get_logger(__name__)

# Project root = parent of src/ = parents[1] of this file
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "default.yaml"
ENV_FILE_PATH = Path(__file__).resolve().parents[1] / ".env"


class HostRateLimit(BaseModel):
    rate: float
    burst: int | None = None


class CollectorConfig(BaseModel):
    enabled: bool = True
    poll_interval_min: int = 15


class RateLimitsConfig(BaseModel):
    default: HostRateLimit = Field(default_factory=lambda: HostRateLimit(rate=0.2, burst=5))
    per_host: dict[str, HostRateLimit] = Field(default_factory=dict)
    jitter_pct: float = 0.5

    @field_validator("jitter_pct")
    @classmethod
    def _clamp_jitter(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class StorageConfig(BaseModel):
    db_path: str = "./data/trends.db"
    retention_days: int = 60


class LLMConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://localhost:11434"
    model: str = "llama3.1:8b"
    format_extraction_interval_h: int = 6


class LoggingConfig(BaseModel):
    level: str = "INFO"
    log_json: bool = Field(default=True, alias="json")
    path: str = "./logs/social-trend-monitor.log"


class AppConfig(BaseModel):
    collectors: dict[str, CollectorConfig] = Field(default_factory=dict)
    collector_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config from YAML, falling back to defaults."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict = {}
    if cfg_path.exists():
        try:
            with cfg_path.open() as f:
                data = yaml.safe_load(f) or {}
            logger.info("config.loaded", path=str(cfg_path))
        except Exception as e:  # noqa: BLE001
            logger.warning("config.load_failed", path=str(cfg_path), error=str(e))
    else:
        logger.info("config.using_defaults", path=str(cfg_path))

    # Allow environment overrides for sensitive bits
    if os.environ.get("X_BEARER_TOKEN"):
        data.setdefault("collectors", {}).setdefault("x", {})["bearer_token_env"] = "X_BEARER_TOKEN"

    return AppConfig.model_validate(data)

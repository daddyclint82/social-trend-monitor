"""Social Trend Monitor — multi-platform trending content discovery.

Entry point: ``python -m social_trend_monitor`` (or the ``cli`` module
imported directly).

Package layout:
    src.collectors       — per-platform data collectors (auto-discovered)
    src.normalizer       — Trend schema + cross-platform join
    src.scoring          — velocity / cross-platform bonus / decay
    src.storage          — SQLite persistence
    src.utils            — rate limiter, retry helpers
    src.orchestrator     — cycle runner
"""
from __future__ import annotations

import logging
import os
import sys

__version__ = "0.1.0"

# Bootstrap structlog once at import time. Until the CLI's _setup_logging
# runs we still want collector log calls to be valid structlog calls.
try:
    import structlog

    if not structlog.is_configured():
        level_name = os.environ.get("STM_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        logging.basicConfig(
            format="%(message)s",
            stream=sys.stdout,
            level=level,
        )
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.dev.ConsoleRenderer(colors=False),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            cache_logger_on_first_use=True,
        )
except ImportError:
    # structlog not available — stdlib logging will be used directly.
    # Collectors must then use %-style formatting instead of kw args.
    pass

"""Auto-discovery registry for collectors.

Drop a file in `src/collectors/platforms/` that defines a `BaseCollector`
subclass. The registry picks it up on next startup. No central dispatch.
"""
from __future__ import annotations

import importlib
import structlog
import pkgutil
from typing import TYPE_CHECKING

from .base import BaseCollector

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

_PLATFORMS_PKG = "src.collectors.platforms"


class CollectorRegistry:
    """Discovers and instantiates collectors.

    Usage:
        registry = CollectorRegistry()
        registry.discover()  # imports all platform modules
        collectors = registry.all()  # list[BaseCollector] (no instances yet)
        for ctor in collectors:
            instance = ctor(http_client, limiter, config)
            trends = await instance.collect()
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[BaseCollector]] = {}

    def discover(self) -> None:
        """Import every module in src.collectors.platforms and register
        any BaseCollector subclasses found."""
        try:
            package = importlib.import_module(_PLATFORMS_PKG)
        except ImportError as e:
            logger.warning("registry.import_failed", package=_PLATFORMS_PKG, error=str(e))
            return

        for _finder, name, _is_pkg in pkgutil.iter_modules(package.__path__):
            full_name = f"{_PLATFORMS_PKG}.{name}"
            try:
                module = importlib.import_module(full_name)
            except Exception as e:  # noqa: BLE001 — discover must not crash
                logger.warning("registry.module_import_failed", module=full_name, error=str(e))
                continue
            for attr_name in dir(module):
                attr = getattr(module, attr_name, None)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseCollector)
                    and attr is not BaseCollector
                    and attr.platform
                ):
                    if attr.platform in self._classes:
                        logger.warning(
                            "registry.duplicate_platform",
                            platform=attr.platform,
                            existing=self._classes[attr.platform].__name__,
                            new=attr.__name__,
                        )
                    self._classes[attr.platform] = attr
                    logger.info("registry.registered", platform=attr.platform, cls=attr.__name__)

    def available(self) -> list[str]:
        """List of platforms that have registered collectors."""
        return sorted(self._classes.keys())

    def get(self, platform: str) -> type[BaseCollector] | None:
        return self._classes.get(platform)

    def all(self) -> list[type[BaseCollector]]:
        return [self._classes[p] for p in sorted(self._classes.keys())]

"""conftest.py — pytest configuration.

Triggers package initialization (which configures structlog) before
any test imports collector modules directly.
"""
# Importing the package executes src/__init__.py, which sets up structlog.
import src  # noqa: F401

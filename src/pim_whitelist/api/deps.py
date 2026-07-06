"""Dependency-injection singletons.

``get_settings`` and ``get_index`` are ``lru_cache``-d so the index is built once
per process (at first request / cold start) and reused across warm invocations.
Both are overridable in tests via ``app.dependency_overrides``.
"""

from __future__ import annotations

from functools import lru_cache

from .index import PimIndex, load_index
from .settings import Settings, load_settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


@lru_cache(maxsize=1)
def get_index() -> PimIndex:
    return load_index(get_settings().data_dir)

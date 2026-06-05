"""Platforms source: GCMD platforms CSV."""

from __future__ import annotations

from ..config import PLATFORMS_URL
from .csv_source import CsvSource


class PlatformsSource(CsvSource):
    url = PLATFORMS_URL
    cache_filename = "gcmd_platforms.csv"

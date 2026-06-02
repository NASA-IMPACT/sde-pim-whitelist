"""Instruments source: GCMD instruments CSV."""

from __future__ import annotations

from ..config import INSTRUMENTS_URL
from .csv_source import CsvSource


class InstrumentsSource(CsvSource):
    url = INSTRUMENTS_URL
    cache_filename = "gcmd_instruments.csv"

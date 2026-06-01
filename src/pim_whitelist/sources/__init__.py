"""Upstream source fetchers/parsers."""

from __future__ import annotations

from ..config import DatasetConfig
from .base import SourceConcept
from .instruments import InstrumentsSource
from .missions import MissionsSource
from .platforms import PlatformsSource

SOURCES = {
    "platforms": PlatformsSource,
    "instruments": InstrumentsSource,
    "missions": MissionsSource,
}


def get_source(dataset: DatasetConfig):
    return SOURCES[dataset.name](dataset)


__all__ = [
    "SourceConcept",
    "PlatformsSource",
    "InstrumentsSource",
    "MissionsSource",
    "SOURCES",
    "get_source",
]

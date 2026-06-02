"""Upstream source fetchers/parsers."""

from __future__ import annotations

from ..config import DatasetConfig
from .base import Source, SourceConcept
from .instruments import InstrumentsSource
from .missions import MissionsSource
from .platforms import PlatformsSource
from .sde import SdeInstrumentsSource, SdePlatformsSource

# Each dataset may be backed by several sources; their concepts are combined
# (de-duplicated by match key) before diffing against the whitelist.
SOURCES: dict[str, list[type[Source]]] = {
    "platforms": [PlatformsSource, SdePlatformsSource],
    "instruments": [InstrumentsSource, SdeInstrumentsSource],
    "missions": [MissionsSource],
}


def get_sources(dataset: DatasetConfig) -> list[Source]:
    return [cls(dataset) for cls in SOURCES[dataset.name]]


__all__ = [
    "SourceConcept",
    "PlatformsSource",
    "InstrumentsSource",
    "MissionsSource",
    "SdePlatformsSource",
    "SdeInstrumentsSource",
    "SOURCES",
    "get_sources",
]

"""Pydantic request/response models — the API's typed contract.

Everything crossing the wire is validated by one of these models: the division
enum (query + record), the pagination query params, the response record, and the
page envelope. Response records are projected from the on-disk
:class:`~pim_whitelist.classify.ClassifiedRecord` via
:meth:`PimRecord.from_classified`, renaming fields to the public shape the user
specified (``canonical_name`` / ``division`` / ``source``).
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, Field

from .. import config
from ..classify import ClassifiedRecord
from .settings import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX, PAGE_SIZE_MIN


class Division(str, enum.Enum):
    """The NASA SMD science divisions (the fixed classifier taxonomy).

    Declared statically for type-checker friendliness; the assertion below guards
    against drift from the single source of truth, ``config.DIVISIONS``.
    """

    earth = "earth"
    heliophysics = "heliophysics"
    planetary = "planetary"
    astrophysics = "astrophysics"
    bps = "bps"


assert tuple(d.value for d in Division) == tuple(
    config.DIVISIONS
), "Division enum is out of sync with config.DIVISIONS"


class PimType(str, enum.Enum):
    """The kind of PIM a record describes (singular, public-facing)."""

    instrument = "instrument"
    platform = "platform"
    mission = "mission"


# Map the sync-side dataset names (plural: config.DATASETS keys) to PimType.
DATASET_TO_PIM_TYPE: dict[str, PimType] = {
    "instruments": PimType.instrument,
    "platforms": PimType.platform,
    "missions": PimType.mission,
}

# division_source (on disk) -> public source label. Provenance came from the SDE
# crawl; everything else was tagged by the LLM classifier.
_SOURCE_LABELS: dict[str, Literal["SDE", "LLM"]] = {
    "provenance": "SDE",
    "openai": "LLM",
}


class PimRecord(BaseModel):
    """A single served PIM record (the public response shape)."""

    canonical_name: str
    # Mirrors the on-disk record: true alternates only, null when there are none.
    aliases: list[str] | None
    division: list[Division]
    source: Literal["SDE", "LLM"]
    type: PimType

    @classmethod
    def from_classified(cls, rec: ClassifiedRecord, pim_type: PimType) -> PimRecord:
        return cls(
            canonical_name=rec.canonical,
            aliases=rec.aliases,
            division=[Division(d) for d in rec.divisions],
            source=_SOURCE_LABELS[rec.division_source],
            type=pim_type,
        )


class PageParams(BaseModel):
    """Validated pagination + filter query parameters.

    Used as a FastAPI query-parameter model, so out-of-range values (page < 1,
    page_size outside 50–100) and unknown division names return 422 automatically.
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(
        default=PAGE_SIZE_DEFAULT, ge=PAGE_SIZE_MIN, le=PAGE_SIZE_MAX
    )
    division: Division | None = None


class Page(BaseModel):
    """Pagination envelope returned by every list endpoint."""

    items: list[PimRecord]
    page: int
    page_size: int
    total: int
    total_pages: int

    @classmethod
    def build(cls, records: list[PimRecord], *, page: int, page_size: int) -> Page:
        total = len(records)
        # ceil division without float; 0 records -> 0 pages.
        total_pages = (total + page_size - 1) // page_size
        start = (page - 1) * page_size
        items = records[start : start + page_size]
        return cls(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        )

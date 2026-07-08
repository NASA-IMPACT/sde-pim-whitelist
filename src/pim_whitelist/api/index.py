"""In-memory index over the classified PIM sidecars.

Built once at cold start from the three ``whitelist/classified/*.json`` files and
reused across warm Lambda invocations. Records are validated in bulk with a
``TypeAdapter`` (cheaper than per-item ``model_validate``) and projected to
:class:`~pim_whitelist.api.models.PimRecord`, preserving on-disk order (already
sorted by ``match_key``).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from pydantic import TypeAdapter

from ..classify import ClassifiedRecord
from .models import DATASET_TO_PIM_TYPE, Division, PimRecord, PimType

logger = logging.getLogger(__name__)

# One adapter reused for every file (validating list[ClassifiedRecord] in one call).
_RECORDS_ADAPTER = TypeAdapter(list[ClassifiedRecord])

# Deterministic order for the flat combined listing (base endpoint).
_TYPE_ORDER = (PimType.instrument, PimType.platform, PimType.mission)


class PimIndex:
    """Holds the served records per type plus a content-hash data version."""

    def __init__(
        self, by_type: dict[PimType, list[PimRecord]], data_version: str
    ) -> None:
        self._by_type = by_type
        # Flat combined list in a stable, deterministic order.
        self._all: list[PimRecord] = [
            r for t in _TYPE_ORDER for r in by_type.get(t, [])
        ]
        self.data_version = data_version

    @classmethod
    def load(cls, data_dir: Path) -> PimIndex:
        """Load and validate all sidecars under ``data_dir``.

        Tolerant of a missing file (that type loads empty) so the app can still
        start and ``/healthz`` can report the gap as 503 rather than crashing the
        whole handler at import time.
        """
        by_type: dict[PimType, list[PimRecord]] = {}
        hasher = hashlib.sha256()
        # Sort by dataset name for a stable data_version hash.
        for name, pim_type in sorted(DATASET_TO_PIM_TYPE.items()):
            path = data_dir / f"{name}.json"
            if not path.exists():
                logger.warning("classified sidecar missing: %s", path)
                by_type[pim_type] = []
                continue
            raw = path.read_bytes()
            hasher.update(raw)
            records = _RECORDS_ADAPTER.validate_json(raw)
            by_type[pim_type] = [
                PimRecord.from_classified(rec, pim_type) for rec in records
            ]
            logger.info("loaded %d %s records", len(records), name)
        return cls(by_type=by_type, data_version=hasher.hexdigest()[:16])

    def list(
        self,
        pim_type: PimType | None = None,
        division: Division | None = None,
    ) -> list[PimRecord]:
        """Records for one type (or all, flat), optionally filtered by division.

        The division filter is a membership test over each record's divisions;
        records with no divisions are excluded by any filter but included when
        no filter is given.
        """
        records = self._all if pim_type is None else self._by_type.get(pim_type, [])
        if division is None:
            return records
        return [r for r in records if division in r.division]

    def counts(self) -> dict[str, int]:
        """Per-type record counts (for the readiness probe / stats)."""
        return {t.value: len(self._by_type.get(t, [])) for t in _TYPE_ORDER}

    def is_ready(self) -> bool:
        """True iff every dataset loaded at least one record."""
        return all(self._by_type.get(t) for t in _TYPE_ORDER)


def load_index(data_dir: Path) -> PimIndex:
    return PimIndex.load(data_dir)

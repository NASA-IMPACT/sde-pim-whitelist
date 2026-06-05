"""SDE source: NASA Science Discovery Engine search API.

The SDE indexes datasets from every SMD science division. A single crawl over
:data:`~pim_whitelist.config.SDE_API_SOURCES` surfaces the ``platform`` and
``instrument`` values attached to those datasets — names that complement the
GCMD keyword lists. Because one crawl yields *both* fields, the result is
memoized for the process so ``update --dataset all`` hits the API only once;
the platforms and instruments sources then each slice their own field out of
the shared crawl.
"""

from __future__ import annotations

import json
import logging

import requests

from ..config import SDE_API_SOURCES, SDE_PAGE_SIZE, SDE_URL
from .base import Provenance, Source, SourceConcept, make_concept

logger = logging.getLogger(__name__)

FIELDS = ("platform", "instrument")

# Sentinel values the SDE returns for "no value"; compared case-insensitively
# against the stripped string.
_JUNK = {"", "[]", "not provided", "not applicable"}

# Process-level memo so a multi-dataset run crawls the corpus only once.
_CRAWL: dict[str, list[dict]] | None = None


def _clean_values(raw_value) -> list[str]:
    """Normalize a document's field value into a list of usable strings.

    The value may be ``None``, a single string (possibly ``;``-delimited), or a
    list. Junk sentinels are dropped.
    """
    if raw_value is None:
        return []
    pieces: list[str] = []
    items = raw_value if isinstance(raw_value, list) else [raw_value]
    for item in items:
        if not isinstance(item, str):
            continue
        for part in item.split(";"):
            part = part.strip()
            if part and part.casefold() not in _JUNK:
                pieces.append(part)
    return pieces


def crawl(session: requests.Session) -> dict[str, list[dict]]:
    """Crawl every collection key once, returning per-field value rows.

    Each row is ``{"value": str, "collection_key": str}``. Duplicates are not
    removed here; cross-row/cross-source de-duplication happens later in
    :func:`pim_whitelist.diff.merge_source_concepts`.
    """
    out: dict[str, list[dict]] = {field: [] for field in FIELDS}
    for collection_key in SDE_API_SOURCES:
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = {
                "search_term": "",
                "page": page,
                "page_size": SDE_PAGE_SIZE,
                "search_type": "keyword",
                "filters": {"collection_key": [collection_key]},
            }
            resp = session.post(SDE_URL, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            pagination = data.get("pagination", {})
            if page == 1:
                total_pages = int(pagination.get("total_pages", 1) or 1)
                logger.info("  SDE %s: %d page(s)", collection_key, total_pages)
            for doc in data.get("documents", []):
                for field in FIELDS:
                    for value in _clean_values(doc.get(field)):
                        out[field].append(
                            {"value": value, "collection_key": collection_key}
                        )
            page += 1
    return out


def crawl_cached(session: requests.Session) -> dict[str, list[dict]]:
    global _CRAWL
    if _CRAWL is None:
        _CRAWL = crawl(session)
    return _CRAWL


def _reset_crawl_cache() -> None:
    """Clear the process-level crawl memo (used by tests)."""
    global _CRAWL
    _CRAWL = None


class SdeSource(Source):
    """Base for the SDE platform/instrument sources.

    ``field`` selects which slice of the shared crawl this source emits.
    """

    field: str = ""

    def fetch_raw(self, session: requests.Session) -> bytes:
        rows = crawl_cached(session)[self.field]
        return json.dumps(rows, ensure_ascii=False).encode("utf-8")

    def parse(self, raw: bytes) -> list[SourceConcept]:
        rows = json.loads(raw.decode("utf-8"))
        concepts: list[SourceConcept] = []
        for row in rows:
            concept = make_concept(
                [row["value"]],
                Provenance(origins=["sde"], collection_keys=[row["collection_key"]]),
            )
            if concept is not None:
                concepts.append(concept)
        return concepts


class SdePlatformsSource(SdeSource):
    field = "platform"
    cache_filename = "sde_platforms.json"


class SdeInstrumentsSource(SdeSource):
    field = "instrument"
    cache_filename = "sde_instruments.json"

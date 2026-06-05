"""Common source machinery: the SourceConcept record and an HTTP helper."""

from __future__ import annotations

from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import RAW_CACHE_DIR, USER_AGENT, DatasetConfig
from ..normalize import clean, match_key


@dataclass
class Provenance:
    """Where a concept came from, and any source-specific identifiers.

    The cross-cutting fields (``origins``, ``collection_keys``, ``uuid``) form a
    contract shared by every source and understood by the diff/report layers;
    :meth:`merge` and :meth:`copy` are the single place that contract is
    maintained. ``extra`` carries source-specific data that is merely carried
    along (e.g. a mission's id/slug) and is not combined across sources.
    """

    origins: list[str] = field(default_factory=list)
    collection_keys: list[str] = field(default_factory=list)
    uuid: str | None = None
    extra: dict = field(default_factory=dict)

    def copy(self) -> Provenance:
        """Return a deep-ish copy: lists and ``extra`` are fresh containers."""
        return Provenance(
            origins=list(self.origins),
            collection_keys=list(self.collection_keys),
            uuid=self.uuid,
            extra=dict(self.extra),
        )

    def merge(self, other: Provenance) -> None:
        """Fold ``other`` into this one (order-preserving union).

        ``origins`` and ``collection_keys`` are unioned; ``uuid`` is filled only
        if currently unset. ``extra`` is left untouched — concepts that carry
        per-source ``extra`` (missions) are single-source and never merged.
        """
        for origin in other.origins:
            if origin not in self.origins:
                self.origins.append(origin)
        for key in other.collection_keys:
            if key not in self.collection_keys:
                self.collection_keys.append(key)
        if self.uuid is None and other.uuid is not None:
            self.uuid = other.uuid


@dataclass
class SourceConcept:
    """A concept as derived from an upstream source.

    ``aliases`` are already :func:`clean`-ed and de-duplicated by match key,
    canonical first. ``provenance`` carries source-specific identifiers (UUID,
    mission id, taxonomy, …) for the report.
    """

    aliases: list[str]
    provenance: Provenance = field(default_factory=Provenance)

    @property
    def canonical(self) -> str:
        return self.aliases[0] if self.aliases else ""

    def keys(self) -> set[str]:
        return {k for k in (match_key(a) for a in self.aliases) if k}


def make_concept(
    values: list[str], provenance: Provenance | None = None
) -> SourceConcept | None:
    """Build a SourceConcept from raw strings (canonical first).

    Each value is cleaned; empties are dropped; later values whose match key
    duplicates an earlier one are dropped. Returns ``None`` if nothing usable
    remains.
    """
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean(value)
        if not cleaned:
            continue
        key = match_key(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(cleaned)
    if not aliases:
        return None
    return SourceConcept(aliases=aliases, provenance=provenance or Provenance())


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        # POST is included for the SDE search API; the search query is
        # idempotent so retrying it on a 5xx/429 is safe.
        allowed_methods=frozenset({"GET", "POST"}),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


class Source:
    """Base class for a dataset source.

    Subclasses implement :meth:`fetch_raw` (returns bytes) and :meth:`parse`
    (raw bytes -> list[SourceConcept]). The base orchestrates caching. Each
    source owns its own ``cache_filename`` (under ``data/raw/``) so a single
    dataset may combine several sources without their caches colliding.
    """

    cache_filename: str = ""

    def __init__(self, dataset: DatasetConfig):
        self.dataset = dataset

    # -- to be overridden -------------------------------------------------
    def fetch_raw(self, session: requests.Session) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def parse(self, raw: bytes) -> list[SourceConcept]:  # pragma: no cover
        raise NotImplementedError

    # -- orchestration ----------------------------------------------------
    def load(self, *, from_cache: bool = False) -> list[SourceConcept]:
        """Return parsed concepts, fetching from network unless ``from_cache``."""
        cache_path = RAW_CACHE_DIR / self.cache_filename
        if from_cache:
            raw = cache_path.read_bytes()
        else:
            with _session() as session:
                raw = self.fetch_raw(session)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(raw)
        return self.parse(raw)

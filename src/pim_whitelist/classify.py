"""Tag whitelist concepts with NASA SMD science divisions.

Two signals are combined, provenance first:

1. **Provenance** (authoritative) — the SDE crawl surfaced each instrument /
   platform name under one or more collection keys, and each key maps 1:1 to a
   division (:data:`~pim_whitelist.config.COLLECTION_KEY_TO_DIVISION`). The crawl
   is cached on disk, so no network call is needed. Any concept the provenance
   covers takes its divisions from there (``division_source: "provenance"``).

2. **OpenAI** (fallback) — only concepts with *empty* provenance (GCMD-only
   instruments / platforms, and every mission, which the SDE doesn't crawl) are
   sent to an OpenAI chat model, which returns zero or more divisions from the
   fixed taxonomy (``division_source: "openai"``). A strict JSON schema
   constrains the response so each returned division is one of the five valid
   values; opaque identifiers the model can't place come back empty.

Output is one JSON file per dataset under
:data:`~pim_whitelist.config.CLASSIFIED_DIR`, one record per concept, with the
same schema the rest of the project already consumes::

    {"match_key", "canonical", "aliases", "divisions", "division_source"}
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel

from . import config
from .normalize import match_key
from .whitelist import Concept, Whitelist

logger = logging.getLogger(__name__)

# division_source values written into each record.
SOURCE_PROVENANCE = "provenance"
SOURCE_OPENAI = "openai"


class ClassifiedRecord(BaseModel):
    """One persisted classification record — the project's on-disk data contract.

    Validated on load (corrupt or out-of-spec cache entries are rejected) and
    serialized back via :meth:`model_dump`. Field declaration order is preserved
    in the dump, so the written JSON stays byte-identical to prior runs.
    """

    match_key: str
    canonical: str
    # True alternates only — the canonical is not repeated here; null when none.
    aliases: list[str] | None
    divisions: list[str]
    division_source: Literal["provenance", "openai"]


# Retry budget for transient OpenAI failures (rate limits, 5xx, timeouts).
_MAX_RETRIES = 5
_BACKOFF_BASE = 2.0  # seconds; doubled each attempt.

# JSON schema the model must conform to: one {index, divisions[]} per concept.
_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "divisions"],
                "properties": {
                    "index": {"type": "integer"},
                    "divisions": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(config.DIVISIONS)},
                    },
                },
            },
        }
    },
}


class ClassifierError(RuntimeError):
    """Raised for unrecoverable classifier problems (missing key, bad response)."""


def resolve_model(model: str | None) -> str:
    """Resolve the model name: explicit arg, then ``OPENAI_MODEL``, then default."""
    return model or os.environ.get("OPENAI_MODEL") or config.DEFAULT_OPENAI_MODEL


def require_api_key() -> None:
    """Raise a clear error if ``OPENAI_API_KEY`` is not set in the environment."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise ClassifierError(
            "OPENAI_API_KEY is not set. Export it before running classify, e.g.\n"
            "  export OPENAI_API_KEY=sk-..."
        )


def load_provenance_divisions(dataset_name: str) -> dict[str, set[str]]:
    """Map ``match_key`` -> set of divisions from the cached SDE crawl.

    Returns an empty map for datasets with no SDE provenance file (missions), or
    if the cache file is absent (a warning is logged in that case).
    """
    filename = config.SDE_CACHE_FILENAMES.get(dataset_name)
    if filename is None:
        return {}
    path = config.RAW_CACHE_DIR / filename
    if not path.exists():
        logger.warning("No SDE cache at %s; provenance divisions unavailable", path)
        return {}

    rows = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for row in rows:
        division = config.COLLECTION_KEY_TO_DIVISION.get(row.get("collection_key"))
        if division is None:
            continue
        key = match_key(row.get("value", ""))
        if not key:
            continue
        out.setdefault(key, set()).add(division)
    return out


def _provenance_for(concept: Concept, prov_map: dict[str, set[str]]) -> list[str]:
    """Union of provenance divisions across all of a concept's match keys."""
    found: set[str] = set()
    for key in concept.keys():
        found |= prov_map.get(key, set())
    return _valid_divisions(found)


def _make_client():
    """Construct an OpenAI client (imported lazily so the dep is optional)."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise ClassifierError(
            "The 'openai' package is required for classify. Install it with "
            "`uv sync` (it is a project dependency)."
        ) from exc
    return OpenAI()


_SYSTEM_PROMPT = (
    "You are an expert NASA Science Mission Directorate (SMD) data curator. "
    "You assign each given platform / instrument / mission name to the NASA SMD "
    "science division(s) it belongs to. The valid divisions are:\n\n"
    + "\n".join(f"- {d}: {config.DIVISION_DESCRIPTIONS[d]}" for d in config.DIVISIONS)
    + "\n\nRules:\n"
    "- A name may belong to MORE THAN ONE division; include all that apply.\n"
    "- If a name is an opaque identifier or you cannot confidently determine any "
    "division, return an empty list for it. Do not guess.\n"
    "- Use only the exact division keys listed above.\n"
    "- Return a result for every input index, preserving the given indices."
)


def build_messages(batch: list[Concept]) -> list[dict]:
    """Build the chat messages for one batch of concepts.

    Each concept is presented with a stable ``index`` so the response can be
    mapped back even though the model returns an unordered array.
    """
    items = [
        {"index": i, "canonical": c.canonical, "aliases": list(c.aliases)}
        for i, c in enumerate(batch)
    ]
    user = (
        "Classify each of the following names into NASA SMD science divisions. "
        "Return one result object per index.\n\n"
        + json.dumps(items, ensure_ascii=False)
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _valid_divisions(divisions: Iterable[str]) -> list[str]:
    """Keep only recognized divisions, in canonical taxonomy order, de-duplicated."""
    seen = {d for d in divisions if d in config.DIVISIONS}
    return [d for d in config.DIVISIONS if d in seen]


def _is_retryable(exc: Exception) -> bool:
    """True only for transient failures worth a backoff retry.

    Rate limits (429) and server errors (5xx) are transient; so are connection /
    timeout errors, which carry no HTTP status. Other 4xx client errors (400 bad
    request, 401 auth, 404 model-not-found) are permanent — retrying them just
    wastes the backoff budget, so fail fast.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        return True  # connection/timeout/decoding errors: treat as transient
    return status == 429 or status >= 500


def _rejects_temperature(exc: Exception) -> bool:
    """True if the error is the model refusing a non-default ``temperature``.

    The gpt-5 / o-series reasoning models only accept the default temperature and
    return a 400 ``unsupported_value`` for ``temperature=0``.
    """
    text = str(exc)
    return "temperature" in text and (
        "unsupported_value" in text or "does not support" in text
    )


def _build_request(model: str, batch: list[Concept]) -> dict:
    """Build the chat-completions request payload for one batch.

    ``temperature=0`` is requested for determinism; :func:`_request_with_retries`
    drops it if the model rejects it.
    """
    return {
        "model": model,
        "messages": build_messages(batch),
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "division_classification",
                "strict": True,
                "schema": _RESPONSE_SCHEMA,
            },
        },
        "temperature": 0,
    }


def _request_with_retries(
    client, request: dict, *, label: str, n_concepts: int
) -> dict:
    """Send ``request`` with retry/backoff and return the parsed JSON payload.

    Transient errors (rate limits, 5xx, connection/timeout) are retried with
    exponential backoff; permanent ones fail fast. ``temperature`` is dropped and
    the request re-sent immediately if the model rejects it (reasoning models),
    without spending the backoff budget. Raises :class:`ClassifierError` on a
    permanent failure or once the retry budget is exhausted. ``label`` identifies
    the batch in log lines so a stuck/retrying request is visible.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            logger.debug(
                "%s: requesting %d concepts (attempt %d/%d)",
                label,
                n_concepts,
                attempt + 1,
                _MAX_RETRIES,
            )
            started = time.monotonic()
            completion = client.chat.completions.create(**request)
            payload = json.loads(completion.choices[0].message.content)
            logger.debug(
                "%s: response received in %.1fs", label, time.monotonic() - started
            )
            return payload
        except Exception as exc:  # noqa: BLE001 - classified below
            last_exc = exc
            # This model only allows the default temperature: drop it and retry
            # immediately (don't spend the backoff budget on a re-send).
            if "temperature" in request and _rejects_temperature(exc):
                logger.info(
                    "%s: model rejects temperature=0; retrying without it", label
                )
                del request["temperature"]
                continue
            if not _is_retryable(exc):
                raise ClassifierError(f"OpenAI request failed: {exc}") from exc
            if attempt == _MAX_RETRIES - 1:
                raise ClassifierError(
                    f"OpenAI request failed after {_MAX_RETRIES} attempts: {exc}"
                ) from exc
            delay = _BACKOFF_BASE * (2**attempt)
            logger.warning(
                "%s: attempt %d/%d failed (%s); retrying in %.0fs",
                label,
                attempt + 1,
                _MAX_RETRIES,
                exc,
                delay,
            )
            time.sleep(delay)
    raise ClassifierError(str(last_exc))  # pragma: no cover - loop returns or raises


def _parse_batch_response(payload: dict, batch_len: int) -> dict[int, list[str]]:
    """Map the model's results array to ``{batch_index: [divisions]}``.

    Out-of-range or non-integer indices are silently skipped; any index the model
    omits is left for the caller to default to an empty list (handled via ``.get``).
    """
    out: dict[int, list[str]] = {}
    for result in payload.get("results", []):
        idx = result.get("index")
        if isinstance(idx, int) and 0 <= idx < batch_len:
            out[idx] = _valid_divisions(result.get("divisions", []))
    return out


def classify_batch(
    client, model: str, batch: list[Concept], *, label: str = "batch"
) -> dict[int, list[str]]:
    """Classify one batch; return ``{batch_index: [divisions]}``.

    Builds the request, sends it with retry/backoff (:func:`_request_with_retries`),
    and maps the response back by index (:func:`_parse_batch_response`).
    """
    request = _build_request(model, batch)
    payload = _request_with_retries(client, request, label=label, n_concepts=len(batch))
    return _parse_batch_response(payload, len(batch))


def _chunk(seq: list[Concept], size: int) -> list[list[Concept]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def classify_concepts(
    concepts: list[Concept],
    *,
    model: str,
    batch_size: int = config.OPENAI_BATCH_SIZE,
    workers: int = 4,
    client=None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, list[str]]:
    """Classify many concepts; return ``{match_key(canonical): [divisions]}``.

    Batches are dispatched concurrently across a small thread pool. ``on_progress``
    (if given) is called with ``(done, total)`` after each batch completes.
    """
    if not concepts:
        return {}
    if client is None:
        require_api_key()
        client = _make_client()

    batches = _chunk(concepts, batch_size)
    total = len(concepts)
    done = 0
    result: dict[str, list[str]] = {}
    logger.info(
        "Classifying %d concepts in %d batch(es) of %d across %d worker(s)",
        total,
        len(batches),
        batch_size,
        max(1, workers),
    )

    def run(
        item: tuple[int, list[Concept]],
    ) -> tuple[list[Concept], dict[int, list[str]]]:
        index, batch = item
        label = f"batch {index + 1}/{len(batches)}"
        return batch, classify_batch(client, model, batch, label=label)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for batch, mapping in pool.map(run, enumerate(batches)):
            for i, concept in enumerate(batch):
                key = match_key(concept.canonical)
                if key:
                    result[key] = mapping.get(i, [])
            done += len(batch)
            logger.info("classified %d/%d concepts", done, total)
            if on_progress:
                on_progress(done, total)
    return result


def _record(concept: Concept, divisions: list[str], source: str) -> ClassifiedRecord:
    # Concept.aliases[0] is the canonical itself; persist only true alternates.
    aliases = [a for a in concept.aliases if a != concept.canonical]
    return ClassifiedRecord(
        match_key=match_key(concept.canonical),
        canonical=concept.canonical,
        aliases=aliases or None,
        divisions=divisions,
        division_source=source,
    )


def _load_existing(dataset: config.DatasetConfig) -> list[ClassifiedRecord]:
    path = config.CLASSIFIED_DIR / f"{dataset.name}.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [ClassifiedRecord.model_validate(item) for item in raw]


def _resolved_cache(
    records: list[ClassifiedRecord],
) -> dict[str, tuple[list[str], str]]:
    """Map every alias ``match_key`` -> ``(divisions, division_source)`` for
    *resolved* records — those with a non-empty division list, regardless of
    source.

    This is the persistent reuse cache: a concept already resolved on a previous
    run is never re-run. Records with empty divisions are deliberately excluded so
    they get retried each run (they are not "resolved").
    """
    cache: dict[str, tuple[list[str], str]] = {}
    for record in records:
        divisions = _valid_divisions(record.divisions)
        if not divisions:
            continue
        value = (divisions, record.division_source)
        keys = [match_key(a) for a in record.aliases or []]
        if record.match_key:
            keys.append(record.match_key)
        for key in keys:
            if key:
                cache.setdefault(key, value)
    return cache


def _cache_lookup(
    concept: Concept, cache: dict[str, tuple[list[str], str]]
) -> tuple[list[str], str] | None:
    """Resolved ``(divisions, source)`` for ``concept``, or ``None`` if not cached.

    The canonical key is tried first so the result is deterministic when several
    of a concept's aliases happen to be cached.
    """
    canonical_key = match_key(concept.canonical)
    if canonical_key in cache:
        return cache[canonical_key]
    for key in sorted(concept.keys()):
        if key in cache:
            return cache[key]
    return None


def _write(dataset: config.DatasetConfig, records: list[ClassifiedRecord]) -> None:
    config.CLASSIFIED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.CLASSIFIED_DIR / f"{dataset.name}.json"
    out_path.write_text(
        json.dumps([r.model_dump() for r in records], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _partition_by_cache(
    whitelist: Whitelist, cache: dict[str, tuple[list[str], str]]
) -> tuple[list[ClassifiedRecord], list[Concept]]:
    """Split whitelist concepts into cache-reused records and the unresolved rest.

    Blank/placeholder lines (no match key) are skipped entirely.
    """
    reused_records: list[ClassifiedRecord] = []
    unresolved: list[Concept] = []
    for concept in whitelist.concepts:
        if not match_key(concept.canonical):
            continue  # blank/placeholder line
        hit = _cache_lookup(concept, cache)
        if hit is not None:
            divisions, source = hit
            reused_records.append(_record(concept, divisions, source))
        else:
            unresolved.append(concept)
    return reused_records, unresolved


def _resolve_provenance(
    unresolved: list[Concept], dataset_name: str
) -> tuple[list[ClassifiedRecord], list[Concept]]:
    """Resolve what provenance can place; return ``(records, needs_openai)``.

    The (cached) SDE crawl is loaded only when there is something to resolve.
    """
    if not unresolved:
        return [], []
    prov_map = load_provenance_divisions(dataset_name)
    prov_records: list[ClassifiedRecord] = []
    needs_openai: list[Concept] = []
    for concept in unresolved:
        prov = _provenance_for(concept, prov_map)
        if prov:
            prov_records.append(_record(concept, prov, SOURCE_PROVENANCE))
        else:
            needs_openai.append(concept)
    return prov_records, needs_openai


def classify_dataset(
    dataset: config.DatasetConfig,
    *,
    model: str | None = None,
    reclassify: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    batch_size: int = config.OPENAI_BATCH_SIZE,
    workers: int = 4,
    client=None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Classify one dataset and (unless ``dry_run``) write its classified JSON.

    The classified JSON is a persistent cache. Any concept already *resolved* on a
    previous run (non-empty divisions, from provenance or OpenAI) is reused
    verbatim and never re-run. Only concepts that are new or whose cached result
    is empty are resolved — provenance first (free SDE-crawl lookup), then OpenAI
    for whatever provenance can't place. The output is rebuilt from the current
    whitelist, so concepts removed from the ``.txt`` are pruned automatically.

    ``reclassify`` ignores the cache and re-resolves every concept (the clean-slate
    / first run). ``limit`` caps how many concepts are sent *to OpenAI* (cheap test
    runs). ``dry_run`` reports without calling OpenAI or writing.

    Returns a stats dict for reporting.
    """
    model = resolve_model(model)
    whitelist = Whitelist.load(dataset.whitelist_path)
    cache = {} if reclassify else _resolved_cache(_load_existing(dataset))
    logger.info(
        "%s: %d concepts in whitelist, %d resolved keys in cache%s",
        dataset.name,
        len(whitelist.concepts),
        len(cache),
        " (ignored: --reclassify)" if reclassify else "",
    )

    # Pass 1: reuse resolved concepts from the cache; collect the rest.
    reused_records, unresolved = _partition_by_cache(whitelist, cache)
    # Pass 2: resolve the rest — provenance first, OpenAI for the remainder.
    prov_records, needs_openai = _resolve_provenance(unresolved, dataset.name)

    if limit is not None:
        needs_openai = needs_openai[:limit]

    logger.info(
        "%s: reusing %d, %d via provenance, %d to OpenAI%s",
        dataset.name,
        len(reused_records),
        len(prov_records),
        len(needs_openai),
        f" (capped by --limit {limit})" if limit is not None else "",
    )

    if dry_run:
        # Preview only: don't call OpenAI (no key needed) — placeholder records.
        openai_records = [_record(c, [], SOURCE_OPENAI) for c in needs_openai]
    else:
        divisions_by_key = classify_concepts(
            needs_openai,
            model=model,
            batch_size=batch_size,
            workers=workers,
            client=client,
            on_progress=on_progress,
        )
        openai_records = [
            _record(c, divisions_by_key.get(match_key(c.canonical), []), SOURCE_OPENAI)
            for c in needs_openai
        ]

    records = reused_records + prov_records + openai_records
    records.sort(key=lambda r: r.match_key)

    if not dry_run:
        _write(dataset, records)

    return {
        "records": len(records),
        "reused": len(reused_records),
        "provenance": len(prov_records),
        "classified": 0 if dry_run else len(openai_records),
        "to_classify": len(needs_openai),
        "empty": sum(1 for r in records if not r.divisions),
    }

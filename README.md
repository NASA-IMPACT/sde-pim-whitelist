# sde-pim-whitelist

Automated reconciliation of the PIM whitelist files in `whitelist/` against their
upstream NASA sources.

The three consolidated whitelists are alias lists — one concept per line, aliases
separated by `;`, canonical name first:

```
MISR;Multi-Angle Imaging SpectroRadiometer;Multi-angle Imaging Spectro-Radiometer
```

This tool fetches the upstream sources and **rebuilds** each whitelist: it folds
duplicate entries together, attaches new upstream names as aliases of the concept
they match, merges safe acronym variants, and alphabetizes the result — without ever
dropping a hand-curated alias. Every run produces two Markdown reports (a delta of
what's new and a consolidation audit) for a human to review before committing.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (it provisions Python 3.12+ and all
dependencies from `uv.lock`). The `--open-pr` flow additionally needs the GitHub
CLI (`gh`); `classify` needs an `OPENAI_API_KEY` (see below).

```bash
uv sync

# The headline command — fetch every source and rebuild all three whitelist files
# (review the git diff and the reports/ before committing):
uv run pim-whitelist update --dataset all

# No network: just re-fold, de-duplicate, and alphabetize the existing files.
uv run pim-whitelist consolidate --dataset all
```

## HTTP read API

Alongside the sync CLI, the repo ships a read-only FastAPI app
(`pim_whitelist.api.app:app`) that serves the classified whitelists from
`whitelist/classified/`. The same app runs locally under `uvicorn` and on AWS
Lambda via `pim_whitelist.api.handler` (see `docs/read-api-architecture.md` and
`docs/fastapi-lambda-serving.md`).

```bash
uv sync

# Run locally on :8000 with autoreload:
uv run uvicorn pim_whitelist.api.app:app --reload --port 8000
```

Then open <http://localhost:8000/docs> for the interactive Swagger UI.

### Endpoints

All list endpoints live under `/fetch_pims_records` and share one response shape:
a pagination envelope (`items`, `page`, `page_size`, `total`, `total_pages`) whose
items are `{canonical_name, aliases[], division[], source ("SDE"|"LLM"), type}`.
Each response carries an `ETag` and `Cache-Control` so an edge cache can honor
`If-None-Match` → `304`.

| Method + path                         | Description                                  |
| ------------------------------------- | -------------------------------------------- |
| `GET /healthz`                        | Readiness probe (no auth); `200` when every dataset loaded ≥1 record, else `503` with per-type counts. |
| `GET /fetch_pims_records`             | All PIMs, flat + paginated (each item carries a `type`). |
| `GET /fetch_pims_records/instruments` | Instruments only.                            |
| `GET /fetch_pims_records/platforms`   | Platforms only.                              |
| `GET /fetch_pims_records/missions`    | Missions only.                               |

Query params (list endpoints): `page` (≥1, default 1), `page_size` (50–100,
default 50), and `division` (`earth`/`heliophysics`/`planetary`/`astrophysics`/`bps`).
Out-of-range or unknown values return `422`.

```bash
BASE=http://localhost:8000

# Readiness probe
curl -s $BASE/healthz | jq

# All PIMs (default page=1, page_size=50)
curl -s "$BASE/fetch_pims_records" | jq

# A single type, paginated
curl -s "$BASE/fetch_pims_records/missions?page=2&page_size=100" | jq '.page, .total, .total_pages'

# Division filter
curl -s "$BASE/fetch_pims_records/instruments?division=earth" | jq '.total'

# Conditional GET: grab the ETag, send it back → 304
ETAG=$(curl -sI "$BASE/fetch_pims_records/platforms" | tr -d '\r' | awk -F': ' 'tolower($1)=="etag"{print $2}')
curl -s -o /dev/null -w "%{http_code}\n" -H "If-None-Match: $ETAG" "$BASE/fetch_pims_records/platforms"
```

### Configuration

Settings are read from `PIM_*` env vars (or a local `.env` off-Lambda):

| Env var             | Default               | Effect                                            |
| ------------------- | --------------------- | ------------------------------------------------- |
| `PIM_DATA_DIR`      | `whitelist/classified`| Directory holding `{instruments,platforms,missions}.json`. |
| `PIM_CORS_ORIGINS`  | `["*"]`               | Browser origins allowed by CORS (JSON list).      |
| `PIM_CACHE_MAX_AGE` | `300`                 | `Cache-Control` max-age (seconds); `0` disables edge/client caching. |

## Sources

| Dataset      | Source(s)                                                                                | Format            |
| ------------ | ---------------------------------------------------------------------------------------- | ----------------- |
| Platforms    | GCMD `cmr.earthdata.nasa.gov/kms/concepts/concept_scheme/platforms?format=csv` + SDE     | CSV + JSON        |
| Instruments  | GCMD `cmr.earthdata.nasa.gov/kms/concepts/concept_scheme/instruments?format=csv` + SDE   | CSV + JSON        |
| Missions     | `www.nasa.gov/wp-json/wp/v2/mission/`                                                     | paginated JSON    |

Concepts come from the CSV `Short_Name` (canonical) + `Long_Name` (alias); CSV
hierarchy rows (empty `Short_Name`) are skipped. Missions use `title.rendered`
(HTML-unescaped, Unicode-normalized); the endpoint is paginated with
`orderby=id&order=asc` for stable results and empty pages are retried.

Platforms and instruments draw from **two** sources: the GCMD keyword CSVs and
the NASA Science Discovery Engine (SDE) search API
(`science.data.nasa.gov/science-discovery-engine/api/search`). The SDE is
crawled once per run across all collection keys (Earth, Helio, Planetary, BPS,
Astro) and its `platform`/`instrument` values are combined with GCMD's —
de-duplicated by match key — before diffing, so a name in both sources is added
only once. Each added concept/alias is tagged in the report by origin (`gcmd`,
`sde`, or both).

## Repository layout

```
sde-pim-whitelist/
├── pyproject.toml                 # Project metadata, deps, and the `pim-whitelist` script entry point
├── uv.lock                        # Pinned dependency lockfile (managed by uv)
├── README.md                      # This file
├── whitelist/                     # The hand-curated alias lists this tool maintains (the source of truth)
│   ├── SMD_Platforms_Consolidated.txt
│   ├── SMD_Instruments_Consolidated.txt
│   ├── SMD_Missions_Consolidated.txt
│   └── classified/                # Generated <dataset>.json division tags (written by `classify`)
├── data/raw/                      # Cached raw upstream responses (written on fetch; replayed with --from-cache)
│   ├── gcmd_platforms.csv          # GCMD platforms CSV
│   ├── gcmd_instruments.csv        # GCMD instruments CSV
│   ├── sde_platforms.json          # SDE platform crawl
│   ├── sde_instruments.json        # SDE instrument crawl
│   └── missions.json               # NASA missions API
├── reports/                       # Generated Markdown reports: <dataset>-delta-<date>.md + <dataset>-consolidation-<date>.md
├── src/pim_whitelist/             # The package
│   ├── __init__.py                # Package version
│   ├── cli.py                     # Click CLI: the `update`, `consolidate`, and `classify` commands and --open-pr / PR plumbing
│   ├── classify.py                # classify_dataset(): tag concepts with NASA SMD divisions (SDE provenance + OpenAI); ClassifiedRecord schema/validation
│   ├── config.py                  # Static config: URLs, file paths, and the dataset registry
│   ├── normalize.py               # Text core: clean() (stored form) and match_key() (comparison key)
│   ├── whitelist.py               # Concept / Whitelist models + parse/serialize (round-trip safe)
│   ├── diff.py                    # compute_delta() + merge_source_concepts() (combine sources before diffing)
│   ├── consolidate.py             # consolidate(): fold duplicates, safe acronym merge, alphabetize the whole list
│   ├── report.py                  # Render a Delta / ConsolidationReport as Markdown
│   └── sources/                   # Upstream fetchers + parsers (raw bytes → SourceConcept list)
│       ├── __init__.py            # Source registry (per-dataset list) + get_sources()
│       ├── base.py                # Source base class, SourceConcept, make_concept(), HTTP session w/ retries
│       ├── csv_source.py          # Shared GCMD CSV parsing (platforms + instruments)
│       ├── platforms.py           # PlatformsSource (GCMD CSV)
│       ├── instruments.py         # InstrumentsSource (GCMD CSV)
│       ├── sde.py                 # SDE search-API crawler: SdePlatformsSource / SdeInstrumentsSource
│       └── missions.py            # MissionsSource: paginated WordPress REST API w/ empty-page retries
└── tests/
    ├── test_normalize.py          # clean() / match_key() behavior
    ├── test_whitelist_roundtrip.py# Byte-identical parse→serialize on the real whitelist files
    ├── test_diff_merge.py         # Delta computation + cross-source dedup semantics
    ├── test_consolidate.py        # Whole-list consolidation: fold, acronym merge, sort, idempotency, near-dup filtering
    ├── test_sources.py            # GCMD CSV parsing
    ├── test_sde.py                # SDE crawl: pagination, filtering, shared-crawl origin tagging
    ├── test_missions_pagination.py# Mission pagination / dedupe-by-id
    └── test_classify.py           # Division classification: provenance + cache reuse, OpenAI batch retries, ClassifiedRecord schema
```

### What each module does

- **`config.py`** — The single place URLs and paths live. `DatasetConfig` ties a
  dataset name to its whitelist filename and raw-cache filename; `DATASETS` is the
  registry the CLI iterates over.
- **`normalize.py`** — Two deliberately separate operations. `clean()` produces the
  *stored* form (fixes HTML entities / Unicode / whitespace, but preserves case and
  punctuation). `match_key()` produces an aggressive *comparison-only* key (casefold
  + strip all non-alphanumerics) so spelling/spacing/punctuation variants collapse
  together. The match key is never written to a file.
- **`whitelist.py`** — `Concept` is an ordered alias group (`aliases[0]` is
  canonical); `Whitelist` is the ordered list of concepts plus the file's
  trailing-newline state. `parse`/`serialize` are exact inverses, and `build_index()`
  maps every alias match key to its owning concept for fast lookup.
- **`sources/`** — Each source turns raw upstream data into a list of
  `SourceConcept`. `base.py` holds the shared `Source` orchestration (fetch →
  cache → parse), the `make_concept()` helper (cleans + dedupes aliases), and a
  retrying HTTP session. CSV sources share `csv_source.py`; missions have their own
  pagination logic.
- **`diff.py`** — `compute_delta()` compares source concepts against the existing
  whitelist via the match-key index and classifies the result into **new concepts**,
  **new aliases** on existing concepts, and **orphans** (in whitelist, absent
  upstream).
- **`consolidate.py`** — `consolidate()` rebuilds the *whole* list each run: it folds
  the existing whitelist and any new source concepts together by match key (existing
  canonical wins), applies a conservative **acronym merge** (a descriptive entry like
  `Science in Microgravity Box (SIMBOX)` folds into a bare `Simbox`, but distinct
  bare-acronym entries such as `JADE`/`JEDI` are never merged), and sorts the result
  case-insensitively by canonical. The loop runs to a fixed point so a re-run is a
  no-op. Lower-confidence candidates (short acronyms, near-duplicate spellings) are
  surfaced in a `ConsolidationReport` for manual review, never auto-applied. The
  near-duplicate scan deliberately ignores **enumerated series** — entries that differ
  only by a serial/generation token (`BARREL 1A`/`1B`, `ACRIM I`/`II`) or by one slot
  of a large templated family (`CPMN … at <code>`, `Magnetometers at <place>`) — so the
  review list holds genuine typos and variants rather than thousands of distinct siblings.
- **`report.py`** — Renders deltas (`render`/`render_many`) and consolidation reports
  (`render_consolidation`) into the Markdown written to `reports/`.
- **`cli.py`** — Wires it together. `update` fetches sources, computes the delta (for
  the "what's new" report), then rebuilds each file via `consolidate`. `consolidate`
  is a network-free command that re-folds/sorts the existing files in place. Both
  write reports and, unless `--dry-run`, save changed files — optionally opening a PR.

## Processing workflow

For each selected dataset, `update` runs this pipeline (`cli.py:process_dataset`):

```
  whitelist/<file>.txt ──► Whitelist.load ──┬─► compute_delta ──► reports/<ds>-delta-<date>.md
                                            │
  upstream source ──► fetch_raw ──► parse ──┴─► consolidate ──► sorted/deduped Whitelist
       (cached to data/raw/)   (→ SourceConcepts)      │                     │
                                                       ▼                     ▼
                                       reports/<ds>-consolidation-...   save to whitelist/
                                                                        (unless --dry-run)
```

1. **Load** the existing whitelist file into a `Whitelist` of `Concept`s.
2. **Fetch** the upstream source (or read `data/raw/` with `--from-cache`), caching
   the raw bytes, and **parse** it into `SourceConcept`s (aliases cleaned + deduped).
3. **Diff** — build a match-key index of the whitelist and classify each source
   concept as a new concept, a contributor of new aliases, or a match; whitelist
   concepts the source never touches are flagged as orphans. This drives the *delta
   report* only.
4. **Consolidate** — rebuild the whole list: fold the existing whitelist + new
   sources by match key, apply the safe acronym merge, and alphabetize. Earlier
   entries (the curated whitelist) keep their canonical spelling.
5. **Report** — write the delta report *and* a consolidation report
   (`reports/<dataset>-consolidation-<date>.md`) listing applied merges plus
   fuzzy/ambiguous candidates to review.
6. **Write** — unless `--dry-run`, save changed whitelist files; with `--open-pr`,
   branch, commit the files + report, push, and open a PR via `gh`.

Matching throughout is case/whitespace/punctuation-insensitive (via
`normalize.match_key`), while stored aliases keep their original casing and
punctuation.

### How consolidation treats entries

- **Duplicate concepts** (same match key) — folded into one line; the first/curated
  canonical is kept, other spellings become aliases.
- **`Acronym (FULL NAME)` entries** — a descriptive line whose parenthetical names a
  bare acronym entry folds into it (e.g. `… (SIMBOX)` → `Simbox`). Two distinct
  bare-acronym entries are never auto-merged.
- **Orphan** (in whitelist, absent upstream) — kept; listed in the delta report.
- **Low-confidence candidates** — short (≤3-char) acronyms and near-duplicate
  spellings are reported for manual review, never auto-merged.
- **Enumerated series** — entries that differ only by a serial/generation token
  (digits, single letters, roman numerals: `BARREL 1A`/`1B`, `ACRIM I`/`II`) or by one
  slot of a large templated family (`bathythermograph - BT`/`MBT`, `CPMN … at <code>`)
  are recognized as distinct siblings, not typos, and kept out of the near-duplicate
  review list. Isolated pairs are judged on token similarity, so genuine variants
  (`Gage`/`Gauge`, `anemometer`/`anemometers`) still surface.

## Usage

```bash
uv sync

# Apply additions to all three whitelist files (the main command):
uv run pim-whitelist update --dataset all

# Preview the delta without touching any files (prints + writes reports/):
uv run pim-whitelist update --dataset all --dry-run

# Apply, then open a PR via gh:
uv run pim-whitelist update --dataset all --open-pr

# Re-run against the cached raw responses in data/raw/ (offline / reproducible):
uv run pim-whitelist update --dataset platforms --from-cache --dry-run

# Re-fold, de-duplicate, and alphabetize the existing files (no network):
uv run pim-whitelist consolidate --dataset all
uv run pim-whitelist consolidate --dataset instruments --dry-run
```

`--dataset` accepts `platforms`, `instruments`, `missions`, or `all` (default).

| Flag           | Effect                                                                 |
| -------------- | ---------------------------------------------------------------------- |
| `--dataset`    | Which dataset(s) to process: `platforms`/`instruments`/`missions`/`all` (both commands). |
| `--dry-run`    | Report only; do not write whitelist files (both commands).             |
| `--from-cache` | `update` only — reuse cached raw responses in `data/raw/` instead of hitting the network. |
| `--open-pr`    | `update` only — create a branch, commit the updated files + report, and open a PR via `gh`. |

## Classifying concepts by science division

`classify` tags each concept with its NASA SMD science division(s) —
`earth`, `heliophysics`, `planetary`, `astrophysics`, `bps` — and writes one JSON
file per dataset to `whitelist/classified/`. A concept may have several divisions
or none (opaque identifiers come back empty). This is a separate step from
`update`/`consolidate`: edit the whitelist `.txt` files first, then classify.

Two signals are combined, **provenance first**:

1. **SDE provenance** (authoritative, free) — the cached SDE crawl in `data/raw/`
   surfaced each instrument/platform under a collection key that maps 1:1 to a
   division. Any concept it covers is tagged from there (`division_source:
   "provenance"`), no API call. Roughly half the instruments and ~70% of
   platforms are covered this way; missions have no SDE provenance.
2. **OpenAI** (fallback) — only concepts with *empty* provenance are sent to an
   OpenAI model, which assigns divisions from the same taxonomy
   (`division_source: "openai"`).

**The classified JSON is a persistent cache.** Once a concept is *resolved* (a
non-empty division list, from provenance or OpenAI) it is reused verbatim on every
later run and never re-classified. Only **empty** records (the model/provenance
found nothing) and **brand-new** concepts are (re)resolved; concepts removed from
the `.txt` are pruned from the JSON. So the steady-state run is cheap — it touches
only what changed. Use `--reclassify` to ignore the cache and re-resolve everything
(the clean-slate **first run**, which also discards any stale tags from earlier
experiments).

Each record matches the existing schema:

```json
{
  "match_key": "modis",
  "canonical": "MODIS",
  "aliases": ["MODIS", "Moderate Resolution Imaging Spectroradiometer"],
  "divisions": ["earth"],
  "division_source": "provenance"
}
```

Records are validated against a Pydantic model (`ClassifiedRecord`) on load and
re-serialized via `model_dump()`, so a corrupt or out-of-spec cache entry fails
loudly instead of silently propagating, and the written JSON stays byte-identical
to prior runs.

The OpenAI key is read from the environment, or from a `.env` file at the repo
root (see `.env.example`). It is only needed when concepts fall through to the
OpenAI fallback (which is almost always — e.g. every mission):

```bash
# Either export it, or put it in .env:
export OPENAI_API_KEY=sk-...

# See the split without spending anything (no key needed):
uv run pim-whitelist classify --dataset instruments --reclassify --dry-run

# Cheap smoke test — only the first 20 non-provenance instruments hit OpenAI:
uv run pim-whitelist classify --dataset instruments --reclassify --limit 20

# First run (clean slate): resolve every concept, provenance + OpenAI:
uv run pim-whitelist classify --dataset all --reclassify

# Every run after that: reuse resolved records, classify only empty + new ones:
uv run pim-whitelist classify --dataset all

# Long run? -v logs each batch (with timestamps + timing) and any retries to stderr:
uv run pim-whitelist classify --dataset all --verbose
```

A real OpenAI run sends thousands of concepts in batches across a thread pool, so
it can take a while with no output. Pass `-v`/`--verbose` to log the plan and a
timestamped line per batch (and a WARNING with the backoff delay on each retry) —
the timestamps make a stalled or rate-limited request easy to spot.

The model resolves in order: `--model` flag → `$OPENAI_MODEL` → the
`DEFAULT_OPENAI_MODEL` constant in `config.py` (currently `gpt-4o-mini`).

| Flag           | Effect                                                                 |
| -------------- | ---------------------------------------------------------------------- |
| `--dataset`    | Which dataset(s) to classify (`platforms`/`instruments`/`missions`/`all`). |
| `--model`      | OpenAI model override; otherwise `$OPENAI_MODEL` then the config default. |
| `--reclassify` | Ignore the cache and re-resolve every concept (clean slate / first run). |
| `--limit`      | Send at most N concepts to OpenAI — a cheap test run (provenance is always resolved). |
| `--batch-size` | Concepts per OpenAI request (default 50).                              |
| `--dry-run`    | Report only; no OpenAI calls, no files written (no API key required).   |
| `-v`, `--verbose` | Log each batch request/response (with timing) and any retries on stderr — use to watch progress on a long run. |

## Development

```bash
uv run pytest
```

The test suite includes a byte-identical round-trip check on the real whitelist
files, so the parser/serializer can never silently mangle them.

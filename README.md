# sde-pim-whitelist

Automated reconciliation of the PIM whitelist files in `whitelist/` against their
upstream NASA sources.

The three consolidated whitelists are alias lists — one concept per line, aliases
separated by `;`, canonical name first:

```
MISR;Multi-Angle Imaging SpectroRadiometer;Multi-angle Imaging Spectro-Radiometer
```

This tool fetches the upstream sources, computes a **delta** against the current
whitelist, and **merges in additions without ever dropping** hand-curated aliases.
A human reviews the delta before it is committed.

## Quick start

```bash
uv sync

# The headline command — fetch every source and merge additions into all three
# whitelist files (review the git diff before committing):
uv run pim-whitelist update --dataset all
```

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
`sde`, or both). This integrated crawl supersedes the standalone
`sdeAPI_pimsList.py` script, which is kept in the repo for reference.

## Repository layout

```
sde-pim-whitelist/
├── pyproject.toml                 # Project metadata, deps, and the `pim-whitelist` script entry point
├── uv.lock                        # Pinned dependency lockfile (managed by uv)
├── README.md                      # This file
├── whitelist/                     # The hand-curated alias lists this tool maintains (the source of truth)
│   ├── SMD_Platforms_Consolidated.txt
│   ├── SMD_Instruments_Consolidated.txt
│   └── SMD_Missions_Consolidated.txt
├── data/raw/                      # Cached raw upstream responses (written on fetch; replayed with --from-cache)
│   ├── platforms.csv
│   ├── instruments.csv
│   └── missions.json
├── reports/                       # Generated Markdown delta reports, one per run (<dataset>-delta-<date>.md)
├── src/pim_whitelist/             # The package
│   ├── __init__.py                # Package version
│   ├── cli.py                     # Click CLI: the `update` command and --open-pr / PR plumbing
│   ├── config.py                  # Static config: URLs, file paths, and the dataset registry
│   ├── normalize.py               # Text core: clean() (stored form) and match_key() (comparison key)
│   ├── whitelist.py               # Concept / Whitelist models + parse/serialize (round-trip safe)
│   ├── diff.py                    # compute_delta() + merge_source_concepts() (combine sources before diffing)
│   ├── merge.py                   # apply_delta(): append-only merge of a delta into a whitelist
│   ├── report.py                  # Render a Delta as a Markdown report (additions tagged by origin)
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
    ├── test_diff_merge.py         # Delta computation + append-only merge semantics
    ├── test_sources.py            # GCMD CSV parsing
    └── test_missions_pagination.py# Mission pagination / dedupe-by-id
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
- **`merge.py`** — `apply_delta()` applies a delta to a *copy* of the whitelist,
  **only ever adding**: new aliases append to their concept's line, new concepts
  append to the end of the file. Nothing is reordered or removed.
- **`report.py`** — Renders one or many deltas into the Markdown report that is
  written to `reports/` and used as the PR body.
- **`cli.py`** — Wires it together: for each selected dataset it loads the whitelist,
  fetches the source, computes the delta, builds the merged whitelist, prints/writes
  the report, and (unless `--dry-run`) saves the files — optionally opening a PR.

## Processing workflow

For each selected dataset, `update` runs this pipeline (`cli.py:process_dataset`):

```
  whitelist/<file>.txt ──► Whitelist.load ──┐
                                            ├─► compute_delta ──► apply_delta ──► merged Whitelist
  upstream source ──► fetch_raw ──► parse ──┘         │                                  │
       (cached to data/raw/)   (→ SourceConcepts)     ▼                                  ▼
                                              report.render_many                   save to whitelist/
                                              (→ reports/<...>.md)                  (unless --dry-run)
```

1. **Load** the existing whitelist file into a `Whitelist` of `Concept`s.
2. **Fetch** the upstream source (or read `data/raw/` with `--from-cache`), caching
   the raw bytes, and **parse** it into `SourceConcept`s (aliases cleaned + deduped).
3. **Diff** — build a match-key index of the whitelist and classify each source
   concept as a new concept, a contributor of new aliases, or a match; whitelist
   concepts the source never touches are flagged as orphans.
4. **Merge** — apply the delta to a copy of the whitelist, appending only.
5. **Report** — render the combined delta to `reports/<dataset>-delta-<date>.md`
   and echo it to the terminal.
6. **Write** — unless `--dry-run`, save changed whitelist files; with `--open-pr`,
   branch, commit the files + report, push, and open a PR via `gh`.

Matching throughout is case/whitespace/punctuation-insensitive (via
`normalize.match_key`), while stored aliases keep their original casing and
punctuation.

### How merging classifies changes

- **New concept** — appended to the end of the file (so the diff is additive).
- **New alias** on an existing concept — appended to that concept's line.
- **Orphan** (in whitelist, absent upstream) — kept untouched, listed in the report
  for review.

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
```

`--dataset` accepts `platforms`, `instruments`, `missions`, or `all` (default).

| Flag           | Effect                                                                 |
| -------------- | ---------------------------------------------------------------------- |
| `--dataset`    | Which dataset(s) to process: `platforms`/`instruments`/`missions`/`all`. |
| `--dry-run`    | Report only; do not write whitelist files.                             |
| `--from-cache` | Reuse cached raw responses in `data/raw/` instead of hitting the network. |
| `--open-pr`    | Create a branch, commit the updated files + report, and open a PR via `gh`. |

## Development

```bash
uv run pytest
```

The test suite includes a byte-identical round-trip check on the real whitelist
files, so the parser/serializer can never silently mangle them.

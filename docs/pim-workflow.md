# PIM Whitelist Workflow — Retrieval, Text Processing, Consolidation & Classification

End-to-end reference for how the **P**latforms / **I**nstruments / **M**issions (PIM) whitelists
are built: from fetching upstream NASA sources, through text normalization and deduplication, into
consolidation, and finally division classification. Every stage below names the concrete module,
function, and (where useful) line so the doc can be checked against the code.

The three curated outputs (`config.py:28`) are:

| Dataset | Whitelist file | Sources |
|---|---|---|
| `platforms` | `whitelist/SMD_Platforms_Consolidated.txt` | GCMD platforms CSV + SDE crawl (`platform` field) |
| `instruments` | `whitelist/SMD_Instruments_Consolidated.txt` | GCMD instruments CSV + SDE crawl (`instrument` field) |
| `missions` | `whitelist/SMD_Missions_Consolidated.txt` | NASA WordPress missions API |

Source-to-dataset wiring lives in `sources/__init__.py:14` (`SOURCES`).

---

## 0. The pipeline at a glance

```
                 ┌──────────────── update (network) ─────────────────┐
                 │                                                    │
   ┌─────────┐   ┌──────────┐   ┌──────────────────┐   ┌──────────────────────┐
   │ upstream │──►│ Source   │──►│ merge_source_    │──►│ compute_delta        │──► delta report
   │ (GCMD,   │   │ .load()  │   │ concepts (cross- │   │ (new/aliases/orphans)│    (reports/*.md)
   │ SDE, WP) │   │ →Source  │   │ source dedup)    │   └──────────────────────┘
   └─────────┘   │ Concept  │   └────────┬─────────┘
                 └──────────┘            │
                                         ▼
                              ┌──────────────────────┐
                              │ consolidate()        │──► rebuilt .txt + consolidation report
                              │ fold→acronym→sort    │
                              └──────────┬───────────┘
                                         │
                 ┌───────────────── classify (OpenAI) ─────────────────┐
                 ▼                                                      │
   ┌──────────────────────┐   ┌──────────────────┐   ┌─────────────────────────────┐
   │ resolved-cache reuse │──►│ provenance (SDE  │──►│ OpenAI fallback (empty prov)│──► classified/*.json
   │ (prior classified)   │   │ collection keys) │   │ strict JSON-schema batches  │
   └──────────────────────┘   └──────────────────┘   └─────────────────────────────┘
```

Two CLI-driven phases:

1. **`pim-whitelist update`** (`cli.py:101`) — network fetch → dedup → delta → consolidate → write `.txt`.
2. **`pim-whitelist classify`** (`cli.py:243`) — tag each concept with SMD science division(s) → write `classified/*.json`.

There is also **`pim-whitelist consolidate`** (`cli.py:167`), a pure-local re-fold of an existing
`.txt` against itself (no network, `source_concepts=[]`).

---

## 1. Text-processing core (`normalize.py`)

Everything rests on a deliberate split between **two** operations. This is the single most important
idea in the whole system: *the stored string and the comparison key are different things.*

### `clean(text)` — the stored form (`normalize.py:42`)

Produces the string that actually gets written to a `.txt` alias:

- `html.unescape` (fix `&amp;` etc.),
- `unicodedata.normalize("NFKC", …)`,
- collapse whitespace runs to a single space, strip ends.
- **Case and punctuation are preserved on purpose** — the whitelists intentionally keep `"MISR"` and
  `"Multi-Angle Imaging SpectroRadiometer"` as separate aliases of one concept.

### `match_key(text)` — the comparison-only key (`normalize.py:57`)

Produces an aggressive key used *only* for equality/dedup, **never written to disk**:

1. `clean()` the text,
2. fold common Unicode punctuation to ASCII via `_UNICODE_FOLD` (`normalize.py:25`) — curly quotes →
   `'`, en/em dash/minus → `-`, non-breaking space → space,
3. `casefold()`,
4. NFKD-decompose and drop combining marks (strip accents),
5. delete **every** non-alphanumeric character (`_NON_ALNUM_RE`, `normalize.py:39`).

So `"Multi-Angle Imaging SpectroRadiometer"` and `"Multi angle imaging spectroradiometer"` both
collapse to `multiangleimagingspectroradiometer`. Returns `""` for input with no alphanumeric
content (blank/placeholder lines).

**The governing rule for the entire pipeline:** two strings are "the same concept" **iff their
match keys are equal**. Dedup, delta matching, cache lookup, and provenance join all pivot on this.

---

## 2. Data model

### `Concept` (`whitelist.py:17`)

An ordered list of `aliases`; `aliases[0]` is the **canonical** name. Aliases are stored *verbatim*
(no stripping) so a parse→serialize round-trip is byte-identical.

- `.canonical` — `aliases[0]`.
- `.text` — the serialized `;`-joined line.
- `.keys()` — the set of non-empty match keys across all aliases (`whitelist.py:36`).
- `.add_alias(a)` — append `a` iff its match key isn't already present (`whitelist.py:43`).

### `Whitelist` (`whitelist.py:55`)

Ordered `concepts` + the file's `trailing_newline` state. Files are `;`-delimited, one concept per
line, first alias canonical. `parse`/`serialize` guarantee round-trip fidelity (`whitelist.py:64,77`).
`build_index()` (`whitelist.py:91`) maps every alias match-key → owning `Concept` (first wins on
collision) — the O(1) lookup structure the delta and the serve API reuse.

### `SourceConcept` + `Provenance` (`sources/base.py`)

A concept as derived from an upstream source, with provenance:

- `SourceConcept` (`base.py:57`): `aliases` (already `clean`-ed, dedup'd by match key, canonical
  first) + `provenance`.
- `Provenance` (`base.py:15`): `origins` (e.g. `["gcmd"]`, `["sde"]`, `["missions"]`),
  `collection_keys` (SDE collection keys → divisions), `uuid` (GCMD), `extra` (per-source carry-along,
  e.g. a mission's id/slug/type/status/link — never merged across sources).
- `Provenance.merge(other)` (`base.py:40`) — **order-preserving union** of `origins` and
  `collection_keys`; `uuid` filled only if unset; `extra` untouched.
- `make_concept(values, provenance)` (`base.py:77`) — cleans each value, drops empties, drops later
  values whose match key duplicates an earlier one (**within-concept dedup**), returns `None` if
  nothing usable remains.

---

## 3. Retrieval — the sources

All sources subclass `Source` (`base.py:119`), which orchestrates a cache: `load(from_cache=…)`
either reads `data/raw/<cache_filename>` or fetches from network (with a shared retry session,
`base.py:102` — 5 retries, backoff, retry on 429/5xx, `GET`+`POST`) and writes the cache. Each source
declares `fetch_raw()` (bytes) and `parse()` (bytes → `list[SourceConcept]`).

### 3a. GCMD CSV sources (platforms + instruments)

Two thin subclasses over one shared parser:

- `PlatformsSource` (`sources/platforms.py`) — `cache_filename = "gcmd_platforms.csv"`, URL
  `config.PLATFORMS_URL` (CMR KMS platforms concept scheme, CSV).
- `InstrumentsSource` (`sources/instruments.py`) — `cache_filename = "gcmd_instruments.csv"`, URL
  `config.INSTRUMENTS_URL`.
- Shared: `CsvSource` + `parse_gcmd_csv` (`sources/csv_source.py:23`). The CSV has a metadata banner,
  then a header row containing `Short_Name`/`Long_Name`/`UUID`. Rows with an **empty `Short_Name`**
  are hierarchy nodes and are skipped. Each real row → `make_concept([short_name, long_name], …)`
  with `Provenance(origins=["gcmd"], uuid=<UUID>)`.

### 3b. SDE crawl source (platforms + instruments)

`sources/sde.py`. The NASA Science Discovery Engine search API is POST-crawled over
`config.SDE_API_SOURCES` (`config.py:58`) — six collection keys spanning all SMD divisions. Key
details:

- One crawl yields **both** `platform` and `instrument` fields, so the crawl is **memoized per
  process** (`_CRAWL`, `sde.py:31`; `crawl_cached`, `sde.py:90`) — `update --dataset all` hits the API
  once, and each of `SdePlatformsSource`/`SdeInstrumentsSource` slices its own field.
- `crawl()` (`sde.py:54`) paginates each collection key (`total_pages` from the response),
  emitting rows `{"value": str, "collection_key": str}`. `_clean_values` (`sde.py:34`) splits
  `;`-delimited values and drops junk sentinels (`""`, `"[]"`, `"not provided"`, `"not applicable"`).
- `parse()` (`sde.py:115`) → `make_concept([value], Provenance(origins=["sde"],
  collection_keys=[collection_key]))`. **This `collection_key` is the authoritative division signal
  consumed later by `classify`.**
- The cached JSON files (`sde_platforms.json`, `sde_instruments.json`) double as the **provenance
  source** for classification (`config.SDE_CACHE_FILENAMES`, `config.py:88`).

### 3c. Missions source

`sources/missions.py`. NASA WordPress REST API (`config.MISSIONS_URL`), paginated by the
`X-WP-TotalPages` header. Deduped by post `id`, sorted by id for a deterministic cache.

- Uses `orderby=id&order=asc` — **without this the CDN returns inconsistent/empty pages**
  (`missions.py:41`). (This is the documented Missions API pagination quirk.)
- Retries empty pages up to 3× (`_EMPTY_PAGE_RETRIES`), accepting genuinely-empty pages after that.
- Each post → `make_concept([title], Provenance(origins=["missions"], extra={id, slug, mission-type,
  mission-status, link}))`. Missions have **no aliases** (one title each) and **no SDE provenance**,
  so every mission falls to the OpenAI classifier.

---

## 4. Cross-source deduplication (`diff.merge_source_concepts`, `diff.py:46`)

When a dataset has multiple sources (GCMD CSV + SDE crawl), their concept lists are concatenated
(`cli.py:44`) and folded into **one deduplicated list** before anything else:

- Maintains a `match_key → SourceConcept` index.
- For each incoming concept, checks its alias keys **in order (canonical-first)** — deliberately
  deterministic, because `keys()` is an unordered set and Python's str hashing is randomized per run
  (`diff.py:63`).
- **Match** → union new aliases into the existing concept (skip keys already present) and
  `provenance.merge()`. This is why an instrument surfaced by *both* GCMD and two SDE collection keys
  ends up represented **once**, carrying `origins=["gcmd","sde"]` and both collection keys.
- **No match** → new concept (a deep copy; inputs are never mutated).
- A concept bridging two previously-separate groups folds into the first it matches — good enough for
  an append-only delta.

This must run **before** `compute_delta`, which matches only against the *existing whitelist* and
would otherwise append the same name once per source.

---

## 5. Delta vs. the existing whitelist (`diff.compute_delta`, `diff.py:89`)

The merged source list is diffed against the current `.txt` (via its `build_index()` match-key map)
into three non-destructive buckets (`Delta`, `diff.py:30`):

- **`new_concepts`** — no alias key exists anywhere in the whitelist.
- **`new_aliases`** (`NewAlias`, `diff.py:23`) — matches an existing concept but contributes ≥1 alias
  whose key is new to that concept.
- **`orphans`** — whitelist concepts none of whose keys appear in the source. **Kept, never removed**;
  surfaced only for human review (upstream churn / renamed entries).

The delta drives the "what's new" report (`reports/<dataset>-delta-<date>.md`). It is **not** what
gets written to the `.txt` — that is the consolidation output (§6). The delta and the written file
are computed independently from the same merged sources (`cli.py:47-48`).

---

## 6. Consolidation (`consolidate.py`)

`consolidate(whitelist, source_concepts, dataset)` (`consolidate.py:331`) rebuilds the **entire**
whitelist from scratch each run — treating the existing file as just another set of concepts — rather
than appending. The input whitelist is never mutated. Four ordered steps:

### Step 1 — Exact-key fold (`_initial_fold`, `consolidate.py:191`)

Existing concepts are **prepended** (so the curated canonical wins) and folded together with the new
source concepts by `match_key` equality, reusing `merge_source_concepts`. Collapses pre-existing
exact-key duplicates *and* attaches new source entries as aliases of the concept they match. Blank
lines (no keys) are skipped.

### Step 2 — Safe acronym merge (`_acronym_merge`, `consolidate.py:232`)

Folds an entry like `Science in Microgravity Box (SIMBOX)` into a separate **bare** entry `Simbox`
whose *canonical* normalizes to the parenthetical acronym. Deliberately conservative:

- The parenthetical acronym must normalize to **≥ `MIN_ACRONYM_LEN` (4)** chars
  (`consolidate.py:42`) — 3-letter acronyms (ACE, CCD, MRI) collide across unrelated concepts and are
  only ever *reported*, never merged.
- Merge target must be a concept whose **canonical itself is the bare acronym** (`_bare_index`,
  `consolidate.py:209`) — "the other entry **is** the acronym," not merely "contains it." So
  `WIND Spacecraft;WIND` is not a target for a stray `(WIND)` parenthetical.
- A concept that is *itself* a bare acronym (JADE, IKAR-P) is **never swallowed** just because its
  line mentions another acronym in parentheses (`_self_is_acronym`, `consolidate.py:226`) — guards the
  JADE/`(JEDI)` false-merge.
- Runs in a **fixed-point loop** (`consolidate.py:350`): merging adds aliases to survivors, which can
  create *new* exact-key collisions, so it re-folds and repeats until nothing changes. This makes a
  single `consolidate` run idempotent.

### Step 3 — Sort (`consolidate.py:359`)

Concepts are ordered case-insensitively by canonical match key (`_sort_key`, `consolidate.py:295`).
Alias order within each line is left untouched.

### Step 4 — Fuzzy near-duplicate detection — **review only** (`_find_fuzzy`, `consolidate.py:300`)

After sorting (near-dupes sit adjacent), scans a small window (`FUZZY_WINDOW = 3`) and flags pairs
with match-key similarity ≥ `FUZZY_RATIO` (0.90) as likely typos for a human to review. Critically,
it **suppresses "template siblings"** so the review list isn't flooded with legitimate series
members. A pair is a sibling (skipped) when `_is_template_sibling` (`consolidate.py:125`) holds:

- **Same content signature** — names differing only by an *enumeration token*: pure digits, single
  serial letters, or small roman numerals I–XV (`_content_signature`/`_is_enum_token`,
  `consolidate.py:80,90`). E.g. `BARREL 1A`/`BARREL 1B`, `ACRIM I`/`ACRIM II`, `Apollo 14`/`Apollo 15`.
- **One-token difference in a large enumerated family** — the differing slot has ≥ `FAMILY_MIN` (3)
  distinct values across the corpus under the same blanked template (`_build_family_index`,
  `consolidate.py:102`). E.g. the 54 `"CPMN fluxgate magnetometer at <code>"` stations.
- **Isolated one-token difference too dissimilar to be a typo** — the differing tokens score below
  `FUZZY_TOKEN_RATIO` (0.6), so they're distinct identifiers, not misspellings
  (`Narrow Angle`/`Wide Angle`, `Rumba`/`Samba`).

Everything the auto-rules refuse is **reported, not applied**.

### The consolidation report (`ConsolidationReport`, `consolidate.py:163`)

Carries `concepts_before/after`, `exact_merges` (count), `acronym_merges` (applied `MergePair`s),
`fuzzy_candidates` (review), and `ambiguous_acronyms` (`_find_ambiguous`, `consolidate.py:268` —
parentheticals that *look* mergeable but weren't: length-3 acronyms below the auto threshold, or the
JADE/JEDI self-acronym case). Rendered to `reports/<dataset>-consolidation-<date>.md`.

**Design invariant:** consolidation *auto-merges only on exact match-key equality*, plus one narrow,
heavily-guarded acronym exception. Everything genuinely ambiguous (short acronyms, near-duplicate
typos) is surfaced for a human, and orphans are never deleted. That conservatism is what makes a run
a safe, idempotent fixed point.

---

## 7. Classification (`classify.py`)

`classify_dataset(dataset, …)` (`classify.py:499`) tags each concept with zero or more NASA SMD
science **divisions** and writes one JSON file per dataset to `whitelist/classified/<dataset>.json`.
The taxonomy is fixed (`config.DIVISIONS`, `config.py:71`):

```python
DIVISIONS = ("earth", "heliophysics", "planetary", "astrophysics", "bps")
```

Divisions are assigned from **three tiers, cheapest first**, so each run only spends OpenAI calls on
what's genuinely new or unresolved.

### The output record — `ClassifiedRecord` (`classify.py:48`)

Pydantic model, the project's on-disk data contract; validated on load, `model_dump`-serialized
back, field order preserved so the JSON stays byte-stable:

```json
{ "match_key": "misr", "canonical": "MISR",
  "aliases": ["MISR", "Multi-Angle Imaging SpectroRadiometer"],
  "divisions": ["earth", "planetary"], "division_source": "provenance" }
```

`divisions` is a **list** (a concept can belong to several divisions); `division_source` is
`"provenance"` or `"openai"`.

### Tier 1 — Resolved-cache reuse (`_partition_by_cache`, `classify.py:457`)

The classified JSON *is a persistent cache*. `_resolved_cache` (`classify.py:404`) maps every alias
match-key of every **resolved** record (non-empty `divisions`, regardless of source) →
`(divisions, source)`. Any whitelist concept already resolved on a previous run is **reused verbatim
and never re-run**. Records with **empty** divisions are deliberately excluded from the cache, so
they get retried every run until the model can place them. `--reclassify` ignores the cache entirely
(clean slate / first run). Because the output is rebuilt from the *current* whitelist, concepts
removed from the `.txt` are pruned automatically.

### Tier 2 — Provenance (authoritative, free) (`_resolve_provenance`, `classify.py:478`)

For concepts the cache didn't cover, `load_provenance_divisions` (`classify.py:110`) reads the cached
SDE crawl (`sde_platforms.json` / `sde_instruments.json`) and maps each row's `collection_key` →
division via `config.COLLECTION_KEY_TO_DIVISION` (`config.py:76`), building
`match_key → set[division]`. A concept's divisions are the **union across all its match keys**
(`_provenance_for`, `classify.py:137`). This is the authoritative signal — a name the SDE surfaced
under a given collection *belongs* to that collection's division — and it needs **no network call**
(the crawl is on disk). Missions have no SDE crawl, so their provenance map is empty and they fall
entirely to Tier 3.

### Tier 3 — OpenAI fallback (`classify_concepts`, `classify.py:334`)

Only concepts with **empty provenance** (GCMD-only instruments/platforms, and every mission) are sent
to an OpenAI chat model:

- **Batching + concurrency:** concepts are chunked into batches of `OPENAI_BATCH_SIZE` (50,
  `config.py:122`) and dispatched across a `ThreadPoolExecutor` (default 4 workers). Progress is
  reported per batch.
- **Prompt:** a system prompt (`classify.py:157`) grounds the model as an SMD data curator and injects
  the five division keys **with descriptions** (`config.DIVISION_DESCRIPTIONS`, `config.py:95`). Rules:
  a name may belong to more than one division; return an **empty list** for opaque identifiers rather
  than guessing; use only the exact keys; return a result for every input index. Each concept is
  presented with a stable `index` (`build_messages`, `classify.py:171`) so the unordered response array
  can be mapped back.
- **Strict structured output:** the request uses `response_format: json_schema` with `strict: true`
  and `_RESPONSE_SCHEMA` (`classify.py:68`), constraining every returned division to the five-value
  `enum`. `temperature=0` is requested for determinism.
- **Robust request handling** (`_request_with_retries`, `classify.py:245`):
  - Transient failures (429, 5xx, connection/timeout — `_is_retryable`, `classify.py:198`) retried with
    exponential backoff (`_MAX_RETRIES=5`, `_BACKOFF_BASE=2s`, doubling).
  - Permanent 4xx (400/401/404) fail fast — no wasted backoff.
  - Reasoning models (gpt-5 / o-series) that reject `temperature=0` are detected
    (`_rejects_temperature`, `classify.py:212`) and the request is re-sent **immediately** without
    temperature, not counted against the backoff budget.
- **Response mapping** (`_parse_batch_response`, `classify.py:303`): out-of-range/non-integer indices
  are skipped; any index the model omits defaults to `[]`. `_valid_divisions` (`classify.py:192`)
  keeps only recognized divisions, in canonical taxonomy order, deduplicated.

### Assembly & write (`classify.py:571`)

`records = reused + provenance + openai`, sorted by `match_key`, then `_write` (`classify.py:447`)
dumps indented UTF-8 JSON to `whitelist/classified/<dataset>.json`. Stats returned for the CLI:
`records, reused, provenance, classified, to_classify, empty`.

- `--limit N` caps how many concepts go **to OpenAI** (cheap test runs).
- `--dry-run` reports counts without calling OpenAI or writing (no API key needed); OpenAI records are
  placeholders with empty divisions.
- Model resolution order (`resolve_model`, `classify.py:96`): `--model` flag → `$OPENAI_MODEL` →
  `config.DEFAULT_OPENAI_MODEL` (`gpt-4o-mini`). `OPENAI_API_KEY` is read from the environment or the
  repo-root `.env` (loaded in `cli.py:79`).

### Edge case — a concept with no divisions

A concept the classifier returns empty for (opaque identifier) is written with `divisions: []`. It is
**not** cached as resolved, so it's retried on the next run. Downstream (the serve API) such a concept
resolves and searches normally but is excluded by any `&division=` filter. This is the honest
behavior and is surfaced via the `empty` count.

---

## 8. CLI orchestration (`cli.py`)

### `update` (`cli.py:101`)

Per dataset (`process_dataset`, `cli.py:33`):
1. `Whitelist.load` the current `.txt`.
2. `get_sources(dataset)` → each `source.load(from_cache=…)`; concatenate raw concepts (`cli.py:44`).
3. `merge_source_concepts` (§4) → deduped source list.
4. `compute_delta` (§5) → delta report.
5. `consolidate` (§6) → rebuilt whitelist + consolidation report.

Then: write the combined delta report and per-dataset consolidation reports; if not `--dry-run`, write
each changed `.txt` (`_changed` compares serialized output to disk, `cli.py:54`). Fails fast on the
first dataset error rather than writing a partial result. `--from-cache` reuses `data/raw/`.
`--open-pr` (`_open_pr`, `cli.py:318`) branches, commits the changed `.txt` + report, pushes, and
opens a PR via `gh`.

### `consolidate` (`cli.py:167`)

Pure-local re-fold of each `.txt` against itself (`consolidate(whitelist, [], …)`) — no network.
Writes a consolidation report and, unless `--dry-run`, the re-folded file.

### `classify` (`cli.py:243`)

Per dataset → `classify_dataset` (§7). `--reclassify`, `--limit`, `--batch-size`, `--model`,
`--dry-run`, `-v/--verbose` (per-request DEBUG logging so a stalled request is visible). Writes
`whitelist/classified/<dataset>.json`.

### The sync chain (typical run)

```
pim-whitelist update       # fetch → dedup → delta → consolidate → write .txt (+ reports)
pim-whitelist classify     # tag concepts with divisions → write classified/*.json
pim-whitelist consolidate  # optional final local re-fold / sort
  └─ update --open-pr      # branch, commit .txt + classified/*.json + reports, gh pr create
```

A human reviews the auto-opened PR (delta + consolidation + classification are the review surface),
and merging redeploys the serve API (see `docs/read-api-architecture.md`).

---

## 9. Determinism & invariants (why this is safe to re-run)

- **Match-key equality is the only auto-merge trigger** — plus one narrow, guarded acronym exception.
  Nothing fuzzy ever silently merges.
- **Deterministic matching** — alias iteration is canonical-first everywhere (`diff.py:63,99`;
  `classify.py:438`) because `keys()` is an unordered set and str hashing is randomized per process.
- **Idempotent consolidation** — the acronym/re-fold loop runs to a fixed point (`consolidate.py:350`).
- **Non-destructive** — orphans are kept and reported, never deleted; the classified JSON prunes only
  what's already gone from the `.txt`.
- **Byte-stable outputs** — `Whitelist` round-trips verbatim; `ClassifiedRecord` preserves field order
  and sorts by `match_key`, so unchanged data produces an unchanged file (clean diffs).
- **Cheapest-first classification** — resolved-cache reuse → free provenance → OpenAI only for the
  remainder; empty results are retried, resolved ones are frozen.

---

## 10. File & module map

| Concern | Module |
|---|---|
| Text processing (`clean`, `match_key`) | `src/pim_whitelist/normalize.py` |
| Data model (`Concept`, `Whitelist`) | `src/pim_whitelist/whitelist.py` |
| Source base, `SourceConcept`, `Provenance` | `src/pim_whitelist/sources/base.py` |
| GCMD CSV parsing | `src/pim_whitelist/sources/csv_source.py` + `platforms.py` / `instruments.py` |
| SDE crawl | `src/pim_whitelist/sources/sde.py` |
| Missions (WordPress API) | `src/pim_whitelist/sources/missions.py` |
| Source→dataset wiring | `src/pim_whitelist/sources/__init__.py` |
| Cross-source dedup + delta | `src/pim_whitelist/diff.py` |
| Consolidation (fold/acronym/sort/fuzzy) | `src/pim_whitelist/consolidate.py` |
| Division classification | `src/pim_whitelist/classify.py` |
| Config (datasets, URLs, taxonomy, prompts) | `src/pim_whitelist/config.py` |
| Reports rendering | `src/pim_whitelist/report.py` |
| CLI | `src/pim_whitelist/cli.py` |

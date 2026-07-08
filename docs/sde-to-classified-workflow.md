# From SDE response to classified whitelist

How platform / instrument / mission names flow from upstream NASA sources into the
`;`-delimited whitelist files and finally into the per-division classified JSON under
`whitelist/classified/`. The focus is the two questions this doc was written to answer:

1. **How are `platform` and `instrument` identified from an SDE response?**
2. **How are multi-valued (semicolon-delimited) records treated — one entry or many?**

Short answer to (2): **many.** A `;`-delimited field is split and each value becomes
its own independent entry; identical values across records are later de-duplicated by
a normalized match key, but the original co-listing is never reconstructed.

## Pipeline at a glance

```
upstream sources ──► SourceConcepts ──► merge/dedup ──► whitelist .txt ──► classify ──► classified/*.json
   (per dataset)        (base.py)         (diff.py)    (consolidate.py)   (classify.py)
```

Three datasets, each with its own upstream sources (`src/pim_whitelist/config.py:28-52`):

| Dataset      | Whitelist file                       | Sources |
|--------------|--------------------------------------|---------|
| platforms    | `SMD_Platforms_Consolidated.txt`     | CMR KMS CSV + **SDE crawl** (`platform` field) |
| instruments  | `SMD_Instruments_Consolidated.txt`   | CMR KMS CSV + **SDE crawl** (`instrument` field) |
| missions     | `SMD_Missions_Consolidated.txt`      | nasa.gov WP API (no SDE) |

Three CLI commands drive it (`src/pim_whitelist/cli.py`):

- `update`   — fetch sources, compute a delta, rebuild + write the `.txt` files.
- `consolidate` — local-only re-fold/merge/sort of a `.txt` file.
- `classify` — tag every concept in a `.txt` with its science division(s) → JSON.

## Stage 1 — Identifying `platform` / `instrument` from the SDE

`src/pim_whitelist/sources/sde.py`. The SDE search API returns one flat document per
dataset; `platform` and `instrument` are **top-level string fields** on that document
(never inferred from text, never nested). The crawler asks for exactly those two
fields (`sde.py:24`):

```python
FIELDS = ("platform", "instrument")
```

It pages through every collection key in `SDE_API_SOURCES` and, for each document,
pulls each field through `_clean_values` (`sde.py:80-86`):

```python
for doc in data.get("documents", []):
    for field in FIELDS:
        for value in _clean_values(doc.get(field)):
            out[field].append({"value": value, "collection_key": collection_key})
```

Note `collection_key` is recorded next to every value — that is the provenance that
later assigns a division for free (Stage 5).

### How multi-valued records are split — the core of question (2)

`_clean_values` is where one document's field becomes *N* values (`sde.py:34-51`):

```python
def _clean_values(raw_value) -> list[str]:
    if raw_value is None:
        return []
    pieces: list[str] = []
    items = raw_value if isinstance(raw_value, list) else [raw_value]
    for item in items:
        if not isinstance(item, str):
            continue
        for part in item.split(";"):          # <-- split the ";"-delimited string
            part = part.strip()
            if part and part.casefold() not in _JUNK:   # drop sentinels per-part
                pieces.append(part)            # <-- each part appended separately
    return pieces
```

So a single document whose `instrument` field is:

```
"Bruker IFS 125HR…;Terahertz Far-Infrared Beamline…;Far-Infrared Beamline…"
```

becomes **three** separate `{"value", "collection_key"}` rows, and then three
independent `SourceConcept`s in `parse` (`sde.py:115-125`) — one alias each. The
semicolon grouping is discarded; the SDE does **not** treat a co-listed
instrument+platform pair as linked.

`_JUNK` filters the SDE's "no value" sentinels case-insensitively, **after** the
split, so `"Not provided;FIELD SURVEYS;FIELD INVESTIGATION"` correctly drops
`"Not provided"` and keeps the two real platforms (`sde.py:28`):

```python
_JUNK = {"", "[]", "not provided", "not applicable"}
```

> Because `platform`/`instrument` come back as strings (or `null`), the
> `isinstance(raw_value, list)` branch only ever matters post-split. The standalone
> exploratory script `sdeAPI_pimsList (1).py` does the same split but has an
> always-true `or` guard that lets `"NOT APPLICABLE"` leak through after splitting —
> the production `_clean_values` above is the corrected version.

The crawl is memoized for the process (`crawl_cached`, `sde.py:90-94`) so a single
`update --dataset all` run hits the SDE once and both the platforms and instruments
sources slice their own field out of the shared result.

## Stage 2 — Normalization: stored form vs match key

`src/pim_whitelist/normalize.py`. Two deliberately separate operations:

- `clean(text)` — the **stored** form. Unescapes HTML entities, NFKC-normalizes,
  collapses whitespace. **Preserves case and punctuation** (the whitelist keeps those
  as distinct aliases).
- `match_key(text)` — a **comparison-only** key. Folds Unicode punctuation to ASCII,
  casefolds, strips accents, and removes every non-alphanumeric char. Never written to
  disk. This is what makes dedup case/spacing/punctuation-insensitive:

```python
def match_key(text: str) -> str:
    cleaned = clean(text).translate(_UNICODE_FOLD)
    folded = cleaned.casefold()
    folded = unicodedata.normalize("NFKD", folded)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _NON_ALNUM_RE.sub("", folded)
```

**Examples:**

| Input | `clean` (stored) | `match_key` (compare) |
|-------|------------------|------------------------|
| `"Multi-Angle Imaging SpectroRadiometer"` | `Multi-Angle Imaging SpectroRadiometer` | `multiangleimagingspectroradiometer` |
| `"Multi angle imaging spectroradiometer"` | `Multi angle imaging spectroradiometer` | `multiangleimagingspectroradiometer` |
| `"AVHRR&#47;3"` (HTML entity) | `AVHRR/3` | `avhrr3` |
| `"ACRIM&nbsp;II"` (non-breaking space) | `ACRIM II` | `acrimii` |

Note the first two rows: different stored strings, **same** match key — so they are
treated as the same concept downstream while both spellings can still be kept as
aliases. The `clean` form never changes case or drops punctuation; only `match_key`
does, and `match_key` is never written to a file.

## Stage 3 — SourceConcept and per-concept dedup

`src/pim_whitelist/sources/base.py`. Every source emits `SourceConcept`s — an ordered
alias list (canonical first) plus `Provenance` (`base.py:57-74`). `make_concept`
cleans inputs and drops aliases whose match key duplicates an earlier one
(`base.py:77-99`). `Provenance` carries `origins` (e.g. `"sde"`) and `collection_keys`
(e.g. `"NAVO_HEASARC"`); `merge`/`copy` keep that contract (`base.py:40-54`).

**Example A — distinct aliases are kept, canonical first.** The mission source feeds
three names for one mission whose match keys all differ:

```python
make_concept(["1M/1", "Mars 1960A", "Mars 1M No.1"])
# match keys: "1m1", "mars1960a", "mars1mno1"  — all distinct
# → SourceConcept(aliases=["1M/1", "Mars 1960A", "Mars 1M No.1"], ...)
# canonical == "1M/1"; all three kept
```

**Example B — duplicate aliases collapse by match key.** Spellings that share a match
key are folded to the first one seen, so only one alias survives:

```python
make_concept(["EPAM", "epam", "E.P.A.M."])
# all three → match_key "epam"
# → SourceConcept(aliases=["EPAM"], ...)   # later duplicates dropped
```

**Example C — provenance attached at creation.** A value sliced out of the SDE crawl
(Stage 1) becomes a single-alias concept tagged with where it came from
(`sde.py:118-124`):

```python
make_concept(["EPAM"], Provenance(origins=["sde"], collection_keys=["SPASE_JSON"]))
```

That `collection_keys=["SPASE_JSON"]` is what lets Stage 6 assign `heliophysics`
without an API call.

## Stage 4 — Cross-source merge / dedup

`src/pim_whitelist/diff.py`, `merge_source_concepts` (`diff.py:46-86`). This is where
the *N* split values get de-duplicated. Concepts that share **any** match key are
folded into one: aliases are unioned (first-seen order), provenance `origins` /
`collection_keys` merged.

```python
for alias in sc.aliases:
    key = match_key(alias)
    if key and key in index:
        target = index[key]      # fold into the concept already holding this key
        break
```

Net effect on multi-valued records:

- **1 record with N `;`-values → up to N entries.**
- **Same value seen across many records → 1 entry** (deduped, provenance unioned).
- The dedup is by normalized key only; it **never** rebuilds the original
  semicolon group. After this stage the platform↔instrument linkage from a single
  dataset is gone.

**Example A — many mentions collapse to one.** `EPAM` appears on dozens of
`SPASE_JSON` documents, so Stage 1 emits dozens of identical `SourceConcept`s. All of
them share match key `"epam"` and fold into a single concept:

```python
[SourceConcept(["EPAM"], Prov(origins=["sde"], collection_keys=["SPASE_JSON"])),
 SourceConcept(["EPAM"], Prov(origins=["sde"], collection_keys=["SPASE_JSON"])),
 ... ×50 ]
# merge_source_concepts → [ SourceConcept(["EPAM"],
#                              Prov(origins=["sde"], collection_keys=["SPASE_JSON"])) ]
```

**Example B — same name from two sources → unioned provenance.** `MODIS` arrives both
from the CMR KMS instruments CSV and from the SDE `CMR_API` crawl. They share one key
and merge — `origins` and `collection_keys` are unioned:

```python
[SourceConcept(["MODIS"], Prov(origins=["csv"])),
 SourceConcept(["MODIS"], Prov(origins=["sde"], collection_keys=["CMR_API"]))]
# → SourceConcept(["MODIS"], Prov(origins=["csv", "sde"], collection_keys=["CMR_API"]))
```

Here both signals point at `earth`. The same union mechanism is what produces
**multi-division** concepts when the collection keys disagree — e.g. a name surfaced
under both `CMR_API` (earth) and `NAVO_HEASARC` (astrophysics) ends up with
`collection_keys=["CMR_API", "NAVO_HEASARC"]` and later resolves to
`["earth", "astrophysics"]` (Stage 6).

`compute_delta` (`diff.py:89-125`) then classifies each merged concept against the
*existing* whitelist as `new_concepts`, `new_aliases`, or `orphans` (for the
"what's new" report only).

## Stage 5 — Whitelist file + consolidation

`src/pim_whitelist/whitelist.py`: each `.txt` line is one concept; aliases joined by
`;`, canonical first; round-trip-faithful (`whitelist.py:63-89`).

**Example — file format.** One concept per line, `;`-joined, canonical first:

```
1M/1;Mars 1960A;Mars 1M No.1
Multi-Angle Imaging SpectroRadiometer;Multi angle imaging spectroradiometer;MISR
```

`src/pim_whitelist/consolidate.py` rebuilds the whole file each run
(`consolidate.py:331-369`): exact-key fold (reusing `merge_source_concepts`), a
conservative parenthetical-acronym merge (`MIN_ACRONYM_LEN = 4`, so ACE/CCD/MRI never
auto-merge), then a case-insensitive sort. It also surfaces — without applying —
fuzzy near-duplicates and ambiguous acronyms for human review, while skipping
deliberate "template siblings" like `BARREL 1A`/`BARREL 1B`.

**Example A — exact-key fold.** A new source spelling that normalizes to an existing
line is absorbed as an alias rather than added as a new concept:

```
existing line:  MODIS
new source:     Modis            (match_key "modis" == existing)
→ folded:       MODIS            (no new line; the existing canonical wins)
```

**Example B — safe acronym merge.** A descriptive entry whose parenthetical acronym
(≥4 chars) equals a separate *bare* entry is folded together (`consolidate.py:232-265`):

```
Science in Microgravity Box (SIMBOX)
Simbox
→  Science in Microgravity Box (SIMBOX);Simbox
```

**Example C — what is *not* merged (reported only).** Generic 3-letter acronyms and
deliberate siblings are left alone:

```
ACE                      ┐ 3-letter acronym → never auto-merged
... (ACE) ...            ┘ (only reported as "ambiguous")
BARREL 1A / BARREL 1B    → template siblings: distinct entities, kept separate
```

## Stage 6 — Classification into science divisions

`src/pim_whitelist/classify.py`. Each concept is tagged with zero or more of the five
NASA SMD divisions (`config.py:71`):

```python
DIVISIONS = ("earth", "heliophysics", "planetary", "astrophysics", "bps")
```

Two signals, **provenance first** (`classify.py` module docstring, `classify_dataset`
`:499-584`):

1. **Provenance (authoritative).** Each SDE `collection_key` maps 1:1 to a division
   (`config.py:76-83`):

   ```python
   COLLECTION_KEY_TO_DIVISION = {
       "CMR_API": "earth",
       "SPASE_JSON": "heliophysics",
       "PDS_API_Legacy_All": "planetary",
       "PDS4_API": "planetary",
       "GENELAB_METADATA_OSDR": "bps",
       "NAVO_HEASARC": "astrophysics",
   }
   ```

   `load_provenance_divisions` reads the cached SDE crawl and builds
   `match_key → {divisions}` (`classify.py:110-134`). A concept covered by provenance
   gets `division_source: "provenance"` — no API call. This is exactly why splitting
   in Stage 1 kept `collection_key` on every value.

2. **OpenAI (fallback).** Only concepts with **empty** provenance (GCMD-only
   instruments/platforms, and *all* missions, which the SDE never crawls) are batched
   to an OpenAI chat model under a strict JSON schema constrained to the five division
   enum values (`classify.py:68-89`, `_build_request` `:224-242`). These get
   `division_source: "openai"`; opaque identifiers the model can't place come back
   with an empty `divisions` list.

The classified JSON is itself a **persistent cache**: already-resolved records
(non-empty divisions) are reused verbatim and never re-run; only new or
empty-result concepts are resolved. `--reclassify` ignores the cache
(`classify_dataset` `:526-575`).

### Output contract

One JSON file per dataset under `whitelist/classified/`, one record per concept
(`ClassifiedRecord`, `classify.py:48-60`), sorted by `match_key`:

```json
{
  "match_key": "epam",
  "canonical": "EPAM",
  "aliases": ["EPAM"],
  "divisions": ["heliophysics"],
  "division_source": "provenance"
}
```

A concept may carry **more than one** division — both because the OpenAI prompt
explicitly allows it and because a name surfaced under multiple collection keys
unions their divisions (`_provenance_for`, `classify.py:137-142`).

**Example A — resolved by provenance (no API call).** `EPAM` was crawled under
`SPASE_JSON`, which maps to `heliophysics`:

```json
{"match_key": "epam", "canonical": "EPAM", "aliases": ["EPAM"],
 "divisions": ["heliophysics"], "division_source": "provenance"}
```

**Example B — resolved by OpenAI (no SDE provenance).** Missions are never crawled
from the SDE, so every mission falls to the model:

```json
{"match_key": "2001marsodyssey", "canonical": "2001 Mars Odyssey",
 "aliases": ["2001 Mars Odyssey"], "divisions": ["planetary"],
 "division_source": "openai"}
```

**Example C — multiple divisions.** A name unioned across `CMR_API` (earth) and
`NAVO_HEASARC` (astrophysics) in Stage 4 carries both, in canonical taxonomy order:

```json
{"divisions": ["earth", "astrophysics"], "division_source": "provenance"}
```

**Example D — unresolvable.** An opaque identifier with no SDE provenance that the
model can't confidently place comes back empty, and is retried on the next run (empty
records are not cached as "resolved"). Illustrative:

```json
{"match_key": "tbd0042x", "canonical": "TBD-0042X", "aliases": ["TBD-0042X"],
 "divisions": [], "division_source": "openai"}
```

## Worked example — semicolon splitting, end to end

A live `SPASE_JSON` document:

```json
{
  "title": "ACE - EPAM - CA60",
  "platform": "ACE;Advanced Composition Explorer, NASA;1997-045A;Explorer 71",
  "instrument": "EPAM"
}
```

1. **Stage 1** splits `platform` into 4 rows, each `collection_key="SPASE_JSON"`;
   `instrument` yields 1 row (`EPAM`).
2. **Stage 4** dedups these against all other docs by `match_key` (e.g. every
   `EPAM` mention collapses to one concept).
3. **Stage 6** maps `SPASE_JSON → heliophysics`, so all five names resolve to
   `["heliophysics"]` with `division_source: "provenance"` — no OpenAI call.

Reproduce the raw response with:

```bash
curl -s -X POST "https://science.data.nasa.gov/science-discovery-engine/api/search" \
  -H "Content-Type: application/json" \
  -d '{"search_term":"","page":1,"page_size":100,"search_type":"keyword","filters":{"collection_key":["SPASE_JSON"]}}' \
| jq '[.documents[] | select((.platform//""|contains(";")) or (.instrument//""|contains(";")))][0] | {title, platform, instrument}'
```

## Key files

| File | Role |
|------|------|
| `src/pim_whitelist/sources/sde.py` | SDE crawl; `platform`/`instrument` extraction; **`;` split** |
| `src/pim_whitelist/normalize.py` | `clean` (stored form) vs `match_key` (dedup key) |
| `src/pim_whitelist/sources/base.py` | `SourceConcept`, `Provenance`, `make_concept` |
| `src/pim_whitelist/diff.py` | `merge_source_concepts` (cross-source dedup), `compute_delta` |
| `src/pim_whitelist/whitelist.py` | `.txt` parse/serialize model |
| `src/pim_whitelist/consolidate.py` | fold / acronym-merge / sort |
| `src/pim_whitelist/classify.py` | provenance-first + OpenAI division tagging → JSON |
| `src/pim_whitelist/config.py` | dataset registry, `COLLECTION_KEY_TO_DIVISION`, `DIVISIONS` |
| `src/pim_whitelist/cli.py` | `update` / `consolidate` / `classify` commands |
| `sdeAPI_pimsList (1).py` | standalone exploratory crawler (superseded by `sde.py`) |

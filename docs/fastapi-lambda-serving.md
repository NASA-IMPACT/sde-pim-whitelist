# Serving the PIM Whitelists as a FastAPI Service on AWS Lambda

## Context

`sde-pim-whitelist` is today a **CLI-only reconciliation tool**. It maintains three hand-curated
alias files — Platforms (~4,412 concepts), Instruments (~7,676), Missions (~1,952) — each a
`;`-delimited line per concept with the canonical name first, plus structured provenance, and an
append-only sync pipeline against upstream NASA APIs (GCMD CSV, SDE search, NASA.gov WordPress REST).

The curated whitelists exist to **canonicalize free-text platform / instrument / mission keyword
strings** found in NASA dataset metadata. That is precisely the job a service should expose: *given a
messy string, return the canonical concept and its aliases.* The matching primitive already exists —
`match_key()` (casefold + strip non-alphanumerics) and `Whitelist.build_index()` (match-key → Concept).

This document is a design report. It covers:
- The read model and why serving is cheap (no network, ~570 KB of data, pure-Python deps).
- The **FastAPI application architecture** at a glance (§1.5).
- A full endpoint catalog tuned for **both** batch resolution *and* interactive search/autocomplete.
- AWS Lambda architecture, with a packaging recommendation.
- What each of the **three API-scope scenarios** (read-only, read + admin sync, full read/write) requires.
- Auth via **API Gateway + API key / usage plan**, plus performance, observability, and an implementation plan.
- **Scheduling** the out-of-band sync (§12), **CI/CD** for build + deploy (§13), and
  **division-based separation** across the SMD science divisions (§14).

Decisions captured: consumers are batch pipelines **and** interactive UI (co-equal);
auth is API Gateway + API key; packaging recommended here; all three scope scenarios covered.

> **Open decisions for the reviewer.** Scheduling (§12), CI/CD (§13), and division separation
> (§14) each present their choices as labeled options with a `> DECISION:` line. They are
> deliberately *not* down-selected — check a box per fork during review and the implementation
> follows from your picks.

---

## 1. What we are serving (the read model)

At serve time the API is **stateless and read-only over three text files**. It does *not* need
`requests`, the `sources/` package, `diff.py`, `merge.py`, or `report.py` — those are sync-time only.

Reused as-is from the existing code:
- `pim_whitelist.whitelist.Whitelist.load(path)` / `.parse(text)` — `src/pim_whitelist/whitelist.py:63`
- `Whitelist.build_index() -> dict[str, Concept]` — `src/pim_whitelist/whitelist.py:91`
- `Concept.canonical`, `Concept.aliases`, `Concept.keys()` — `src/pim_whitelist/whitelist.py:27`
- `pim_whitelist.normalize.match_key(text)` / `clean(text)` — `src/pim_whitelist/normalize.py:42`
- `pim_whitelist.config.DATASETS` registry + `DatasetConfig.whitelist_path` — `src/pim_whitelist/config.py:40`

**Important gap:** the on-disk whitelist files carry **no provenance** — provenance lives only on
`SourceConcept` during a sync and is never persisted (the files are plain `;`-delimited text). So a
read-only API can return canonical + aliases + match keys, but **cannot** return origins / UUID /
mission id unless we either (a) re-derive provenance by running sources at sync time and persist a
sidecar, or (b) accept that provenance is sync-time-only. This is called out per-scenario below.
**Division metadata is the concrete instance of this gap** — division = the SDE `collection_keys`
that live only on sync-time provenance — so §14 (division-based separation) depends directly on
resolving it.

### In-memory index built once per cold start
For each dataset we build, at module import:
- `concepts: list[Concept]` (preserves file order).
- `index: dict[str, Concept]` via `build_index()` — O(1) exact/normalized lookup.
- An **alias-level** map `match_key(alias) -> (Concept, alias)` for resolution that reports *which*
  alias matched.
- A list of `(match_key, canonical, concept)` tuples to back substring/prefix search and autocomplete.

Total data is tiny (~570 KB of text → a few MB of Python objects across all three datasets), so
everything lives in memory. Build cost is dominated by `match_key` over ~14k aliases — low tens of ms.

---

## 1.5 FastAPI application architecture at a glance (answering "what is the architecture?")

The architecture is **stateless, read-only, and single-process**: a FastAPI app whose only state
is an in-memory index built once at import and reused across warm Lambda invocations. The pieces
below are detailed in their own sections; this is the one-screen synthesis.

```
                       request (x-api-key)
                              │
   ┌──────────────────────────▼──────────────────────────┐
   │ API Gateway (REST) — API keys + usage plan (§6)      │
   └──────────────────────────┬──────────────────────────┘
                              │ proxied event
   ┌──────────────────────────▼──────────────────────────┐
   │ Lambda — handler = Mangum(app)  (§4)                 │
   │  FastAPI app (app.py)                                │
   │   ├─ routes/  resolve · search · browse  (§2)        │
   │   ├─ models.py  Pydantic request/response (§8)       │
   │   └─ index.py  WhitelistIndex ──────────────┐        │
   └──────────────────────────────────────────────┼──────┘
                                                   │ built once at
                                                   │ module import
   ┌───────────────────────────────────────────────▼──────┐
   │ in-memory index over 3 whitelist .txt files           │
   │  reuses Whitelist.load / build_index / match_key      │
   │  (whitelist.py, normalize.py) — no network, no DB      │
   └───────────────────────────────────────────────────────┘
```

**Layering** (request → response): `routes/` (thin FastAPI glue, validation) → `WhitelistIndex`
(`index.py`, the only substantial new logic — resolve/search over `Concept`/`match_key`, optional
`rapidfuzz`) → reused core (`whitelist.py`, `normalize.py`). The handler (`handler.py`) is just
`Mangum(app)`; the same `app` object runs locally under `uvicorn`.

Where each architectural concern is specified:

| Concern | Section |
|---|---|
| Read model / what is served | §1 |
| Endpoints (the public contract) | §2 |
| Fuzzy matching strategy | §3 |
| Lambda runtime, cold start, concurrency | §4 |
| Packaging (container vs zip) | §5 |
| Auth | §6 |
| Read/admin/write scope scenarios | §7 |
| Response models | §8 |
| Code layout / IaC sketch | §9 |
| Observability | §10 |
| **Scheduling the sync** | **§12** |
| **CI/CD** | **§13** |
| **Division-based separation** | **§14** |

---

## 2. Endpoint catalog

Base path `/v1`. `{dataset}` ∈ `platforms | instruments | missions`. All responses JSON.
Designed so **batch resolution** and **interactive search** are both first-class.

> **Division note.** This catalog is division-agnostic. If §14 Option A (facet at root) is
> chosen, every resolve/search/browse endpoint gains an optional `&division=<name>` filter and
> concepts carry a `divisions` field — no new routes. Options B/C in §14 change the path/dataset
> shape instead. See §14 for the full design.

### Resolution (the core use case)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/{dataset}/resolve?q=<text>&fuzzy=false` | Resolve one string to a canonical concept. |
| `POST` | `/v1/{dataset}/resolve:batch` | Resolve up to N strings in one request (batch pipelines). |
| `POST` | `/v1/resolve:batch` | Mixed-dataset batch: each item names its own dataset. |

`GET .../resolve` response:
```json
{
  "query": "multi-angle imaging spectroradiometer",
  "matched": true,
  "match_type": "alias",            // "canonical" | "alias" | "fuzzy" | "none"
  "matched_alias": "Multi-Angle Imaging SpectroRadiometer",
  "concept": {
    "canonical": "MISR",
    "aliases": ["MISR", "Multi-Angle Imaging SpectroRadiometer", "Multi-angle Imaging Spectro-Radiometer"],
    "match_key": "misr"
  },
  "score": 1.0                       // 1.0 for exact/normalized; <1.0 for fuzzy
}
```
- Matching strategy: compute `match_key(q)`; hit the alias map for an exact normalized match
  (`match_type` = `canonical` if it equals the canonical's key, else `alias`). If miss and
  `fuzzy=true`, fall back to a ranked candidate list (see §3).
- `matched: false` with `match_type: "none"` when nothing clears the threshold — never 404 for a
  well-formed query; reserve 404 for unknown `{dataset}`.

`POST .../resolve:batch` request/response:
```json
// request
{ "queries": ["MODIS", "modis aqua", "not a real instrument"], "fuzzy": true, "limit_per_query": 1 }
// response
{ "results": [ {<resolve object>}, {<resolve object>}, {<resolve object>} ] }
```
Batch caps (e.g. ≤ 1,000 queries/request) protect the 6 MB API Gateway payload limit and Lambda CPU.

### Search & autocomplete (interactive UI)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/{dataset}/search?q=&mode=substring&limit=&offset=` | Ranked concept search (substring/prefix/fuzzy). |
| `GET` | `/v1/{dataset}/suggest?q=&limit=10` | Lightweight typeahead — canonical + best alias only. |

`search` returns ranked concepts with pagination metadata; `mode` ∈ `prefix | substring | fuzzy`.
`suggest` is the latency-optimized typeahead variant (prefix-first, small payload, aggressively cacheable).

### Browse & detail

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/datasets` | List datasets + counts + data version/etag. |
| `GET` | `/v1/{dataset}/concepts?limit=&offset=` | Paginated full listing (stable file order). |
| `GET` | `/v1/{dataset}/concepts/{key}` | Concept detail by canonical match-key (stable id). |

`{key}` is the canonical's `match_key` — a stable, URL-safe identifier (e.g. `misr`). Detail page
backs UI "concept" views.

### Validation & meta

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/{dataset}/validate` | Bulk "is each of these a known term?" → booleans + canonical. |
| `GET` | `/v1/stats` | Per-dataset counts, alias totals, data version, build time. |
| `GET` | `/health` | Liveness — index built, file checksums present. (No auth.) |
| `GET` | `/v1/openapi.json`, `/docs` | FastAPI auto-generated schema + Swagger UI. |

`validate` is `resolve:batch` minus the concept payload — cheap gate for ingest pipelines.

### Cross-cutting conventions
- **Pagination:** `limit` (default 50, max 500) + `offset`; responses include `total`.
- **ETag / caching:** every response carries an `ETag` derived from a content hash of the loaded
  files (the "data version"). Clients send `If-None-Match` → `304`. Set `Cache-Control: public,
  max-age=...` on read endpoints so CloudFront/API Gateway caching absorbs hot queries.
- **Errors:** RFC-9457 problem+json shape; 400 (bad query/oversized batch), 404 (unknown dataset/key),
  429 (usage-plan throttle, emitted by API Gateway), 5xx.
- **Versioning:** path prefix `/v1`; the data version (file hash) is separate and surfaced in headers
  + `/v1/stats` so consumers can detect whitelist refreshes.

---

## 3. Fuzzy matching note

Exact normalized matching via `match_key` covers the dominant case for free. For `fuzzy=true` /
`mode=fuzzy` we need ranked near-matches. Options, cheapest first:
- **Prefix/substring over match keys** — zero new deps, good for autocomplete, weak for typos.
- **`rapidfuzz`** (small, fast C-extension) — token/ratio scoring over ~14k keys is sub-millisecond
  per query; the pragmatic choice. Adds one dependency.
- Token-set / n-gram index if we later need scale — not warranted at this size.

**Recommendation:** ship exact + prefix/substring first; add `rapidfuzz`-backed fuzzy behind the
`fuzzy`/`mode` flags. Keep `match_key` as the normalization layer feeding the scorer so behavior stays
consistent with the sync pipeline.

---

## 4. AWS Lambda architecture

```
          ┌────────────┐      ┌─────────────────────┐      ┌──────────────────────────┐
 client → │ API Gateway │ ───► │  Lambda (FastAPI via │ ───► │ in-memory index built at  │
          │  (REST/HTTP │      │  Mangum ASGI adapter)│      │ cold start from 3 .txt    │
          │ + API keys/ │      │  read-only handler   │      │ files (bundled or S3)     │
          │ usage plan) │      └─────────────────────┘      └──────────────────────────┘
          └────────────┘
                │ optional: CloudFront in front for edge caching + WAF
```

- **ASGI adapter:** `Mangum` wraps the FastAPI app as a Lambda handler (`handler = Mangum(app)`).
  Alternative: AWS **Lambda Web Adapter** (runs uvicorn, fewer code changes, works for both Lambda and
  containers). Either is fine; Mangum is the lightest for a pure-Lambda target.
- **Gateway choice:** **HTTP API** is cheaper/faster and sufficient; use **REST API** only if you need
  API-Gateway-native API keys + usage plans (REST API supports usage plans directly; HTTP API needs a
  Lambda authorizer or WAF for keys). Given the API-key + usage-plan requirement, **REST API** is the
  simplest path — see §6.
- **Memory/timeout:** 512 MB–1 GB (more memory = more CPU = faster cold-start index build and fuzzy
  scoring), timeout 10–15 s (reads complete in ms; headroom only).
- **Concurrency:** stateless → scales horizontally for free. Set **provisioned concurrency** (e.g. 1–2)
  if p99 cold-start latency matters to the interactive UI; otherwise on-demand is fine.
- **Statelessness:** no DB, no disk writes on the read path. The only state is the in-memory index,
  rebuilt per cold start.

### Cold start
Pure-Python (FastAPI + Mangum [+ rapidfuzz]) keeps the package small and import fast. Index build over
~14k aliases is low tens of ms. Expect cold starts in the low hundreds of ms range; mitigate with
provisioned concurrency if needed. **Build the index at module scope** (import time) so it is reused
across warm invocations, not per-request.

---

## 5. Packaging — recommendation

Two viable shapes:

**Option A — Container image, data bundled (RECOMMENDED).**
- Whitelist `.txt` files baked into the image; index built at cold start from local files.
- Pros: atomic, reproducible deploys; image is the single source of truth for code **and** data;
  no runtime S3 dependency or IAM read role on the hot path; trivial local parity (`docker run`).
- Cons: refreshing data = rebuild + redeploy image. But that is *desirable* here — whitelist changes
  already flow through the git/PR review pipeline, so coupling a data change to a deploy keeps the
  reviewed-files-are-what-ships invariant. CI builds a new image when `whitelist/*.txt` changes.

**Option B — Zip + Lambda layer, data in S3.**
- Code as zip, deps in a layer, `.txt` files pulled from S3 at cold start (cache in `/tmp`).
- Pros: refresh data (upload to S3) without redeploying code; decouples data cadence from code.
- Cons: adds S3 read IAM + network on cold start; "what data is live" is no longer pinned to a deploy
  artifact; need cache-invalidation signaling (e.g. an S3 object version surfaced as the data version).

**Recommendation: Option A (container, data bundled).** The data is tiny, changes go through PR review
already, and bundling gives the strongest reproducibility and simplest hot path. Adopt Option B later
only if whitelist refresh frequency outpaces an acceptable deploy cadence. A middle ground that
preserves A's simplicity while allowing no-redeploy refresh: bundle the files as a **fallback** and, at
cold start, optionally overlay a newer copy from S3 if `WHITELIST_S3_URI` is set.

---

## 6. Authentication — API Gateway + API key / usage plan

- Use **API Gateway REST API** with **API keys** attached to a **Usage Plan** (rate + burst + quota).
  REST API supports this natively without custom code; the Lambda stays auth-agnostic.
- Each consumer (batch pipeline, UI backend) gets its own API key → independent throttling and per-key
  CloudWatch metrics. Keys passed via `x-api-key` header.
- `GET /health` (and optionally `/docs`) left unauthenticated for load-balancer/uptime checks.
- Add **WAF** (rate-based rule, IP allow/deny) in front if exposed beyond a trusted network.
- The Lambda still validates input (batch size caps, query length) — defense in depth; never rely on
  the gateway alone for payload sanity.
- **Do not** put secrets in the read service; it serves public reference data. API keys here are for
  throttling/attribution, not confidentiality.

---

## 7. The three API-scope scenarios

What **each** scope requires. All three share the read service above; they differ
in what write/sync surface is added and the operational cost that comes with it.

### Scenario 1 — Read-only serving (RECOMMENDED default)
- **Surface:** §2 endpoints only. Sync stays the existing CLI, run as a **separate scheduled job**.
- **Requirements:** the FastAPI app + Mangum, the index built from bundled files, API Gateway + keys.
  No network egress, no write IAM, no long timeouts. Smallest blast radius, cheapest, easiest to reason
  about. Stateless → trivially scalable.
- **Sync runs out-of-band:** an **EventBridge schedule → Lambda (or Fargate/CodeBuild) → run
  `pim-whitelist update --open-pr`** on a cadence (e.g. weekly). That job does the network fetch +
  delta + PR; a human reviews/merges; merging triggers the read-service image rebuild/redeploy (Option
  A) or an S3 upload (Option B). The serve path never touches upstream NASA APIs.
  **Runner choice and cadence are designed in detail in §12 (Scheduling); the merge→deploy half of
  the loop is §13 (CI/CD).**
  - Note: the existing sync writes files in the repo and shells out to `gh` for PRs — that fits a
    build-runner (CodeBuild/Fargate with a checkout + git creds) far better than a bare Lambda.

### Scenario 2 — Read + admin sync endpoints
- **Adds:** authenticated, role-separated endpoints to *trigger* a sync and inspect results, e.g.:
  - `POST /v1/admin/sync` `{ "dataset": "all", "dry_run": true }` → starts a job, returns a job id.
  - `GET /v1/admin/sync/{job_id}` → status + the generated Markdown/JSON delta report.
  - `GET /v1/admin/deltas/latest` → most recent report.
- **Requirements beyond Scenario 1:**
  - **Async execution.** A sync hits 3+ paginated NASA APIs and the 25 MB missions crawl — far past a
    snappy request. Do **not** run it inline in the API Lambda. Instead the endpoint **enqueues**
    (Step Functions / a worker Lambda with a longer timeout / Fargate task) and returns a job id; poll
    for status. Reuse `cli.process_dataset()` and `report.render_many()` in the worker.
  - **Outbound network + retries** in the worker (the `sources/` package + `requests`), VPC/NAT only
    if egress must be controlled.
  - **A job/result store** — DynamoDB (job status) + S3 (report artifacts). The API Lambda gains
    read/write IAM to those.
  - **Stronger auth** — admin endpoints behind a *separate* API key / usage plan, ideally IAM or a
    Lambda authorizer with an admin scope, distinct from the public read key.
  - **Write target decision** — does sync still produce a git PR (human-in-the-loop, recommended;
    needs git creds + `gh` in the worker), or write straight to S3/Dynamo (faster, loses the review
    gate)?
  - It still does **not** mutate live serving data without a deploy/refresh step (Scenario 3 territory).

### Scenario 3 — Full read + write (edit whitelists via API)
- **Adds:** endpoints to create/extend concepts directly:
  - `POST /v1/{dataset}/concepts` (new concept), `POST /v1/{dataset}/concepts/{key}/aliases`
    (add alias), with append-only semantics mirroring `merge.apply_delta()`.
- **Requirements beyond Scenario 2 — this is the biggest departure from today's model:**
  - **A real datastore.** Lambda's filesystem is ephemeral and per-instance; you cannot edit the `.txt`
    files in place and have it stick or be consistent across concurrent instances. Move the canonical
    store to **DynamoDB** (or RDS, or versioned S3 objects) keyed by `(dataset, match_key)`. The `.txt`
    files become an *export/serialization* of that store, regenerated via `Whitelist.serialize()` for
    the git repo / downstream consumers.
  - **Write consistency + validation.** Enforce the existing invariants on write: `match_key`
    uniqueness within a concept, append-only (never drop curated aliases), `clean()` normalization of
    stored form. Reject writes that would merge/split concepts without explicit intent.
  - **Concurrency control** — conditional writes / optimistic locking (Dynamo condition expressions) so
    two callers don't clobber a concept.
  - **AuthN/AuthZ with real identity** — IAM/Cognito with write scopes; API keys alone are
    insufficient for attributable, authorized writes. Audit every mutation (who/what/when) — this
    replaces the git history that currently provides provenance of edits.
  - **Bypasses the git/PR review flow** that is the project's current safety mechanism. To preserve it,
    prefer "write = open a PR" (Scenario 2 style) over "write = mutate live store." If live writes are
    required, add an audit log + the ability to export to git for review after the fact.
  - **Cache invalidation** — the in-memory index must be refreshed/invalidated on write (e.g. version
    token checked per request, or push invalidation), or reads go stale across warm instances.

**Recommendation:** ship **Scenario 1** now (it delivers the core value and is operationally trivial),
keep the existing CLI sync as a scheduled out-of-band job, and treat Scenarios 2/3 as later phases only
if a clear consumer needs API-triggered sync or live editing. Scenario 3 in particular is a
re-architecture (the source of truth moves off flat files), not an add-on.

---

## 8. Pydantic response models (read service)

```python
class ConceptModel(BaseModel):
    canonical: str
    aliases: list[str]
    match_key: str                      # stable id (match_key of canonical)

class ResolveResult(BaseModel):
    query: str
    matched: bool
    match_type: Literal["canonical", "alias", "fuzzy", "none"]
    matched_alias: str | None = None
    concept: ConceptModel | None = None
    score: float | None = None

class BatchResolveRequest(BaseModel):
    queries: list[str] = Field(..., max_length=1000)
    fuzzy: bool = False
    limit_per_query: int = 1

class SearchResult(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ConceptModel]

class DatasetInfo(BaseModel):
    name: str
    concept_count: int
    alias_count: int
    data_version: str                   # content hash of the source file
```
Provenance fields (`origins`, `collection_keys`, `uuid`, `extra`) appear **only** if we persist a
provenance sidecar (see §1 gap) — otherwise omitted from the read model.

---

## 9. Project structure additions

New package (additive — does not touch sync code):
```
src/pim_whitelist/api/
  __init__.py
  app.py          # FastAPI app, routers, CORS, exception handlers
  index.py        # WhitelistIndex: build + resolve/search over Concept/match_key (reuses whitelist.py, normalize.py)
  models.py       # Pydantic request/response models (§8)
  routes/
    resolve.py    # /resolve, /resolve:batch, /validate
    search.py     # /search, /suggest
    browse.py     # /datasets, /concepts, /concepts/{key}, /stats
  handler.py      # handler = Mangum(app)   <- Lambda entrypoint
Dockerfile        # AWS Lambda Python base image, bundles whitelist/*.txt (Option A)
```
- `index.py` is the only new logic of substance; it composes `Whitelist.load`, `build_index`,
  `match_key`, and (optionally) `rapidfuzz`. Everything else is thin FastAPI glue.
- `pyproject.toml` gains an optional extra, e.g. `api = ["fastapi", "mangum", "rapidfuzz"]`, so the CLI
  install stays lean.
- Local dev: `uvicorn pim_whitelist.api.app:app --reload`. Same app object Mangum wraps in Lambda.

### Infrastructure as code (sketch)
- API Gateway **REST API** + **Usage Plan** + **API Key(s)** → Lambda integration.
- Lambda (container image, Option A), 512 MB–1 GB, 10–15 s timeout, optional provisioned concurrency.
- CloudWatch logs + metrics; optional CloudFront + WAF in front.
- (Scenario 2/3 only) EventBridge schedule, worker Lambda/Fargate, DynamoDB, S3 artifacts, scoped IAM.
- Author with SAM, CDK, or Terraform — recommend **AWS SAM** or **CDK** for the tight Lambda+Gateway
  coupling.

---

## 10. Observability & ops
- **Structured logs** (JSON) per request: dataset, query, match_type, score, latency, api-key id.
- **Metrics:** resolution hit-rate (matched vs none) is the key product metric — a falling hit-rate
  signals the whitelists need a sync. Emit as a CloudWatch metric.
- **Dashboards/alarms:** p99 latency, 5xx rate, throttle (429) rate, cold-start count.
- **Data version surfaced** in `/v1/stats` and the `ETag` so consumers and dashboards can correlate
  behavior with whitelist refreshes.

---

## 11. Verification

End-to-end verification for the **read service (Scenario 1)** once implemented:

1. **Unit:** `WhitelistIndex` resolves known cases — `match_key("multi angle imaging spectroradiometer")`
   → MISR; canonical vs alias `match_type`; unknown string → `matched: false`; batch ordering preserved.
   Reuse fixtures from `tests/` patterns.
2. **App (local):** `uvicorn pim_whitelist.api.app:app`; hit `GET /v1/instruments/resolve?q=modis`,
   `POST /v1/instruments/resolve:batch`, `/v1/platforms/search?q=terra`, `/v1/datasets`, `/health`,
   `/docs`. Confirm payload shapes match §8 and ETag/304 works.
3. **Lambda parity:** `docker build` the image; invoke locally with the Lambda Runtime Interface
   Emulator (RIE) and a sample API Gateway event; confirm Mangum returns correct status/body.
4. **Cold start budget:** log index build time at import; confirm it is well within timeout and
   acceptable for the UI (add provisioned concurrency if p99 is too high).
5. **Auth (deployed):** call without `x-api-key` → 403; with a throttled key over quota → 429.
6. **Contract:** validate `GET /v1/openapi.json` against expected schema; smoke-test a real batch from a
   consumer pipeline.

(Scenarios 2/3 add: worker job lifecycle tests, sync against `--from-cache` fixtures, datastore
read/write + concurrency tests, and audit-log verification.)

---

## 12. Scheduling (the out-of-band sync)

The **serve path** is never scheduled — it is a request/response read service. What needs a
schedule is the **sync**: periodically refresh the whitelists against upstream NASA sources so the
served data does not drift. Today that is `pim-whitelist update` run by hand; this section designs
its automation.

**The loop is the same regardless of runner:**

```
EventBridge schedule (cron)
      │
      ▼
run  pim-whitelist update --open-pr      # fetch GCMD CSV + SDE + missions, delta, consolidate, open PR
      │
      ▼
human reviews the auto-opened PR         # delta + consolidation reports are the review surface
      │
      ▼
merge to main  ──► triggers CI/CD (§13) ──► rebuild/redeploy image (or S3 refresh)
```

The serve path never touches upstream NASA APIs; only this job does. The key design fact: the
sync **writes files in the repo and shells out to `git` + `gh`** (`cli.py` `update --open-pr`), so
the runner needs a git checkout, git credentials, and the `gh` CLI — which steers the runner choice.

### Runner options

**Option S1 — EventBridge → CodeBuild  *(recommended rationale below)***
- **What it is:** a scheduled CodeBuild project checks out the repo, `pip install`s the package,
  runs `pim-whitelist update --open-pr`. Git + `gh` + credentials are first-class in CodeBuild.
- **Pipeline impact:** smallest — CodeBuild already exists in most AWS setups; one buildspec.
- **Pros:** native fit for a job whose *output is a git PR*; easy secrets (CodeBuild env / Secrets
  Manager for the `gh` token); per-run logs in CloudWatch; cheap (pay per build minute).
- **Cons:** another build project to own; cold-ish start (tens of seconds) — irrelevant for a
  weekly batch job.
- **Effort:** low.

**Option S2 — EventBridge → Fargate task**
- **What it is:** the scheduled task runs a container (could reuse the API image plus git/`gh`)
  executing the same command.
- **Pipeline impact:** medium — needs a task definition, an ECS cluster/launch config, networking.
- **Pros:** full container control; reuses container tooling; no per-build-minute model if you
  already run ECS.
- **Cons:** more moving parts (cluster, task role, subnet/SG) for what is a periodic batch job;
  the API image would need git/`gh` added (or a separate sync image).
- **Effort:** medium.

**Option S3 — EventBridge → Lambda**
- **What it is:** a worker Lambda runs the sync directly.
- **Pipeline impact:** low to write, but **only viable if the sync stops opening PRs** and instead
  writes results to S3/DynamoDB — a bare Lambda is a poor host for `git`/`gh`, and the 15-min cap
  is tight for the missions crawl.
- **Pros:** simplest infra; no build runner.
- **Cons:** **loses the git/PR review gate** that is the project's current safety mechanism;
  mismatched with today's `gh`-based flow; timeout risk on large crawls.
- **Effort:** low to wire, high in hidden cost (gives up review).

> **DECISION (runner):** ☐ S1 (CodeBuild)  ☐ S2 (Fargate)  ☐ S3 (Lambda)

### Cadence

A falling **resolution hit-rate** (§10) is the product signal that the whitelists are stale and a
sync is overdue — wire that metric to the cadence choice.

- **Weekly** — matches the existing doc's example; low churn upstream, low review burden.
- **Daily** — fresher, but more PRs to review for little delta most days.
- **On-demand only** — a manually triggered EventBridge/CodeBuild run; no cron. Good while the
  consumer base is small.

> **DECISION (cadence):** ☐ weekly  ☐ daily  ☐ on-demand

---

## 13. CI/CD

Today the repo has **no `.github/workflows/`** — only `.pre-commit-config.yaml`
(black/isort/flake8/pyupgrade) and `pytest`. This section designs the pipeline that lints/tests on
PRs and builds/deploys the read service on merge. It is the merge→deploy half of the loop in §12.

### Pipeline stages (shared across all options)

1. **On PR** — run `pre-commit` (the existing hooks) + `pytest`. Pure quality gate; no AWS access.
2. **On merge to `main`** — build the deploy artifact, then deploy it.
3. **Data refresh coupling** — when `whitelist/*.txt` changes (the output of a merged sync PR from
   §12), rebuild + redeploy so that **the reviewed files are exactly what ships** (this is the
   invariant §5 Option A is built around).

The forks below are independent — pick one from each.

### Fork CI-1 — Deploy artifact (mirrors §5 packaging)

**Option A — Container image → ECR → Lambda**  *(consistent with §5's recommendation)*
- Build the image (data bundled), push to ECR, update the Lambda to the new image tag.
- **Pros:** atomic, reproducible; image is the single source of truth for code **and** data; local
  parity via `docker run`.
- **Cons:** data refresh = full rebuild + redeploy (acceptable — data changes already go through PR).

**Option B — Zip + Lambda layer, data in S3**
- Package code as a zip, deps in a layer; upload `.txt` files to S3; refresh data by re-uploading.
- **Pros:** refresh data without redeploying code; decouples data cadence from code cadence.
- **Cons:** "what data is live" no longer pinned to a deploy artifact; adds S3 read IAM + cache
  invalidation signaling.

> **DECISION (artifact):** ☐ A (container/ECR)  ☐ B (zip+layer/S3)

### Fork CI-2 — IaC tool (mirrors §9)

| Option | Pros | Cons |
|---|---|---|
| **SAM** | Tightest Lambda+API-Gateway ergonomics; minimal YAML; `sam local` parity | AWS-only; less general than CDK |
| **CDK** | Real language (TS/Python); good for growing infra into Scenarios 2/3 | More moving parts; bootstrap step |
| **Terraform** | Cloud-agnostic; if the org already standardizes on it | More verbose for Lambda+Gateway glue |

> **DECISION (IaC):** ☐ SAM  ☐ CDK  ☐ Terraform

### Fork CI-3 — Cloud auth from CI

**Option OIDC — GitHub Actions OIDC → short-lived AWS role**  *(recommended)*
- No stored AWS keys; the workflow assumes a scoped role per run.
- **Pros:** no long-lived secrets; least-privilege per workflow.
- **Cons:** one-time IAM OIDC-provider + role trust-policy setup.

**Option Keys — long-lived IAM access keys in GitHub secrets**
- **Pros:** trivial to set up.
- **Cons:** standing credentials to rotate and guard; weaker posture.

> **DECISION (auth):** ☐ OIDC  ☐ long-lived keys

### Workflow sketches *(illustrative — not production-ready)*

```yaml
# .github/workflows/pr-checks.yml  (Fork-independent)
name: pr-checks
on: { pull_request: { branches: [main] } }
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install pre-commit pytest -e .
      - run: pre-commit run --all-files
      - run: pytest -q
```

```yaml
# .github/workflows/deploy.yml  (sketch for CI-1=A, CI-3=OIDC)
name: deploy
on:
  push:
    branches: [main]
    paths: ["src/**", "whitelist/*.txt", "Dockerfile", "pyproject.toml"]
permissions: { id-token: write, contents: read }   # OIDC
jobs:
  build-deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with: { role-to-assume: ${{ secrets.DEPLOY_ROLE_ARN }}, aws-region: us-east-1 }
      - run: |              # build + push image, then point Lambda at the new tag
          docker build -t "$ECR_REPO:$GITHUB_SHA" .
          # aws ecr get-login-password | docker login ...; docker push ...
          # sam deploy / cdk deploy / aws lambda update-function-code --image-uri ...
```

---

## 14. Division-based separation (NASA SMD divisions)

**The core question:** should divisions be **new endpoints**, or a **facet at the root** per
division? Answering it well requires two stacked decisions — the API shape (14A) and the data
prerequisite that gates it (14B).

**Grounding facts (must survive any design):**
- "Division" = the **NASA SMD science divisions**. The mapping already exists as comments in
  `src/pim_whitelist/config.py:57-64`: `CMR_API`→Earth, `SPASE_JSON`→Heliophysics,
  `PDS_API_Legacy_All`/`PDS4_API`→Planetary, `GENELAB_METADATA_OSDR`→BPS, `NAVO_HEASARC`→Astrophysics.
- A division is recorded **only at sync time** as `Provenance.collection_keys`
  (`src/pim_whitelist/sources/base.py:27`) and is **discarded on write** — the `;`-delimited
  whitelist files carry no division tags today (this is the §1 provenance gap, scoped to divisions).
- `Provenance.merge()` (`base.py:50`) **unions** collection_keys across sources, so **one concept
  can belong to multiple divisions** (e.g. an instrument flown for both Earth and Planetary). A
  design that forces one-concept-one-division would misrepresent the data.

### Decision 14A — API shape

**Option A — Facet at root (query param)**
- **What it is:** keep one index per dataset; `division` is an optional *filter*. Concepts
  advertise their divisions.
- **Endpoint shape:** `GET /v1/{dataset}/resolve?q=…&division=heliophysics` (and the same optional
  `&division=` on `search`, `suggest`, `concepts`, `validate`). Response concept gains
  `"divisions": ["earth", "planetary"]`. `GET /v1/datasets?division=…` filters counts.
- **Data-model impact:** each concept needs a division list (see 14B). No new routes.
- **Pipeline impact:** none to file layout; sync must persist divisions (14B).
- **Pros:** represents multi-division concepts **once and honestly**; backward-compatible (omit the
  param → today's behavior); cheapest to build on the existing single-index design; adding/removing
  a division is a tag change, not a route change.
- **Cons:** "only heliophysics" is a query convention, not a structural guarantee; filter logic
  lives in the handler.
- **Effort:** low.

**Option B — Separate paths per division**
- **What it is:** division becomes a path segment.
- **Endpoint shape:** `GET /v1/{division}/{dataset}/resolve?q=…`, e.g.
  `/v1/heliophysics/instruments/resolve`. A cross-division route (e.g. `/v1/all/...`) is still needed.
- **Data-model impact:** still needs division tags (14B); plus a routing layer keyed on division.
- **Pipeline impact:** none to files; more routes to version/maintain.
- **Pros:** explicit per-division base URL to hand a consumer; per-division metrics/throttling fall
  out of distinct paths naturally.
- **Cons:** a multi-division concept appears under **every** division path → duplication in
  responses, and you must decide whether the duplicates are identical or diverge; cross-division
  search needs its own route anyway.
- **Effort:** medium.

**Option C — Separate datasets per division**
- **What it is:** each (division × dataset) becomes its own dataset/file with its own index.
- **Endpoint shape:** `GET /v1/heliophysics-instruments/resolve?q=…`; files like
  `whitelist/SMD_Instruments_Heliophysics.txt`.
- **Data-model impact:** forks the file layout, the sync/consolidation pipeline, and the `DATASETS`
  registry (`config.py:40`); 3 datasets → ~15.
- **Pipeline impact:** large — consolidation, reports, and the CLI all multiply by division.
- **Pros:** maximum isolation; each division's file can be owned/reviewed independently; no runtime
  filtering.
- **Cons:** multi-division concepts are **physically duplicated** across files → a consistency
  problem (edit a concept in N files); biggest departure from today's "one consolidated file per
  dataset" model.
- **Effort:** high.

> **DECISION (API shape):** ☐ A (facet at root)  ☐ B (paths per division)  ☐ C (datasets per division)

### Decision 14B — Persisting division metadata (prerequisite that gates 14A)

**None of the 14A options work until divisions are persisted** — today the serve path has nothing
to filter on, because `collection_keys` is sync-time-only and dropped on write. So 14B must be
settled alongside 14A.

**Option X — Persist division tags now.** The sync stops discarding `collection_keys` and maps them
→ division names (mapping already at `config.py:57-64`). Two storage sub-options:

- **X-format — trailing tag on the `.txt` line.**
  e.g. `MISR;Multi-Angle Imaging SpectroRadiometer\tdiv=earth,planetary`.
  - **Pros:** one artifact; division travels with the concept.
  - **Cons:** **changes the file format** every downstream consumer of the `.txt` files parses.
- **X-sidecar — parallel `divisions.json` keyed by `match_key`.**  *(lower blast radius)*
  - **Pros:** leaves the `.txt` files byte-for-byte unchanged (no consumer breakage); the API loads
    it alongside the index.
  - **Cons:** a second artifact to keep in sync with the whitelist files.

**Option Y — Frame as a future-phase prerequisite.** Fully design division separation (pick a 14A
shape on paper) but ship Scenario 1 **division-agnostic** now; persisting `collection_keys` is
called out as the dependency that unblocks it later.
- **Pros:** keeps the current read-only service simple; no data-model change yet; honest sequencing.
- **Cons:** division filtering does not actually function until the persistence work is done.

> **DECISION (persistence):** ☐ X-format  ☐ X-sidecar  ☐ Y (defer)

### How the two decisions combine

| 14A \ 14B | X-format / X-sidecar (persisted) | Y (deferred) |
|---|---|---|
| **A — facet at root** | ✅ ships division filtering; smallest change | Design only; param inert until X |
| **B — paths per division** | ✅ works; duplicated concepts across paths | Design only; routes inert until X |
| **C — datasets per division** | ✅ works; file/pipeline fork + duplication | Not meaningful without the file split (X is implied) |

The chosen combination then flows into the rest of the system: the **sync** (§12) is where
division tags get written (X), and **CI/CD** (§13) ships them — for Option A/B via the bundled
index or sidecar, for Option C via the multiplied file set. Division also surfaces in
observability (§10): hit-rate can be reported per division to spot a stale division.

### Verification for §14 (once a combination is chosen)
- Unit: a concept sourced from two collection_keys resolves with both divisions present; a
  `division` filter (Option A) includes/excludes correctly; multi-division concept is not dropped.
- Data: the persisted tags (X) round-trip — sync writes them, the index reads them, counts in
  `/v1/datasets?division=…` match the source collection_keys.

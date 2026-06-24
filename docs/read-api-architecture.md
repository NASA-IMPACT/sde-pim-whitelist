# Read-only PIM Whitelist API with Division Filters — Architecture & Decisions

## Status

This is the **buildable, down-selected spec** for serving the curated PIM whitelists as a
read-only HTTP API with division filtering. It supersedes the open decisions in
[`fastapi-lambda-serving.md`](./fastapi-lambda-serving.md), which remains the survey of
alternatives. Where this document says "Option A," the reasoning for *why not B/C* lives in that
prior doc; here we state the choice and build on it.

**What changed since the prior doc:** division metadata now exists on disk. The CLI `classify`
command (`src/pim_whitelist/classify.py`) writes a **sidecar JSON per dataset** at
`whitelist/classified/{instruments,platforms,missions}.json`. Each record is the
`ClassifiedRecord` contract (`classify.py:48`):

```json
{ "match_key": "misr", "canonical": "MISR",
  "aliases": ["MISR", "Multi-Angle Imaging SpectroRadiometer"],
  "divisions": ["earth", "planetary"], "division_source": "provenance" }
```

This **closes the §1 / §14B "provenance gap"** the prior doc agonized over: the serve path now has
a concrete, byte-stable artifact to filter on, and because `divisions` is a **list**, a concept
flown for two divisions is represented **once and honestly**. The division taxonomy is fixed in
`config.py:71`:

```python
DIVISIONS = ("earth", "heliophysics", "planetary", "astrophysics", "bps")
```

---

## 1. Decision summary

Every open fork from the prior doc, resolved:

| Topic | Decision | Section |
|---|---|---|
| API scope (§7) | **Scenario 1 — read-only.** CLI sync stays an out-of-band job. | §8, §11 |
| Division API shape (§14A) | **Option A — facet at root.** Optional `&division=` filter; concept gains `divisions[]`. | §5, §12 |
| Division persistence (§14B) | **Already implemented** — `whitelist/classified/*.json` sidecar (the X-sidecar option). | §3, §12 |
| Search backend | **In-memory index** (no OpenSearch/SageMaker). Right at this data size; see §15. | §3, §15 |
| Packaging (§5) | **Container image, data bundled** — `.txt` files **and** `classified/*.json` baked in. | §8 |
| Auth (§6) | **API Gateway REST API + API keys + usage plan**, behind CloudFront + WAFv2. | §9 |
| Fuzzy matching (§3) | **Exact + prefix/substring now**; `rapidfuzz` behind `fuzzy`/`mode` flags. | §7 |
| Sync runner (§12) | **CodeBuild** — runs `update → classify → consolidate → open PR`. | §11 |
| Sync cadence (§12) | **On-demand only** (manual trigger; no cron). | §11 |
| CI/CD artifact (§13 CI-1) | **Container image → ECR → Lambda** (alias-published for provisioned concurrency). | §8, §11 |
| IaC tool (§13 CI-2) | **Terraform/OpenTofu (recommended** — matches the in-house `sde-elastic-wrapper` estate); **AWS SAM** as a lighter standalone alternative. | §11 |
| Environments | **dev → test → prod** promoted by git branch, per-env `tfvars` + AWS account. | §11 |
| CI/CD cloud auth (§13 CI-3) | **GitHub Actions OIDC → short-lived per-env role** (one-time bootstrap stack). | §11 |
| Edge / observability | **CloudFront caching + WAFv2 + X-Ray** tracing. | §9, §13 |

---

## 2. Scope

Read-only over reference data. No write endpoints, no DB, no network egress on the request path.
Two co-equal consumers: **batch resolution pipelines** (canonicalize messy keyword strings) and
**interactive search/autocomplete** (a UI). The sync that refreshes the data is a separate
out-of-band job (§11), exactly as today's CLI.

---

## 3. The read model (with divisions)

At cold start, per dataset, the API builds an in-memory index from **two** local artifacts and
joins them by `match_key`:

1. **The whitelist `.txt` file** — the concept/alias source of truth. Reuse as-is:
   - `Whitelist.load(path)` → `Whitelist.build_index()` (`src/pim_whitelist/whitelist.py:73,91`)
     for the O(1) `match_key → Concept` map.
   - `Concept` (`whitelist.py:17`): `.canonical`, `.aliases`, `.keys()`.
   - `normalize.match_key(text)` / `clean(text)` (`src/pim_whitelist/normalize.py:57,42`) — the
     same normalization the sync uses, so API matching is consistent with the pipeline.
   - `config.DATASETS` (`config.py:41`) + `DatasetConfig.whitelist_path` for file locations.

2. **The classified sidecar** `whitelist/classified/{dataset}.json` — a list of `ClassifiedRecord`
   (`classify.py:48`). Load it, validate each record (Pydantic), and build a
   `match_key → set[division]` map.

In-memory structures held per dataset (built once at module import, reused across warm invocations):

- `concepts: list[Concept]` — preserves file order.
- `index: dict[str, Concept]` — exact/normalized lookup via `build_index()`.
- `alias_index: dict[str, tuple[Concept, str]]` — `match_key(alias) → (concept, matched_alias)`,
  so resolve reports *which* alias matched.
- `divisions_by_key: dict[str, set[str]]` — from the sidecar; the only new data the API adds.
- `search_rows: list[tuple[str, str, Concept]]` — `(match_key, canonical, concept)` backing
  prefix/substring/fuzzy search.

**Edge case — concept not in the sidecar.** A concept added to a `.txt` file but not yet
classified (or one the classifier returned empty for) has **no divisions**. Such a concept resolves
and searches normally, but is **excluded** by any `&division=` filter. This is the honest behavior
and the `classify` step (run on each sync, §11) is what eventually fills it in. `/v1/stats` surfaces
the count of unclassified concepts so a growing gap is visible.

Data is tiny: ~570 KB of `.txt` + the sidecars → a few MB of Python objects. Build cost is
dominated by `match_key` over ~14k aliases plus the sidecar join — low tens of ms. Everything lives
in memory; no DB, no `/tmp`, no S3 on the hot path.

**Why not OpenSearch/SageMaker (as the sibling `sde-elastic-wrapper` uses).** That service backs a
genuine search problem — 10+ catalogs, relevance ranking, semantic/vector queries — so OpenSearch
Serverless + SageMaker embeddings earn their keep. Ours is *canonicalization over a fixed ~14k-alias
vocabulary*: exact `match_key` lookup is the dominant path and `rapidfuzz` covers the typo tail at
sub-millisecond cost. An in-memory index is faster (no network hop per query), cheaper (no cluster,
no endpoint), and operationally trivial (no SigV4, no IAM data-plane, no index lifecycle). The
external-search stack was considered and rejected as overkill at this scale; see §15.

---

## 4. Architecture at a glance

Stateless, read-only, single-process. The only state is the in-memory index built once at import.

```
                        request (x-api-key, ?division=)
                               │
   ┌───────────────────────────▼───────────────────────────┐
   │ CloudFront + WAFv2 — edge cache (ETag) + rate limit §9 │
   └───────────────────────────┬───────────────────────────┘
                               │ cache miss
   ┌───────────────────────────▼───────────────────────────┐
   │ API Gateway (REST) — API keys + usage plan  (§9)       │
   └───────────────────────────┬───────────────────────────┘
                               │ proxied event
   ┌───────────────────────────▼───────────────────────────┐
   │ Lambda — handler = Mangum(app)   (§8)                  │
   │  FastAPI app (api/app.py)                              │
   │   ├─ routes/  resolve · search · browse  (§5)          │
   │   ├─ models.py  Pydantic request/response (§6)         │
   │   └─ index.py  WhitelistIndex ───────────────┐         │
   └────────────────────────────────────────────────┼──────┘
                                                     │ built once
                                                     │ at import
   ┌─────────────────────────────────────────────────▼──────┐
   │ in-memory index over 3 .txt files + 3 classified/*.json │
   │  reuses Whitelist.load / build_index / match_key        │
   │  + sidecar loader (divisions). No network, no DB.       │
   └─────────────────────────────────────────────────────────┘
```

**Layering** (request → response): `routes/` (thin FastAPI glue + validation) → `WhitelistIndex`
(`index.py` — the only substantial new logic: resolve/search + division filter over
`Concept`/`match_key`, optional `rapidfuzz`) → reused core (`whitelist.py`, `normalize.py`) + a
small sidecar loader. The handler is just `Mangum(app)`; the same `app` runs locally under
`uvicorn`.

---

## 5. Endpoint catalog (division-aware)

Base path `/v1`. `{dataset}` ∈ `platforms | instruments | missions`. All JSON. The **Option A
facet** is applied uniformly: every resolve/search/browse/validate endpoint takes an optional
`&division=<name>` filter, and every returned concept carries `"divisions": [...]`.

### Resolution (core use case)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/{dataset}/resolve?q=&fuzzy=false&division=` | Resolve one string to a canonical concept. |
| `POST` | `/v1/{dataset}/resolve:batch` | Resolve up to 1,000 strings in one request. |
| `POST` | `/v1/resolve:batch` | Mixed-dataset batch; each item names its dataset. |

`GET .../resolve` response:

```json
{
  "query": "multi-angle imaging spectroradiometer",
  "matched": true,
  "match_type": "alias",
  "matched_alias": "Multi-Angle Imaging SpectroRadiometer",
  "concept": {
    "canonical": "MISR",
    "aliases": ["MISR", "Multi-Angle Imaging SpectroRadiometer"],
    "match_key": "misr",
    "divisions": ["earth", "planetary"]
  },
  "score": 1.0
}
```

- Matching: compute `match_key(q)`; hit `alias_index` for an exact normalized match
  (`match_type` = `canonical` if it equals the canonical's key, else `alias`). On miss with
  `fuzzy=true`, fall back to the ranked candidate list (§7).
- `&division=` is a **post-filter**: a match whose `divisions` does not include the requested
  division is reported as `matched: false`, `match_type: "none"` (the term exists but not in that
  division). Never 404 for a well-formed query; reserve 404 for unknown `{dataset}`.

`POST .../resolve:batch` (caps at 1,000 queries to protect the 6 MB API Gateway payload limit):

```json
// request
{ "queries": ["MODIS", "modis aqua", "nope"], "fuzzy": true, "division": "earth" }
// response
{ "results": [ {<resolve object>}, {<resolve object>}, {<resolve object>} ] }
```

### Search & autocomplete (interactive UI)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/{dataset}/search?q=&mode=substring&division=&limit=&offset=` | Ranked search (`mode` ∈ `prefix \| substring \| fuzzy`). |
| `GET` | `/v1/{dataset}/suggest?q=&division=&limit=10` | Latency-optimized typeahead (canonical + best alias). |

### Browse & detail

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/datasets?division=` | Datasets + counts (division-filtered when `division` given) + data version. |
| `GET` | `/v1/{dataset}/concepts?division=&limit=&offset=` | Paginated listing (stable file order). |
| `GET` | `/v1/{dataset}/concepts/{key}` | Concept detail by canonical `match_key` (stable id). |

### Divisions, validation & meta

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/divisions` | The 5 divisions, descriptions, and per-(dataset×division) counts. **New.** |
| `POST` | `/v1/{dataset}/validate` | Bulk "is each a known term?" → booleans + canonical (+ optional `division`). |
| `GET` | `/v1/stats` | Per-dataset counts, alias totals, **unclassified count**, data version, build time. |
| `GET` | `/healthz` | Multi-step readiness probe (see below). **No auth.** |
| `GET` | `/docs`, `/v1/openapi.json` | FastAPI Swagger UI + schema. |

`GET /v1/divisions` is sourced directly from `config.DIVISIONS` + `config.DIVISION_DESCRIPTIONS`
(`config.py:71,95`) joined with the sidecar counts.

**`/healthz` — multi-step readiness, not a bare 200.** Modeled on `sde-elastic-wrapper`'s
`app/api/health.py`, the probe runs ordered checks and returns a structured per-step report, with
**503** (not 200) if any critical step fails — so a load balancer / deploy gate fails fast on a
broken image:

1. index built for all three datasets (`concepts` non-empty),
2. each `classified/*.json` sidecar loaded and validated,
3. data-version file checksums present (the ETag source),
4. a canary `resolve` of a known concept (e.g. `misr`) returns a match.

Their probe checks live OpenSearch connectivity + permissions; ours checks the in-memory artifacts
instead — same diagnostic philosophy, no network dependency.

### Cross-cutting conventions

- **Pagination:** `limit` (default 50, max 500) + `offset`; responses include `total`.
- **Division validation:** `division` must be one of `config.DIVISIONS` → otherwise `400`.
- **ETag / caching:** every response carries an `ETag` derived from a content hash of the `.txt`
  files **and** the `classified/*.json` sidecars (the "data version"). `If-None-Match` → `304`.
  `Cache-Control: public, max-age=…` on reads so CloudFront/Gateway caching absorbs hot queries.
- **Errors:** RFC-9457 problem+json; 400 (bad query / oversized batch / unknown division),
  404 (unknown dataset/key), 429 (usage-plan throttle), 5xx.
- **Versioning:** path prefix `/v1`; data version (file hash) is separate and surfaced in headers +
  `/v1/stats`.

---

## 6. Pydantic response models

```python
class ConceptModel(BaseModel):
    canonical: str
    aliases: list[str]
    match_key: str                      # stable id (match_key of canonical)
    divisions: list[str] = []           # from the classified sidecar; [] if unclassified

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
    division: str | None = None

class SearchResult(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ConceptModel]

class DivisionInfo(BaseModel):
    name: str                           # one of config.DIVISIONS
    description: str                    # from config.DIVISION_DESCRIPTIONS
    counts: dict[str, int]              # {dataset: concept_count} in this division

class DatasetInfo(BaseModel):
    name: str
    concept_count: int                  # division-filtered when ?division= given
    alias_count: int
    unclassified_count: int
    data_version: str                   # hash of .txt + sidecar
```

`division_source` from the sidecar is **not** exposed (internal provenance of the tag, not a
consumer concern); only the resolved `divisions` list is served.

---

## 7. Fuzzy matching

- **Now:** exact `match_key` lookup (free, dominant case) + prefix/substring over match keys for
  autocomplete. Zero new runtime cost.
- **Behind `fuzzy=true` / `mode=fuzzy`:** `rapidfuzz` (small C-extension) — token/ratio scoring over
  ~14k keys is sub-millisecond per query. Added as an optional dependency in the `api` extra.
- `match_key` stays the normalization layer feeding the scorer, so fuzzy behavior matches the sync
  pipeline's notion of equality. Token-set/n-gram indexing is not warranted at this size.

---

## 8. Lambda runtime & packaging

**Scenario 1, container image, data bundled.**

- **Packaging:** a container image bakes in the FastAPI app, deps, the 3 whitelist `.txt` files,
  **and** `whitelist/classified/*.json`. Index built at cold start from local files — no runtime S3,
  no read IAM on the hot path. Refreshing data = rebuild + redeploy, which is *desirable*: whitelist
  and classification changes already flow through git/PR review (§11), so coupling data to the image
  keeps the **reviewed-files-are-what-ships** invariant.
- **ASGI adapter:** `handler = Mangum(app)`. The same `app` runs locally under `uvicorn`.
- **Gateway:** **REST API** (needed for native API keys + usage plans — see §9).
- **Memory/timeout:** 512 MB–1 GB (more memory = more CPU = faster cold-start build + fuzzy
  scoring); timeout 10–15 s (reads finish in ms; headroom only).
- **Cold start:** pure-Python (FastAPI + Mangum [+ rapidfuzz]); index build (incl. sidecar join) in
  low tens of ms; expect low-hundreds-of-ms cold starts. **Build the index at module scope** so it
  is reused across warm invocations. Add **provisioned concurrency** (1–2) only if UI p99 demands —
  which requires `publish = true` on the function and a Lambda **alias** that API Gateway invokes
  (not `$LATEST`), exactly as `sde-elastic-wrapper`'s `aws_lambda_alias.live` does.
- **Statelessness:** scales horizontally for free; the only state is the in-memory index.

> **Alternative — zip → S3 → Lambda (what the sibling uses).** `sde-elastic-wrapper` builds a zip
> via a multi-stage Docker build, uploads it to a per-env S3 bucket with a SHA256 content hash for
> change detection, and points Lambda at the S3 object. That is a proven in-house pattern and a fine
> choice. We still recommend the **container image** here because our differentiator is bundling the
> whitelist `.txt` **and** `classified/*.json` *data* atomically with the code — keeping the
> reviewed-files-are-what-ships invariant in one artifact. For a code-only function (their case,
> where data lives in OpenSearch) zip+S3 is lighter; for our data-bundled case the image wins.

---

## 9. Authentication

- **API Gateway REST API** + **API keys** on a **Usage Plan** (rate + burst + quota). REST API
  supports this natively; the Lambda stays auth-agnostic.
- Each consumer (batch pipeline, UI backend) gets its own key (`x-api-key` header) → independent
  throttling + per-key CloudWatch metrics.
- `GET /healthz` (and optionally `/docs`) left unauthenticated for uptime checks.
- **CloudFront + WAFv2 in front (standard, not optional)** — matching `sde-elastic-wrapper`'s
  `terraform/modules/cdn`: the **AWS Managed Common Rule Set** + a **rate-based rule** (e.g. 1,000
  req / 5 min per IP). The Lambda still validates input (batch caps, query length, division name) —
  defense in depth.
- **CORS restricted to known origins** (the UI's domain[s]) via settings — *not* `*`. The sibling
  allows all origins; for a UI-facing API that is worth tightening.
- **No secrets in the read service:** it serves public reference data. Keys are for
  throttling/attribution, not confidentiality.

> **Edge caching — a win the sibling can't take.** `sde-elastic-wrapper` runs CloudFront with TTL=0
> (every request passes through) because search results aren't cacheable. Our reference data **is**
> cacheable: enable real CloudFront edge caching keyed on path + query string, backed by our ETag /
> `Cache-Control` headers (§5), so hot resolve/suggest queries are absorbed at the edge and never
> reach Lambda. API-key auth still flows through (forward the `x-api-key` header); scope the cache
> key accordingly or reserve caching for the unauthenticated reference reads.

---

## 10. Project structure additions

Additive — does not touch sync code:

```
src/pim_whitelist/api/
  __init__.py
  app.py          # FastAPI app, routers, CORS, exception handlers
  settings.py     # Pydantic BaseSettings (env knobs; Lambda auto-detection)
  deps.py         # Depends() factories + @lru_cache singletons (get_index, get_settings)
  index.py        # WhitelistIndex: build + resolve/search/division-filter
                  #   (reuses whitelist.py, normalize.py)
  sidecar.py      # loads whitelist/classified/*.json → match_key → set[division]
  models.py       # Pydantic request/response (§6)
  routes/
    resolve.py    # /resolve, /resolve:batch, /validate
    search.py     # /search, /suggest
    browse.py     # /datasets, /concepts, /concepts/{key}, /divisions, /stats
  handler.py      # handler = Mangum(app)   <- Lambda entrypoint
Dockerfile        # AWS Lambda Python base image; bundles whitelist/*.txt + classified/*.json
```

- `index.py` + `sidecar.py` are the only new logic of substance; everything else is thin glue.
- **`settings.py`** uses Pydantic `BaseSettings` for the few env-driven knobs (CORS origins,
  optional `WHITELIST_S3_URI` overlay, provisioned-concurrency toggle), with **Lambda
  auto-detection** — load `.env` only off-Lambda (gate on `AWS_LAMBDA_FUNCTION_NAME`), exactly as
  `sde-elastic-wrapper`'s `app/config/settings.py`.
- **`deps.py`** exposes the index via `Depends(get_index)` backed by `@lru_cache`, so the
  module-scope singleton is injected (and trivially overridden in tests) — mirroring the sibling's
  `app/api/dependencies.py`.
- `pyproject.toml` gains an optional extra:
  `api = ["fastapi", "mangum", "pydantic-settings", "rapidfuzz"]`, so the CLI install stays lean.
- Local dev: `uvicorn pim_whitelist.api.app:app --reload`.

---

## 11. Sync (out-of-band) and CI/CD

The serve path is never scheduled. What needs running is the **sync** that refreshes the data.

### Sync job — on-demand CodeBuild

A **manually triggered CodeBuild** project (no cron) runs the full chain and opens a PR:

```
manual trigger (CodeBuild "Start build", or EventBridge later)
      │
      ▼
pim-whitelist update      # fetch GCMD CSV + SDE + missions, compute delta, consolidate
pim-whitelist classify    # tag new/empty concepts with divisions (OpenAI)  ← needs OPENAI_API_KEY
pim-whitelist consolidate # final fold / sort
  └─ --open-pr            # branch, commit .txt + classified/*.json + reports, gh pr create
      │
      ▼
human reviews the auto-opened PR   # delta + consolidation + classification are the review surface
      │
      ▼
merge to main ──► CI/CD rebuilds + redeploys the container image
```

CodeBuild is the right host because the job's **output is a git PR**: git, the `gh` CLI, and
secrets (`OPENAI_API_KEY` via Secrets Manager) are all first-class. **On-demand** suits the small
current consumer base; an EventBridge cron can be added later without changing the job. Note that
`classify` adds an OpenAI dependency and per-run cost, and that records with empty divisions are
retried on each run until the model resolves them.

### CI/CD pipeline

Two GitHub Actions workflows; cloud auth via **OIDC** (no stored AWS keys — the workflow assumes a
scoped, short-lived role per run). This mirrors `sde-elastic-wrapper`'s `.github/workflows/deploy.yml`.

1. **`pr-checks.yml`** — on PR: `pre-commit run --all-files` + `pytest`. Pure quality gate, no AWS.
2. **`deploy.yml`** — on push to an environment branch, path-filtered so a merged sync PR triggers a
   redeploy:

   ```yaml
   on:
     push:
       branches: [development, test, prod]   # branch → environment (see below)
       paths:
         - "src/**"
         - "whitelist/*.txt"
         - "whitelist/classified/*.json"   # <- classified sidecar is shipped data
         - "Dockerfile"
         - "pyproject.toml"
   permissions: { id-token: write, contents: read }   # OIDC
   ```

   Steps: resolve env from branch → assume the per-env role via OIDC → **verify AWS identity**
   (print account/role for audit, as the sibling does) → build the container → push to **ECR** →
   `terraform apply -var-file=<env>.tfvars` to point the Lambda alias at the new image.

### Multi-environment promotion (dev → test → prod)

Adopted from `sde-elastic-wrapper`, which runs three isolated environments in **separate AWS
accounts**:

- **Branch → environment:** `development → dev`, `test → test`, `prod → prod`.
- **Per-env `tfvars`** (`dev.tfvars`, `test.tfvars`, `prod.tfvars`) carry the knobs that differ:
  memory/timeout, provisioned-concurrency count, allowed CORS origins, log level.
- **Per-env AWS account + deploy role ARN**, pulled from GitHub secrets
  (`AWS_ROLE_ARN_{DEV,TEST,PROD}`) — no account ids or roles hard-coded in the workflow.
- **Remote state per env:** an S3 backend + DynamoDB lock table (one per environment), as in the
  sibling's `backend.tf`, so concurrent applies can't corrupt state.

### IaC — Terraform/OpenTofu (recommended), or AWS SAM

**Terraform/OpenTofu (recommended).** The in-house `sde-elastic-wrapper` already deploys this exact
shape with reusable modules — `terraform/modules/{lambda, api_gateway, cdn}` — so the cheapest,
most consistent path is to **reuse those modules**, not author fresh IaC. We add a `usage_plan` +
`api_key` to the `api_gateway` module and bundle our container instead of their zip. Resources:
`aws_lambda_function` (`package_type = "Image"`, `publish = true`) + `aws_lambda_alias`,
`aws_api_gateway_rest_api`/`_resource`/`_method`/`_integration`,
`aws_api_gateway_usage_plan` + `_usage_plan_key` + `_api_key`, `aws_cloudfront_distribution` +
`aws_wafv2_web_acl`, `aws_ecr_repository`, with an S3 + DynamoDB state backend.

**AWS SAM (lighter alternative).** If a standalone deploy decoupled from the sibling's estate is
preferred: `AWS::Serverless::Function` (`PackageType: Image`), a REST `AWS::Serverless::Api`, and an
`AWS::ApiGateway::UsagePlan` + `ApiKey` — minimal YAML and `sam local` parity, at the cost of not
sharing the org's proven modules.

Both consume the identical ECR image, so the choice is org-consistency vs. standalone simplicity,
not an architectural fork.

### OIDC bootstrap (one-time, per account)

A small **bootstrap stack** (run once per AWS account, separate from the app stack) creates the
GitHub OIDC identity provider, a trust policy scoped to **this repo** (`org/sde-pim-whitelist:*`),
and the per-environment deploy roles. This is the sibling's `bootstrap/iam_github.tf` pattern; copy
it and narrow the role policies to the resources this service actually touches (Lambda, API Gateway,
CloudFront, WAF, ECR, CloudWatch Logs, and the state bucket/lock table).

---

## 12. Why Option A (facet at root) for divisions

The prior doc left the division API shape open across three options. Option A wins **because the
data is already shaped for it**:

- The sidecar `divisions` field is a **list** (`classify.py:59`), and `Provenance.merge()` unions
  collection keys across sources — so a concept flown for, say, both Earth and Planetary genuinely
  belongs to both. Option A represents it **once**, with `divisions: ["earth", "planetary"]`, and a
  `&division=` filter is a simple membership test.
- **Option B** (paths per division, `/v1/heliophysics/instruments/…`) and **Option C** (separate
  files/datasets per division) would **duplicate** every multi-division concept across paths/files —
  a consistency problem on edit — and still need a cross-division route anyway.
- **14B is settled:** divisions are persisted in the sidecar; no change to the `.txt` file format,
  so no downstream `.txt` consumer breaks. The API simply loads `classified/*.json` alongside the
  index.

Adding or removing a division is a tag change in the sidecar (re-run `classify`), not a route or
file-layout change.

---

## 13. Observability

- **Structured JSON logs** per request: dataset, query, match_type, score, latency, api-key id, and
  `division` (when filtered).
- **Key product metric — resolution hit-rate** (matched vs none), emitted to CloudWatch and
  **reportable per division** so a stale division (falling hit-rate) is visible before users
  complain. This is the signal that a sync (§11) is overdue.
- **Dashboards/alarms:** p99 latency, 5xx rate, 429 (throttle) rate, cold-start count.
- **X-Ray tracing** enabled on both the Lambda and the API Gateway stage (as `sde-elastic-wrapper`
  does), so a slow request can be traced end-to-end across the gateway → handler → index build.
- **WAF CloudWatch metrics** on the managed-rule and rate-based rules (§9) for blocked-request
  visibility at the edge.
- **Data version** (hash of `.txt` + sidecar) surfaced in `/v1/stats` and the `ETag`, so behavior
  can be correlated with whitelist/classification refreshes.

---

## 14. Verification

End-to-end checks once implemented:

1. **Unit — `WhitelistIndex`:**
   - `match_key("multi angle imaging spectroradiometer")` → MISR; `match_type` distinguishes
     canonical vs alias; unknown string → `matched: false`; batch order preserved.
   - **Division join:** a concept with two sidecar divisions surfaces **both**; `&division=earth`
     includes it, `&division=bps` excludes it; a concept **absent from the sidecar** resolves with
     `divisions: []` and is excluded by any `&division=` filter.
   - Unknown `division` value → 400.
2. **App (local):** `uvicorn pim_whitelist.api.app:app`; hit `/v1/instruments/resolve?q=modis`,
   `/v1/instruments/resolve?q=modis&division=earth`, `/v1/instruments/resolve:batch`,
   `/v1/platforms/search?q=terra`, `/v1/divisions`, `/v1/datasets?division=heliophysics`,
   `/v1/stats`, `/healthz`, `/docs`. Confirm shapes match §6 and ETag/304 works.
3. **Data consistency:** counts in `/v1/datasets?division=…` and `/v1/divisions` match the
   `classified/*.json` sidecar; `unclassified_count` in `/v1/stats` equals concepts present in the
   `.txt` but missing from the sidecar.
4. **Lambda parity:** `docker build` the image (with both `.txt` and `classified/*.json` bundled);
   invoke via the Lambda Runtime Interface Emulator with a sample API Gateway event; confirm Mangum
   returns correct status/body.
5. **Cold start budget:** log index build time (incl. sidecar join) at import; confirm it is well
   within timeout and acceptable for the UI (provisioned concurrency if p99 too high).
6. **Auth (deployed):** call without `x-api-key` → 403; throttled key over quota → 429.
7. **Health probe:** `/healthz` returns 200 with all steps green on a good image; simulate a missing
   sidecar / unreadable file and confirm it returns **503** with the failing step named (§5).
8. **Contract:** validate `/v1/openapi.json` against expected schema; smoke a real batch from a
   consumer pipeline.

---

## 15. Prior art: alignment with `sde-elastic-wrapper`

This service was designed against the in-house sibling
[`sde-elastic-wrapper`](../../sde-elastic-wrapper) — same team, same SDE domain, already running in
dev/test/prod. Where its patterns are proven and apply, we adopt them; where its scale-driven
choices don't fit a tiny read-only vocabulary service, we deliberately diverge.

| Borrowed from `sde-elastic-wrapper` | Where |
|---|---|
| Terraform/OpenTofu modular IaC (`modules/{lambda, api_gateway, cdn}`) + S3/DynamoDB remote state | §11 |
| Multi-environment promotion (dev → test → prod) by git branch, per-env `tfvars` + AWS account | §11 |
| GitHub Actions **OIDC** + one-time bootstrap stack (`bootstrap/iam_github.tf`) | §11 |
| CloudFront + WAFv2 (managed rules + rate limit), X-Ray tracing | §9, §13 |
| Multi-step diagnostic health check returning 503 (`app/api/health.py`) | §5 |
| Pydantic `BaseSettings` + Lambda auto-detection; `Depends()`/`@lru_cache` DI | §10 |
| Lambda `publish` + alias for provisioned concurrency (`aws_lambda_alias.live`) | §8 |

| Deliberately different | Why |
|---|---|
| **In-memory index, not OpenSearch Serverless + SageMaker** | ~570 KB / ~14k aliases; exact `match_key` + `rapidfuzz` beats a cluster on latency, cost, and ops. Their stack is right for 10+ catalogs + semantic search; overkill here. (§3) |
| **ETag / `Cache-Control` + real CloudFront edge caching** | The sibling has no HTTP caching and runs CloudFront at TTL=0 (search results aren't cacheable). Our reference data **is** cacheable — a free latency/cost win. (§5, §9) |
| **Container image with data bundled, not zip→S3** | Bundling `.txt` + `classified/*.json` *data* atomically with code preserves the reviewed-files-are-what-ships invariant; their code-only zip→S3 is fine when data lives in OpenSearch. (§8) |
| **CORS restricted to known origins, not `*`** | Tightens a gap flagged in the sibling for a UI-facing API. (§9) |
| **6 MB response limit handled by pagination + batch caps** | The sibling needs a pre-signed-S3-URL escape hatch for its large DCAT dump; we only would if a bulk-export endpoint is added later. (§5) |

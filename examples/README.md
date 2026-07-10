# Examples

Runnable examples for consuming the PIM read API from Python.

## `query_api.ipynb`

A hands-on tour of the read-only FastAPI service: readiness checks, pagination,
type and division filters, conditional GETs (`ETag` → `304`), loading results
into pandas, and handling `422` validation errors.

### Configuration

The notebook reads its connection details from the repo's `.env` file (loaded
with `python-dotenv`; see `.env.example` for the template):

| variable           | purpose                                                             |
| ------------------ | ------------------------------------------------------------------- |
| `PIM_API_BASE_URL` | base URL of the API (defaults to `http://localhost:8000` if unset)  |
| `PIM_API_KEY`      | value sent as the `x-api-key` request header                        |

The deployed API sits behind CloudFront and **requires the `x-api-key` header**
on the `/fetch_pims_records*` endpoints (they return `403` without it; `/health`
is open). `.env` ships pointed at that endpoint with a working key, so the
notebook runs against it out of the box.

### Running it

The notebook only needs `requests` and `python-dotenv` (both already project
dependencies); `jupyter` and `pandas` are optional extras:

```bash
uv pip install jupyter pandas   # optional, for running the notebook + analysis cell
uv run jupyter lab examples/query_api.ipynb
```

### Running against a local API instead

Start the API from the repo root and point `PIM_API_BASE_URL` at it (no key
needed locally):

```bash
uv sync
uv run uvicorn pim_whitelist.api.app:app --reload --port 8000
# then set PIM_API_BASE_URL=http://localhost:8000 in .env (or your shell)
```

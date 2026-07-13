# Provenance-PIMS Division-Clustering Fine-Tune

A **standalone, isolated** pipeline that fine-tunes
[`nasa-impact/indus-sde-st-v0.2`](https://huggingface.co/nasa-impact/indus-sde-st-v0.2)
so PIMS names (instruments + platforms) embed into clusters by NASA SMD science
division.

This directory is fully self-contained. It reads the existing classified
whitelists **read-only** and touches **no** existing code, data, root
`pyproject.toml`, or `uv.lock`.

## Data

Training data is the *provenance* subset of the classified whitelists — records
whose division label came from the authoritative NASA SDE crawl
(`division_source == "provenance"`), from `whitelist/classified/instruments.json`
and `platforms.json`. Missions and OpenAI-labeled records are excluded.

- 7,228 provenance entities → 10,750 rows (one per unique alias surface form).
- Split **by entity** (no alias leakage), deterministic and seeded: train 80 /
  val 10 / test 10.
- Only 4 of 5 divisions are populated (no astrophysics provenance data);
  heliophysics dominates, bps is small — eval reports per-division metrics.

Each JSONL row: `{text, label, division, divisions, entity_id, kind, split}`.

## Setup (isolated venv — never touches the root lockfile)

```bash
uv venv finetune/.venv --python 3.12
uv pip install --python finetune/.venv -r finetune/requirements.txt
```

Use `uv pip install -r` (not `uv sync`/`uv add`) so uv never rewrites the root
`uv.lock` / `pyproject.toml`. Run everything below with `finetune/.venv/bin/python`.

## Usage

```bash
# 1. Build train/val/test splits (stdlib only — no venv needed for this step)
python finetune/build_dataset.py

# 2. Quick end-to-end smoke test on CPU (~minutes)
finetune/.venv/bin/python finetune/smoke_test.py

# 3. Full fine-tune (auto-selects CUDA -> MPS -> CPU)
finetune/.venv/bin/python finetune/train.py

# 4. Evaluate a checkpoint against the base model
finetune/.venv/bin/python finetune/evaluate.py \
    --model finetune/models/indus-sde-st-provenance-v1 --baseline
```

## How it works

- **Objective:** division clustering only. Loss is `BatchHardTripletLoss` over
  `(text, primary_division_id)` — it mines same-division positives and
  different-division negatives within each batch. This is the right objective
  with few populated classes; MNR would treat same-class rows as false negatives.
- **Sampling:** `SentenceLabelDataset` guarantees several samples per label in
  each batch so triplets can be mined in-batch.
- **Device:** auto CUDA → MPS → CPU. On MPS/CPU it forces fp32 (fp16/bf16 are
  unstable on Metal) and sets `PYTORCH_ENABLE_MPS_FALLBACK=1`.
- **Eval:** KMeans homogeneity/completeness/ARI, silhouette, and kNN division
  accuracy (overall + per-division), always compared to the base model.

## Files

| File | Purpose |
|------|---------|
| `config.py` | Self-contained taxonomy + paths (no `pim_whitelist` imports) |
| `build_dataset.py` | Classified JSON (read-only) → `data/{train,val,test}.jsonl` + manifest |
| `train.py` | Fine-tune with `BatchHardTripletLoss` |
| `evaluate.py` | Clustering + kNN division-accuracy, base-vs-tuned deltas |
| `smoke_test.py` | Tiny build→train→eval verification |
| `data/`, `models/`, `runs/` | Generated artifacts (git-ignored) |

## Caveats

- **astrophysics** has zero provenance data → no supervision; those entities are
  out-of-distribution for this checkpoint.
- Heavy class imbalance (heliophysics ≫ bps) — read per-division metrics, not
  just the aggregate.
- Small dataset → keep epochs low (default 4) and trust the val/test deltas.
- 162 multi-division records train on their primary division; the full list is
  kept for eval.

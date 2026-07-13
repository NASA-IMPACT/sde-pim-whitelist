"""Build train/val/test splits for the division-clustering fine-tune.

Reads the *provenance* subset of the classified PIMS whitelists (instruments +
platforms where ``division_source == "provenance"`` with a non-empty division
list) and emits one row per alias surface form, labeled by the record's primary
division. Splitting is done at the *entity* level so no alias of a training
entity leaks into val/test.

All inputs are read-only. Outputs land under ``finetune/data/``.

Usage (from the repo root, with any Python 3.12 — only the stdlib is needed):

    python finetune/build_dataset.py                # full build
    python finetune/build_dataset.py --limit 200    # tiny smoke build
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import config as cfg

# Split ratios (percent of the 0..99 hash bucket space). Must sum to 100.
_VAL_CUTOFF = 80  # buckets [0,80)   -> train
_TEST_CUTOFF = 90  # buckets [80,90) -> val ; [90,100) -> test


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bucket(entity_id: str) -> int:
    """Deterministic 0..99 bucket for an entity, salted with the global SEED."""
    digest = hashlib.sha1(f"{cfg.SEED}:{entity_id}".encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def _split_for(bucket: int) -> str:
    if bucket < _VAL_CUTOFF:
        return "train"
    if bucket < _TEST_CUTOFF:
        return "val"
    return "test"


def _load_provenance_records(classified_dir: Path) -> list[dict]:
    """Load provenance records (instruments + platforms), tagged with ``kind``.

    Keeps only ``division_source == "provenance"`` records whose ``divisions``
    list is non-empty and whose primary division is a recognized taxonomy value.
    """
    records: list[dict] = []
    for kind, filename in cfg.PROVENANCE_DATASETS.items():
        path = classified_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Classified file not found: {path}")
        for rec in json.loads(path.read_text(encoding="utf-8")):
            if rec.get("division_source") != cfg.PROVENANCE_SOURCE:
                continue
            divisions = [d for d in rec.get("divisions", []) if d in cfg.DIVISION_TO_ID]
            if not divisions:
                continue
            records.append(
                {
                    "kind": kind,
                    "match_key": rec["match_key"],
                    "canonical": rec["canonical"],
                    "aliases": rec.get("aliases", []),
                    "divisions": divisions,
                    "primary": divisions[0],
                    "entity_id": f"{kind}:{rec['match_key']}",
                }
            )
    return records


def _rows_for_record(rec: dict, split: str) -> list[dict]:
    """One row per unique alias surface form, labeled by the primary division."""
    seen: set[str] = set()
    rows: list[dict] = []
    for alias in rec["aliases"]:
        text = alias.strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        rows.append(
            {
                "text": text,
                "label": cfg.DIVISION_TO_ID[rec["primary"]],
                "division": rec["primary"],
                "divisions": rec["divisions"],
                "entity_id": rec["entity_id"],
                "kind": rec["kind"],
                "split": split,
            }
        )
    return rows


def build(classified_dir: Path, out_dir: Path, limit: int | None) -> dict:
    records = _load_provenance_records(classified_dir)
    records.sort(key=lambda r: r["entity_id"])  # deterministic order
    if limit is not None:
        records = records[:limit]

    splits: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    entity_ids: dict[str, set[str]] = {"train": set(), "val": set(), "test": set()}
    for rec in records:
        split = _split_for(_bucket(rec["entity_id"]))
        entity_ids[split].add(rec["entity_id"])
        splits[split].extend(_rows_for_record(rec, split))

    # Sanity: entities must never appear in more than one split.
    assert not (entity_ids["train"] & entity_ids["val"])
    assert not (entity_ids["train"] & entity_ids["test"])
    assert not (entity_ids["val"] & entity_ids["test"])

    out_dir.mkdir(parents=True, exist_ok=True)
    for split, rows in splits.items():
        path = out_dir / f"{split}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    manifest = _manifest(classified_dir, records, splits, entity_ids, limit)
    (out_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _division_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["division"]] = counts.get(row["division"], 0) + 1
    return dict(sorted(counts.items()))


def _manifest(
    classified_dir: Path,
    records: list[dict],
    splits: dict[str, list[dict]],
    entity_ids: dict[str, set[str]],
    limit: int | None,
) -> dict:
    return {
        "base_model": cfg.BASE_MODEL,
        "seed": cfg.SEED,
        "limit": limit,
        "split_cutoffs": {"train<": _VAL_CUTOFF, "val<": _TEST_CUTOFF, "test<": 100},
        "source_files": {
            filename: _sha256(classified_dir / filename)
            for filename in cfg.PROVENANCE_DATASETS.values()
        },
        "total_entities": len(records),
        "total_rows": sum(len(r) for r in splits.values()),
        "splits": {
            split: {
                "entities": len(entity_ids[split]),
                "rows": len(rows),
                "divisions": _division_counts(rows),
            }
            for split, rows in splits.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--classified-dir",
        type=Path,
        default=cfg.CLASSIFIED_DIR,
        help="Directory holding instruments.json / platforms.json (read-only).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=cfg.DATA_DIR,
        help="Where to write train/val/test JSONL + manifest.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of entities (smoke tests).",
    )
    args = parser.parse_args(argv)

    manifest = build(args.classified_dir, args.out_dir, args.limit)
    json.dump(manifest, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

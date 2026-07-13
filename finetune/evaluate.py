"""Evaluate how well name embeddings cluster by science division.

Reports, on the held-out test split:
  * KMeans(k = # populated divisions) homogeneity / completeness / ARI vs. the
    ground-truth division,
  * silhouette score of embeddings labeled by division,
  * kNN division-classification accuracy (train embeddings -> test labels), the
    most production-relevant number,
  * per-division kNN accuracy (imbalance-aware).

:func:`compare` runs the same eval on the base model and the fine-tuned model so
improvement is provable. Usable as a module (from ``train.py``) or a CLI:

    python finetune/evaluate.py --model finetune/models/indus-sde-st-provenance-v1
    python finetune/evaluate.py --model <dir> --baseline    # base-vs-tuned deltas
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import config as cfg


def _load_rows(path: Path, limit: int | None) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return rows[:limit] if limit is not None else rows


def _load_model(model_name_or_path: str, device: str):
    """Load a SentenceTransformer. Falls back to explicit mean pooling for a bare
    encoder (e.g. the un-fine-tuned base model)."""
    from sentence_transformers import SentenceTransformer, models

    try:
        return SentenceTransformer(model_name_or_path, device=device)
    except Exception:  # noqa: BLE001 - bare encoder without ST config
        word = models.Transformer(model_name_or_path)
        pooling = models.Pooling(word.get_embedding_dimension(), pooling_mode="mean")
        return SentenceTransformer(modules=[word, pooling], device=device)


def evaluate_model(model, train_rows: list[dict], test_rows: list[dict]) -> dict:
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.metrics import (
        adjusted_rand_score,
        completeness_score,
        homogeneity_score,
        silhouette_score,
    )
    from sklearn.neighbors import KNeighborsClassifier

    train_texts = [r["text"] for r in train_rows]
    train_labels = np.array([r["label"] for r in train_rows])
    test_texts = [r["text"] for r in test_rows]
    test_labels = np.array([r["label"] for r in test_rows])

    train_emb = model.encode(train_texts, normalize_embeddings=True, show_progress_bar=False)
    test_emb = model.encode(test_texts, normalize_embeddings=True, show_progress_bar=False)

    n_clusters = len(set(test_labels.tolist()))

    # Unsupervised clustering quality on the test embeddings.
    km = KMeans(n_clusters=n_clusters, random_state=cfg.SEED, n_init=10)
    pred = km.fit_predict(test_emb)
    clustering = {
        "n_clusters": n_clusters,
        "homogeneity": float(homogeneity_score(test_labels, pred)),
        "completeness": float(completeness_score(test_labels, pred)),
        "ari": float(adjusted_rand_score(test_labels, pred)),
        "silhouette": float(silhouette_score(test_emb, test_labels)),
    }

    # Supervised: does a name land near same-division names? (kNN over train.)
    knn = KNeighborsClassifier(n_neighbors=5, metric="cosine")
    knn.fit(train_emb, train_labels)
    knn_pred = knn.predict(test_emb)
    overall = float((knn_pred == test_labels).mean())

    per_division: dict[str, float] = {}
    for div, div_id in cfg.DIVISION_TO_ID.items():
        mask = test_labels == div_id
        if mask.any():
            per_division[div] = float((knn_pred[mask] == test_labels[mask]).mean())

    return {
        "clustering": clustering,
        "knn_accuracy": overall,
        "knn_accuracy_per_division": per_division,
        "test_rows": len(test_rows),
    }


def compare(tuned_model_path: str, *, device: str | None = None, limit: int | None = None) -> dict:
    """Evaluate the base model and the fine-tuned model; return both + deltas."""
    dev = device or _auto_device()
    train_rows = _load_rows(cfg.DATA_DIR / "train.jsonl", limit)
    test_rows = _load_rows(cfg.DATA_DIR / "test.jsonl", limit)

    base = evaluate_model(_load_model(cfg.BASE_MODEL, dev), train_rows, test_rows)
    tuned = evaluate_model(_load_model(tuned_model_path, dev), train_rows, test_rows)

    return {
        "device": dev,
        "base": base,
        "tuned": tuned,
        "delta": {
            "knn_accuracy": tuned["knn_accuracy"] - base["knn_accuracy"],
            "ari": tuned["clustering"]["ari"] - base["clustering"]["ari"],
            "homogeneity": tuned["clustering"]["homogeneity"] - base["clustering"]["homogeneity"],
            "silhouette": tuned["clustering"]["silhouette"] - base["clustering"]["silhouette"],
        },
    }


def _auto_device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True, help="Path/name of the model to evaluate.")
    p.add_argument("--baseline", action="store_true", help="Also eval the base model + deltas.")
    p.add_argument("--device", default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)

    dev = args.device or _auto_device()
    if args.baseline:
        report = compare(args.model, device=dev, limit=args.limit)
    else:
        train_rows = _load_rows(cfg.DATA_DIR / "train.jsonl", args.limit)
        test_rows = _load_rows(cfg.DATA_DIR / "test.jsonl", args.limit)
        report = {"device": dev, "model": args.model,
                  "eval": evaluate_model(_load_model(args.model, dev), train_rows, test_rows)}

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

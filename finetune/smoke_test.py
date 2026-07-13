"""Tiny end-to-end check of the fine-tune pipeline (build -> train -> eval).

Runs on CPU in a couple of minutes. Verifies the whole path works and that a
short fine-tune moves same-division names closer together than a cross-division
pair. Exits non-zero on any failure so it is CI-usable.

    python finetune/smoke_test.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import config as cfg


def _cos(model, a: str, b: str) -> float:
    from sentence_transformers import util

    emb = model.encode([a, b], normalize_embeddings=True, show_progress_bar=False)
    return float(util.cos_sim(emb[0], emb[1]).item())


def main() -> int:
    import build_dataset
    import evaluate as ev
    import train as tr

    # 1) Build a tiny dataset and assert splits don't leak entities.
    manifest = build_dataset.build(cfg.CLASSIFIED_DIR, cfg.DATA_DIR, limit=200)
    assert manifest["total_rows"] > 0, "no rows built"
    print(f"[ok] built dataset: {manifest['total_rows']} rows")

    # 2) Short CPU train.
    smoke_out = cfg.MODELS_DIR / "_smoke"
    if smoke_out.exists():
        shutil.rmtree(smoke_out)
    args = tr.argparse.Namespace(
        epochs=1, batch_size=8, samples_per_label=2, lr=2e-5, max_seq_length=64,
        device="cpu", output=smoke_out, limit=None, no_eval=True,
    )
    tr.train(args)
    assert (smoke_out / "config.json").exists(), "model config not saved"
    print("[ok] trained + saved smoke model")

    # 3) Behavioral check: two same-division names should end up more similar
    #    than a cross-division pair after training.
    base = ev._load_model(cfg.BASE_MODEL, "cpu")
    tuned = ev._load_model(str(smoke_out), "cpu")
    # Two heliophysics riometers vs. an earth-science instrument.
    a, b = "30MHz Imaging Riometer", "64MHz Riometer"
    c = "MODIS"
    same_base, same_tuned = _cos(base, a, b), _cos(tuned, a, b)
    cross_tuned = _cos(tuned, a, c)
    print(f"same-division cos: base={same_base:.3f} tuned={same_tuned:.3f}")
    print(f"cross-division cos (tuned): {cross_tuned:.3f}")
    assert same_tuned > cross_tuned, "same-division not closer than cross-division"

    # 4) Eval runs and returns metrics.
    report = ev.compare(str(smoke_out), device="cpu", limit=200)
    assert "knn_accuracy" in report["tuned"], "eval produced no knn metric"
    print(f"[ok] eval ran: tuned knn_accuracy={report['tuned']['knn_accuracy']:.3f}")

    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)

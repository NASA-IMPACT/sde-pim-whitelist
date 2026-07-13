"""Fine-tune ``indus-sde-st-v0.2`` to cluster PIMS names by science division.

Objective: division clustering only. Each training sample is ``(text, label)``
where ``label`` is the primary NASA SMD division id. We use a label-based triplet
loss (:class:`BatchHardTripletLoss`), which mines same-division positives and
different-division negatives within each batch — the right objective when only a
handful of classes are populated (MNR would treat same-class rows as false
negatives here).

Reads the JSONL splits produced by ``build_dataset.py`` and writes the best
checkpoint to ``finetune/models/`` and eval metrics to ``finetune/runs/``.

    python finetune/train.py                       # full run (auto device)
    python finetune/train.py --limit 200 --epochs 1 --device cpu   # smoke
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import config as cfg


def pick_device(requested: str | None) -> str:
    """Resolve the compute device: explicit arg, else cuda -> mps -> cpu."""
    import torch

    if requested:
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_rows(path: Path, limit: int | None) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return rows[:limit] if limit is not None else rows


def build_model(base_model: str, device: str, max_seq_length: int):
    """Load the base model as a SentenceTransformer with mean pooling.

    ``indus-sde-st-v0.2`` is a plain RoBERTa encoder. If it ships no pooling
    module, SentenceTransformer would append one automatically, but we construct
    it explicitly so the pooling mode (mean) is unambiguous and matches the
    bi-encoder the base was trained as.
    """
    from sentence_transformers import SentenceTransformer, models

    word = models.Transformer(base_model, max_seq_length=max_seq_length)
    pooling = models.Pooling(word.get_embedding_dimension(), pooling_mode="mean")
    return SentenceTransformer(modules=[word, pooling], device=device)


def train(args: argparse.Namespace) -> dict:
    import torch
    from sentence_transformers import InputExample, losses
    from sentence_transformers.datasets import SentenceLabelDataset
    from torch.utils.data import DataLoader

    # MPS: fp16/bf16 are unstable; force fp32 and enable CPU fallback for any op
    # not yet implemented on the Metal backend.
    device = pick_device(args.device)
    if device == "mps":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    torch.manual_seed(cfg.SEED)

    train_rows = load_rows(cfg.DATA_DIR / "train.jsonl", args.limit)
    print(f"device={device}  train_rows={len(train_rows)}")

    model = build_model(cfg.BASE_MODEL, device, args.max_seq_length)

    # SentenceLabelDataset yields batches with several samples per label, which
    # BatchHardTripletLoss needs to mine triplets in-batch.
    examples = [InputExample(texts=[r["text"]], label=int(r["label"])) for r in train_rows]
    train_ds = SentenceLabelDataset(examples, samples_per_label=args.samples_per_label)
    train_dl = DataLoader(train_ds, batch_size=args.batch_size, drop_last=True)
    train_loss = losses.BatchHardTripletLoss(model=model)

    warmup = int(len(train_dl) * args.epochs * 0.1)
    out_dir = args.output or (cfg.MODELS_DIR / "indus-sde-st-provenance-v1")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    model.fit(
        train_objectives=[(train_dl, train_loss)],
        epochs=args.epochs,
        warmup_steps=warmup,
        optimizer_params={"lr": args.lr},
        use_amp=(device == "cuda"),  # fp16 only on CUDA
        output_path=str(out_dir),
        show_progress_bar=True,
    )
    model.save(str(out_dir))
    print(f"saved model -> {out_dir}")

    summary = {
        "base_model": cfg.BASE_MODEL,
        "device": device,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "lr": args.lr,
        "max_seq_length": args.max_seq_length,
        "samples_per_label": args.samples_per_label,
        "train_rows": len(train_rows),
        "output": str(out_dir),
    }

    # Post-train evaluation vs. the base model on the held-out test split.
    if not args.no_eval:
        import evaluate as ev

        cfg.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        report = ev.compare(str(out_dir), device=device, limit=args.limit)
        (cfg.RUNS_DIR / "eval_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        summary["eval"] = report
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--samples-per-label", type=int, default=4)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--max-seq-length", type=int, default=64)
    p.add_argument("--device", default=None, help="cuda|mps|cpu (default: auto)")
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--limit", type=int, default=None, help="Cap train rows (smoke).")
    p.add_argument("--no-eval", action="store_true", help="Skip post-train eval.")
    args = p.parse_args(argv)

    train(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

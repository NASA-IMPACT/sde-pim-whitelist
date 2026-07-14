# Evaluation Results

Base (`nasa-impact/indus-sde-st-v0.2`) vs. fine-tuned
(`indus-sde-st-provenance-v1`) on the held-out test split.

- **Device:** cuda
- **Test rows:** 1005

## Supervised classifiers (division accuracy over frozen embeddings)

Each head trains on the train-split embeddings and predicts the test-split
division. Same features, different inductive biases.

| Classifier | Base acc. | Tuned acc. | Δ |
|---|---|---|---|
| kNN (k=5, cosine) | 0.7463 | 0.7303 | **−0.0159** |
| Random Forest (300 trees, balanced) | 0.7622 | 0.7701 | **+0.0080** |
| Logistic Regression (balanced) | 0.6786 | 0.6756 | **−0.0030** |

Random Forest is the best head on both models and the **only** classifier that
improves after fine-tuning.

### Per-division accuracy

**kNN**

| Division | Base | Tuned | Δ |
|---|---|---|---|
| earth | 0.6601 | 0.8824 | +0.2222 |
| heliophysics | 0.8988 | 0.7529 | −0.1459 |
| planetary | 0.4682 | 0.4220 | −0.0463 |
| bps | 0.4167 | 0.3333 | −0.0833 |

**Random Forest**

| Division | Base | Tuned | Δ |
|---|---|---|---|
| earth | 0.8268 | 0.8366 | +0.0098 |
| heliophysics | 0.8288 | 0.8191 | −0.0097 |
| planetary | 0.4798 | 0.5376 | +0.0578 |
| bps | 0.3333 | 0.3333 | 0.0000 |

**Logistic Regression**

| Division | Base | Tuned | Δ |
|---|---|---|---|
| earth | 0.7418 | 0.7255 | −0.0163 |
| heliophysics | 0.6673 | 0.6673 | 0.0000 |
| planetary | 0.6127 | 0.6185 | +0.0058 |
| bps | 0.5000 | 0.5833 | +0.0833 |

## Unsupervised clustering (KMeans, k = 4)

| Metric | Base | Tuned | Δ |
|---|---|---|---|
| homogeneity | 0.1084 | 0.1353 | +0.0269 |
| completeness | 0.1058 | 0.2090 | +0.1031 |
| ARI | 0.0454 | −0.0550 | −0.1004 |
| silhouette | 0.0021 | −0.0023 | −0.0044 |

## Takeaways

- **Fine-tuning did not clearly help.** kNN accuracy dropped (−0.016) and
  clustering ARI/silhouette both regressed; only Random Forest improved, and
  marginally (+0.008).
- **Random Forest is the strongest supervised head** on these embeddings
  (0.77 tuned) — worth preferring over kNN in production.
- **Class imbalance still dominates.** `bps` and `planetary` remain the weakest
  divisions across every classifier; `class_weight="balanced"` helps Logistic
  Regression most on `bps` (0.58 tuned) but at a large overall-accuracy cost.
- The clustering `completeness` gain with an ARI drop suggests the fine-tune
  pulled some divisions together while smearing others — consistent with the
  small dataset and heavy heliophysics skew.

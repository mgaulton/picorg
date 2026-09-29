# Accuracy-only methods review

## Question

Which recognition, quality, calibration, and clustering methods are most likely to reduce false matches and mixed-person groups in PicOrg?

## Sources

| URL/path | What it proves | Retrieved |
|---|---|---|
| `/opt/picorg/face_cluster_unmatched.py`, `/opt/picorg/face_match_benchmark.py`, `/opt/picorg/production_readiness.py` | PicOrg already has complete-link grouping, cached embeddings, labeled-pair calibration, held-out splits, and strict readiness gates. | 2026-08-21 |
| [AdaFace paper](https://arxiv.org/abs/2204.00964) | Quality-adaptive margins improve recognition under degraded image quality; official code/models are available. | 2026-08-21 |
| [MagFace paper](https://arxiv.org/abs/2103.06627) | Embedding magnitude can act as a learned face-quality signal and supports quality-aware recognition/clustering. | 2026-08-21 |
| [QMagFace repository](https://github.com/pterhoer/QMagFace) | Quality-aware face recognition specifically targets cross-age, cross-pose, and cross-quality matching. | 2026-08-21 |
| [Probabilistic Face Embeddings](https://github.com/seasonSH/Probabilistic-Face-Embeddings) | Representing each face with mean plus uncertainty can improve recognition and expose risk. | 2026-08-21 |
| [HDBSCAN documentation](https://hdbscan.readthedocs.io/en/latest/index.html) | HDBSCAN supports density-based clusters, outliers, probabilities, and parameterized cluster selection. | 2026-08-21 |
| [HDBSCAN clustering guidance](https://hdbscan.readthedocs.io/en/latest/parameter_selection.html) | Leaf selection tends to produce smaller, more homogeneous clusters than excess-of-mass selection. | 2026-08-21 |
| [InsightFace evaluation docs](https://github.com/deepinsight/insightface/blob/master/README.md) | Standard IJB/Megaface evaluation pipelines and RetinaFace/SCRFD implementations are available. | 2026-08-21 |

## Findings

- [S1] The highest-probability accuracy improvement is quality-aware reference selection, not simply lowering or raising the distance threshold. AdaFace and MagFace both make image quality part of the representation/training signal. [S2][S3]
- [S2] PicOrg already rejects corrupt, oversized, low-quality, and multi-face inputs in preflight/embedding extraction, but it does not yet learn a per-image quality weight for reference prototypes. [S1]
- [S3] A single centroid per identity is unsafe for pose/lighting/age variation. A quality-weighted medoid plus a small diverse gallery should be evaluated against the current representative-based complete-link clustering. [S1]
- [S4] Probabilistic embeddings are promising for difficult/occluded images because uncertainty can reduce the influence of ambiguous faces, but the available reference implementation is old TensorFlow code and should not be adopted directly. [S4]
- [S5] HDBSCAN can provide outlier/noise labels and soft probabilities, but its documented single-linkage behavior can bridge islands; for PicOrg it should be used as a review proposal generator, with complete-link or pairwise verification retained as the acceptance gate. [S5][S6]
- [S6] Hard-negative mining is essential: threshold calibration must include visually similar different people, same-person cross-pose/cross-quality pairs, multi-face images, and generic-folder contamination—not only random impostors. This follows the failure modes targeted by AdaFace/MagFace and the existing PicOrg held-out gate. [S1][S2][S3]
- [S7] Cross-model agreement is useful as a reject option: require dlib and InsightFace/UniFace to agree for automatic suggestions, while sending disagreements to review. It should not average incompatible distances.

## Ranked methods

1. **Quality-weighted gallery/prototype matching — P1, Adopt/Test.** Store quality, pose, face size, and embedding for each approved reference. Build identity prototypes from the top-quality diverse samples; require a candidate to agree with multiple references. Low implementation risk and directly compatible with the current DB/cache.
2. **Hard-negative and hard-positive calibration — P1, Adopt.** Expand labeled pairs by sampling nearest impostors and worst-quality genuine pairs. Recompute thresholds with image-disjoint splits and Wilson bounds. This improves accuracy evidence without changing models.
3. **Cross-model reject option — P1, Test.** Run a second backend only on candidates near the decision boundary; auto-accept only when both models agree. This trades speed for lower false matches.
4. **AdaFace/MagFace/QMagFace quality-aware embeddings — P1, Test.** Benchmark isolated models on the local held-out set. Do not promote based on published benchmark scores alone.
5. **HDBSCAN soft clustering — P2, Test for review only.** Use `probabilities_`, outlier scores, and leaf clusters to split mixed groups; never use its labels alone for automatic moves.
6. **Probabilistic embeddings — P3, Monitor.** Valuable for uncertainty-aware decisions, but the practical integration and maintenance cost is currently higher than quality-weighted prototypes and reject options.

## Recommendation

Implement quality-weighted multi-reference matching and hard-negative calibration first. These reuse PicOrg's current artifacts and directly target its observed mixed-person clusters. Then benchmark AdaFace/MagFace/QMagFace and cross-model agreement on the same held-out data. Keep HDBSCAN and probabilistic embeddings review-only until local evidence shows a reduction in false merges.

## Open gaps

- No local benchmark yet measures quality-weighted prototypes versus the current representative gallery.
- No approved model-weight license decision exists for every quality-aware model candidate.
- Current labeled pairs are far below the desired production evidence size unless the review set is expanded.

# PicOrg AI matching implementation research

## Question

Which maintained, self-hostable AI components and implementation techniques can improve PicOrg's face-match accuracy without weakening its review and safety gates?

## Sources

| ID | URL/path | What it proved | Retrieved |
|---|---|---|---|
| S1 | `https://github.com/deepinsight/insightface` | InsightFace provides face detection, alignment, recognition, and ArcFace/SCRFD tooling; the repository also documents its model-license restriction. | 2026-09-01 |
| S2 | `https://github.com/BSI-OFIQ/OFIQ-Project` | OFIQ is an open-source facial-image-quality assessment library with documented C/C++ APIs. | 2026-09-01 |
| S3 | `https://github.com/unicef/python-ofiq` | Python bindings exist for integrating OFIQ quality measurements into a Python pipeline. | 2026-09-01 |
| S4 | `https://github.com/facebookresearch/faiss` | FAISS provides exact and approximate dense-vector search, Python/NumPy bindings, CPU/GPU options, and current releases. | 2026-09-01 |
| S5 | `https://github.com/scikit-learn-contrib/hdbscan` | HDBSCAN is a maintained high-performance clustering implementation that can leave ambiguous points as noise. | 2026-09-01 |
| S6 | `https://github.com/voxel51/fiftyone` | FiftyOne provides dataset curation, similarity search, duplicate inspection, and model-error analysis. | 2026-09-01 |
| S7 | `https://arxiv.org/abs/2204.00964` | AdaFace evaluates quality-adaptive face-recognition margins; it is a model replacement requiring new embeddings. | 2026-09-01 |
| S8 | `https://github.com/IrvingMeng/MagFace` | MagFace combines face representation and quality assessment; its repository is an official, research-oriented implementation. | 2026-09-01 |
| R1 | `/opt/picorg/face_cluster_unmatched.py` | PicOrg currently extracts dlib embeddings, caches them, filters geometry, and clusters with a configurable cosine floor. | 2026-09-01 |
| R2 | `/opt/picorg/face_group_unmatched.py` | PicOrg's identity matcher already supports quality-weighted ranking, top-two margin gating, and adaptive jitter retries. | 2026-09-01 |
| R3 | `/opt/picorg/faiss_candidate_retriever.py` | PicOrg has a fail-safe FAISS adapter with gallery/database manifests and brute-force fallback, but it is not the production matcher. | 2026-09-01 |
| R4 | `/opt/picorg/review_ui.py`, `/opt/picorg/review_decision_ledger.jsonl` | Confirmed image decisions are persisted and can support active-learning calibration without changing source media. | 2026-09-01 |
| R5 | `/opt/picorg/run_face_review_pipeline.sh`, `/opt/picorg/runweb.sh` | The pipeline separates dry-run/name matching, face grouping, review, and safety-gated apply; current clustering defaults to cosine similarity `0.90`. | 2026-09-01 |

## Findings

- [S1][R1][R5] PicOrg currently has two useful but separate face paths: dlib extraction for review clustering and an optional InsightFace backend. A model switch must rebuild all reference and query embeddings; vectors from the two families must not be compared directly.
- [S2][S3][R1] Geometry-based quality is already recorded, but a standards-oriented quality score would provide stronger filtering and exemplar selection than image dimensions alone.
- [S4][R3] Exact FAISS retrieval can reduce ranking cost while preserving the current verifier, provided the index is tied to a model/database/gallery manifest and falls back on any mismatch.
- [S5][R1] HDBSCAN is useful for unknown-face exploration because it can label ambiguous points as noise; it should not replace the deterministic safety path until cluster-purity tests pass.
- [S6][R4] A dataset-curation UI can help inspect hard negatives, but PicOrg already owns the review workflow, so adding a second production UI would increase maintenance without improving the decision authority.
- [S7][S8][R4] AdaFace/MagFace are model-level experiments, not drop-in threshold changes. They require image-disjoint validation and a complete gallery rebuild.
- [R2][R4][R5] The highest-confidence local signal is the confirmed-decision ledger. Thresholds should be calibrated from confirmed genuine/impostor pairs and confidence intervals before automatic moves are enabled.
- [R5] The active rebuild is currently CPU-bound dlib extraction with two bounded workers. Replacing the model during this run would invalidate its cache and make its output incomparable.

## Recommendation

**Adopt now (high confidence):** keep the current 0.90 face-cluster floor, quality-weighted identity scoring, top-two margin, adaptive jitter, and confirmed-decision safety gates. Add calibration reports and quality metadata before changing thresholds.

**Test next (medium confidence):** run a bounded dlib-versus-InsightFace benchmark on image-disjoint confirmed pairs. If ArcFace wins on both false-match and false-nonmatch confidence bounds, rebuild the entire gallery with one model family and record its model/license metadata.

**Test after that (medium confidence):** integrate OFIQ as an optional preflight/exemplar-quality stage, then test exact FAISS retrieval using the existing adapter and parity checks. Keep brute-force fallback enabled.

**Experimental (low confidence):** evaluate HDBSCAN for unknown-only clustering and AdaFace/MagFace for a separate model benchmark. Do not change production defaults from these experiments without held-out evidence.

## Implementation plan

1. Let the current 0.90 rebuild finish and preserve its checkpoint/database artifacts.
2. Generate an image-disjoint confirmed-pair benchmark with at least 100 genuine and 100 impostor pairs where available.
3. Compare dlib and InsightFace using identical quality filters, top-two margins, and identity-level reporting.
4. Add quality-score fields and rejection reasons to preflight and review reports; keep the quality provider optional.
5. Build a versioned exact FAISS index only after top-k parity is demonstrated; include model ID, dimensions, normalization, database hash, and gallery hash.
6. Use confirmed assignments to recalibrate per-identity thresholds and report Wilson confidence intervals.
7. Enable automatic moves only when the existing precision/recall and benchmark gates pass.

## Open gaps

- The current local confirmed-pair corpus may still be too small or label-impure for production threshold calibration.
- OFIQ Python packaging, model files, runtime cost, and license compatibility with this deployment were not installed or validated.
- InsightFace model licensing must be explicitly confirmed for the intended use before adoption.
- HDBSCAN and AdaFace/MagFace have not been evaluated on PicOrg's image-disjoint corpus.
- GPU availability and storage throughput were not assumed; all recommendations retain a CPU-safe path.

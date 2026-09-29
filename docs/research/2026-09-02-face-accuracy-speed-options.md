# Additional face-accuracy and throughput options for PicOrg

## Question

Which additional, directly relevant methods or open-source components could
reduce mixed face clusters and rebuild time while preserving PicOrg's local,
human-confirmed, fail-closed workflow?

## Scope

This addendum covers only PicOrg's face extraction, gallery matching,
clustering, duplicate/quality preflight, and review-evaluation paths. It does
not propose a replacement for `photo_reorg`, a cloud identity service, or a
general infrastructure stack.

## Current anchors

- Extraction and optional SCRFD/ArcFace backend: `face_cluster_unmatched.py`,
  `insightface_backend.py`.
- Candidate identity matching: `face_group_unmatched.py`.
- SQLite gallery and cached extraction: `picorg_face_database.py`.
- Strict review clustering and `FACE_CLUSTER_SIMILARITY=0.90` launch default:
  `face_cluster_unmatched.py`, `runweb.sh`.
- Human-label calibration and image-disjoint gates:
  `build_face_pairs.py`, `split_face_pairs.py`, `production_readiness.py`.
- Non-destructive quality/duplicate checks: `media_preflight.py` and
  `dedupe_priority.py`.

## Sources

| ID | Official source | Relevant evidence | Retrieved |
|---|---|---|---|
| S1 | [AdaFace](https://github.com/mk-minchul/AdaFace) | Quality-adaptive margin recognition; the project reports gains in mixed/low-quality benchmarks and notes alignment/color-channel requirements. | 2026-09-02 |
| S2 | [MagFace](https://github.com/IrvingMeng/MagFace) | Embedding magnitude is exposed as a face-quality signal and the repo includes recognition and quality-assessment evaluation code. | 2026-09-02 |
| S3 | [HDBSCAN](https://github.com/scikit-learn-contrib/hdbscan) | Density-based clustering supports variable-density clusters, soft membership, persistence, outlier scores, and explicit noise. | 2026-09-02 |
| S4 | [FAISS index-selection guide](https://github.com/facebookresearch/faiss/wiki/Guidelines-to-choose-an-index) | `Flat` is the exact baseline; HNSW is a fast/accurate approximate option with tunable memory/search trade-offs. | 2026-09-02 |
| S5 | [ONNX Runtime quantization](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html) | Graph preprocessing and INT8 quantization can improve CPU throughput, but quantization is lossy and must be measured against accuracy. | 2026-09-02 |
| S6 | [CleanVision](https://github.com/cleanlab/cleanvision) | Finds corrupt, duplicate, blurry, and other image-dataset issues before model processing; Apache-2.0 and Python 3.9+ support. | 2026-09-02 |
| S7 | [imagededup](https://github.com/idealo/imagededup) | Provides exact/near-duplicate hashing and CNN methods plus an evaluation framework; Apache-2.0. | 2026-09-02 |

## Findings

- [S1][S2] Quality-aware embeddings are more promising for this gallery than
  lowering a global similarity threshold. They can turn blur, crop, pose, and
  low-resolution information into an explicit reject/weight signal. AdaFace is
  the stronger first candidate; MagFace is a useful quality-signal comparison
  but its official repository is an abridged research implementation with older
  dependencies. Neither should replace ArcFace until an image-disjoint local
  benchmark demonstrates lower false merges.
- [S3] HDBSCAN could expose outliers instead of forcing every embedding into a
  cluster. It is useful as a *candidate diagnostic* for the current large,
  vaguely-similar groups, but density stability is not person identity. Keep
  complete-link or all-member similarity as the final acceptance rule; use
  HDBSCAN labels/strengths only to split or prioritize review.
- [S4] The current gallery size does not justify a vector database. Use FAISS
  `IndexFlatIP`/`IndexFlatL2` as the exact baseline and consider HNSW only for a
  measured retrieval bottleneck. Approximate candidates must always be
  re-ranked by the exact verifier before a move or identity marker.
- [S5] Quantization and graph optimization can reduce CPU cost, but embedding
  drift can move the calibrated threshold. Any quantized model needs a new
  model digest, pair calibration, and a side-by-side error report; do not
  quantize the production path silently.
- [S6][S7] Preflight can remove expensive decoder work and improve gallery
  hygiene. PicOrg already has hash dedupe and preflight, so these projects are
  optional references rather than immediate dependencies. A small native
  perceptual-hash extension is lower risk than adding a second pipeline.
- [repo] The largest accuracy risk remains contaminated exemplars and
  transitive/representative clustering. A single high-quality exemplar can
  identify a person, but it must not cause weak neighboring faces to be merged.
  Store per-face quality, nearest-neighbor margin, cluster minimum/median pair
  similarity, and an abstention reason in the audit.

## Recommendations

### P1 — Test quality-aware recognition

**AdaFace** — affects `insightface_backend.py`,
`face_group_unmatched.py`, `face_cluster_unmatched.py`, and the benchmark
scripts. Advantages: quality-adaptive representation is directly aimed at
mixed-quality media; local inference is possible. Drawbacks: model conversion,
alignment, BGR/RGB compatibility, and model-weight licensing must be checked;
the repository is research-oriented. Security: pin and hash model files; do
not download weights during a production run. Health: **Acceptable** (active
research repository, but not a turnkey production package). Integration:
**High**. Recommendation: **Test**, not default.

**MagFace** — same integration area. Advantages: embedding magnitude provides
an interpretable quality/reject signal and Apache-2.0 code. Drawbacks: official
repo is abridged, older, and would require a separate inference/preprocessing
adapter. Health: **Questionable for production; useful for a benchmark**.
Integration: **High**. Recommendation: **Test only if AdaFace does not improve
the held-out error curve**.

### P1 — Tighten cluster formation without filename influence

**Two-stage graph/cluster gate** — affects `face_cluster_unmatched.py` (native
code; no new dependency). Build candidate edges with FAISS or exact cosine,
require a mutual-nearest-neighbor or all-member similarity floor, split
connected components at articulation/low-margin edges, and label singleton or
low-persistence items as `fbunknown###`. For any proposed identity, require a
second exact verifier pass and a calibrated margin over the runner-up.
Advantages: directly addresses large mixed clusters and preserves the user's
face-only rule. Drawback: more singleton clusters and compute on difficult
components. Integration: **Medium**. Recommendation: **Adopt after an
image-disjoint benchmark**.

**HDBSCAN diagnostic mode** — affects `face_cluster_unmatched.py` and audit
reports. Configure a conservative `min_cluster_size` and retain outliers/noise;
compare cluster purity and contamination against the existing 0.90 complete
link. Do not use HDBSCAN membership alone to move files. Health: **Healthy**
(BSD-3-Clause, active repository). Integration: **Medium**. Recommendation:
**Test**.

### P1 — Make speed measurable and reversible

**FAISS exact-first, HNSW candidate mode** — affects the existing FAISS adapter
and `run_ai_matching_experiments.sh`. Keep exact `Flat` scores as the oracle;
benchmark HNSW `efSearch`/`M` only for shortlist generation, then exact
re-rank. Integration: **Low/Medium**. Recommendation: **Adopt only if the
benchmark shows a real wall-time win with zero additional held-out errors**.

**ONNX Runtime graph/thread/quantization benchmark** — affects
`insightface_backend.py` and rebuild launch settings. Measure one shared
session, bounded crop batches, provider actually used, peak RAM, and INT8/FP32
score drift. Quantization should be an opt-in artifact with its own threshold
and model digest. Integration: **Medium**. Recommendation: **Test**.

### P2 — Improve input quality and review prioritization

**Native quality-weighted exemplar policy** — affects
`picorg_face_database.py` and `face_group_unmatched.py`. Keep a capped,
pose/quality-diverse exemplar set per confirmed identity; down-weight duplicates
and near-identical bursts; quarantine conflicting confirmations for review.
This uses existing metadata and avoids a new dependency. Recommendation:
**Adopt**.

**CleanVision or imagededup as an offline audit** — affects `media_preflight.py`
and `dedupe_priority.py` only. Run on a bounded sample to compare corrupt,
blur, and near-duplicate detection; do not add it to every run unless it beats
the current implementation on wall time and recall. Integration: **Low/Medium**.
Recommendation: **Monitor/Test**.

## Rejected or deferred

- Replacing the pipeline with a hosted face-search service: privacy, terms,
  uncertain coverage, and no defensible ground truth.
- Adding Qdrant/Triton/CompreFace now: useful at larger concurrent-service or
  GPU scale, but they add operational boundaries without addressing exemplar
  contamination or calibration.
- Training a custom identity model immediately: the confirmed set is still too
  small/noisy; overfitting would make the apparent accuracy worse.

## Evaluation protocol

For every candidate backend or clustering change, report on an image-disjoint
set with at least 100 genuine and 100 impostor pairs where available:

1. FMR/FNMR with 95% Wilson intervals and the chosen operating threshold.
2. Cluster purity, largest-cluster contamination, singleton/noise rate, and
   known-identity recall.
3. Exact-versus-approximate score drift, cache hit rate, images/sec, elapsed
   time, peak RAM, and decoder-error count.
4. A fixed replay of prior human confirmations, with no labels reused for
   threshold fitting.

## Final roadmap

### Implement now

- Add cluster audit metrics (minimum/median pair similarity, runner-up margin,
  quality distribution, and noise reasons).
- Keep exact re-ranking and fail-closed moves; never interpret “90%” as a
  probability.
- Cap/diversify confirmed exemplars and version the model/preprocessing digest.

### Test next

- Benchmark AdaFace against InsightFace ArcFace and dlib on the existing
  image-disjoint pair harness.
- Run HDBSCAN in report-only mode against the same embeddings and compare
  contamination of the largest clusters.
- Benchmark shared-session batching and FAISS HNSW shortlisting, retaining
  exact `Flat` as the oracle.

### Future enhancements

- Incremental connected-component reclustering: only recompute components
  touched by new hashes or newly confirmed markers.
- Optional quantized/OpenVINO/accelerated artifacts selected by a measured
  hardware profile, each with independent calibration.

### Watch list

- MagFace quality scoring if its preprocessing can be isolated cleanly.
- CleanVision/imagededup if native preflight misses a measurable fraction of
  corrupt or near-duplicate media.

### Rejected

- Cloud/open-web biometric identification and any LLM-driven identity
  assignment.

## Recommended next action

Run a report-only A/B benchmark of AdaFace versus the current ArcFace/dlib path
on an image-disjoint, human-confirmed sample; change no production threshold
until FMR/FNMR, cluster contamination, and throughput are recorded.

## Measured benchmark update (2026-09-02)

The existing pair ledger referenced moved files. The benchmark was rerun with
unique-basename relinking under `/mnt/elements16/@mixedpics_sorted`; ambiguous
basenames are rejected rather than guessed.

| Backend | Images | Pairs | FMR @ 0.40 | FNMR @ 0.40 | Elapsed |
|---|---:|---:|---:|---:|---:|
| UniFace ArcFace | 208 | 21,528 | 0.000000 | 0.123104 | 24.0 s |
| UniFace AdaFace | 208 | 21,528 | 0.000000 | 0.095004 | 45.4 s |

AdaFace improved this sample's recall at the displayed operating points, but
it was roughly 1.9x slower and the sample is derived from existing review
decisions. It is not an approval for production adoption; a dlib comparison,
cluster-purity evaluation, model/license review, and an independently held-out
set are still required. The original no-coverage run now exits nonzero instead
of being usable as evidence.

## Implementation update

PicOrg now enables an exact all-member similarity gate for review clustering by
default (`FACE_CLUSTER_STRICT_ALL_MEMBERS=1`). The prior bounded-representative
path remains available as an explicit speed/diagnostic mode. This closes the
known representative-bridge failure mode without changing identity assignment,
automatic moves, or the calibrated safety gates. Focused and full tests pass.

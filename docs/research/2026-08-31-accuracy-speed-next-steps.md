# Accuracy and speed next steps

## Question

Which changes are most likely to improve PicOrg's identity accuracy, mixed-person clustering, runtime, and operational safety without weakening review controls?

## Sources

| URL/path | What it proved | Retrieved |
|---|---|---|
| `README.md`, `docs/agent-workbench/WORK.md` | PicOrg already has fingerprinted caches, identity-isolated reference clustering, hard-negative calibration, and a failed broad automatic-move gate. | 2026-08-31 |
| [NIST FRTE 1:1 verification](https://pages.nist.gov/frvt/html/frvt11.html) | FMR/FNMR must be measured at an operating threshold; image quality materially affects both error rates. | 2026-08-31 |
| [NIST FATE quality assessment](https://pages.nist.gov/frvt/html/frvt_quality.html) | Quality filtering should be evaluated against recognition errors, not treated as a cosmetic score. | 2026-08-31 |
| [AdaFace (CVPR 2022)](https://arxiv.org/abs/2204.00964) | Quality-adaptive margins are a plausible model candidate for low-quality faces, but require local validation. | 2026-08-31 |
| [MagFace (CVPR 2021)](https://openaccess.thecvf.com/content/CVPR2021/papers/Meng_MagFace_A_Universal_Representation_for_Face_Recognition_and_Quality_Assessment_CVPR_2021_paper.pdf) | Embedding magnitude can provide a recognition-oriented quality signal and supports quality-aware gallery selection. | 2026-08-31 |
| [Faiss documentation](https://faiss.ai/) | Vector indexes support batched top-k search and speed/recall trade-offs; retrieval is separate from acceptance verification. | 2026-08-31 |
| [scikit-learn HDBSCAN](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.HDBSCAN.html) | Density clustering can leave noise unassigned and supports fine-grained leaf clusters for variable-density data. | 2026-08-31 |
| [InsightFace repository](https://github.com/deepinsight/insightface) | Code is MIT, but released recognition models have separate licensing terms that must be checked before distribution or commercial use. | 2026-08-31 |

## Findings

- [S1] PicOrg's current broad held-out calibration and hard-negative calibration fail the production gate; threshold tuning alone should not enable automatic moves. [README.md][docs/agent-workbench/WORK.md]
- [S2] The highest-value accuracy work is a versioned, image-disjoint evaluation loop with hard positives and nearest impostors, then per-identity operating thresholds. This directly measures the FMR/FNMR tradeoff described by NIST. [NIST FRTE 1:1 verification][S1]
- [S3] Quality should be an acceptance feature: face size, pose, blur, occlusion, decoder status, and embedding quality should route weak samples to review instead of allowing a weak match to move. [NIST FATE quality assessment][MagFace (CVPR 2021)]
- [S4] AdaFace and MagFace are worthwhile offline A/B candidates, but published benchmark gains do not establish performance on PicOrg's sources. [AdaFace (CVPR 2022)][MagFace (CVPR 2021)]
- [S5] FAISS can reduce search latency and enable batched top-k retrieval, but it cannot correct bad labels, thresholds, or gallery contamination. Keep a verifier after retrieval. [Faiss documentation]
- [S6] HDBSCAN's noise label and leaf selection are useful for unknown-face review, but clustering must remain identity-partitioned for known references and must not be treated as proof of identity. [scikit-learn HDBSCAN][docs/agent-workbench/WORK.md]
- [S7] The current InsightFace model artifacts need a license decision before any redistribution or non-research deployment. [InsightFace repository]

## Recommendation

1. **P0 — Keep automatic moves disabled.** Add a release gate requiring image-disjoint FMR/FNMR, Wilson upper bounds, minimum genuine/impostor counts, and zero mixed-identity clusters on a fixed regression set. Confidence: high.
2. **P0 — Turn confirmed UI decisions into an active-learning ledger.** Store SHA-256, face box, model/detector ID, embedding fingerprint, canonical identity, reviewer, and timestamp. Rebuild hard-positive and nearest-impostor sets after each review batch. Confidence: high.
3. **P0 — Make quality first-class.** Persist pose/blur/occlusion/face-size/quality fields; require multiple agreeing exemplars and send low-quality or multi-face images to review. Confidence: high.
4. **P1 — Evaluate AdaFace and MagFace offline.** Use the exact same held-out and hard-negative splits, compare at fixed FMR targets, and record model license metadata. Promote only if the upper-bound gate improves. Confidence: medium.
5. **P1 — Separate retrieval from verification.** Add a batched exact FAISS index only when profiling shows reference search is a bottleneck; retain current calibrated distance, margin, identity-consistency, and mutual-nearest-neighbor checks after retrieval. Confidence: high for speed, low for accuracy gain.
6. **P1 — Add density/outlier review.** Benchmark HDBSCAN leaf clusters against current representative clustering, with minimum cluster size and explicit noise handling; never merge across known identity partitions. Confidence: medium.
7. **P1 — Improve operational telemetry.** Add per-stage throughput, cache hit rate, decode-error categories, skipped paths, CPU/GPU utilization, and resumable manifest validation to every run. Confidence: high.
8. **P2 — Add an evidence-only visual search lane.** Use local similarity/metadata tools for review prioritization, never as an identity authority or automatic move source; retain provenance and privacy controls. Confidence: medium.

## Open gaps

- No local benchmark has yet compared AdaFace/MagFace with PicOrg's actual media and confirmed labels.
- The current gallery contains substantial unreadable/decode-failure coverage; repairing or permanently excluding those files should precede another model comparison.
- Retrieval is now measured as a major post-extraction cost; an end-to-end run
  still needs stage timings to quantify decode and embedding extraction costs.
- Model and dataset licensing for any external model must be reviewed for the intended use.

## Implemented in PicOrg

- Review events now append to `review_decision_ledger.jsonl`, including image hashes captured before moves.
- The apply path can require a held-out benchmark and fails closed on insufficient samples or Wilson-bound violations; the TUI's move action enables this gate by default.
- Identity matching progress includes a bar, throughput, and ETA while remaining line-oriented for screen/TUI logs.
- A bounded exact-retrieval benchmark now compares FAISS `IndexFlatL2` with the
  NumPy brute-force ranking path. On 5,000 native-cache vectors and 256
  queries, top-10 rankings were identical for all 256 queries; FAISS search
  was approximately 12x faster (0.015s vs 0.186s). This is a speed result,
  not an accuracy result, and does not yet justify changing production search.
- A full native-cache sample (51,399 vectors, 128 dimensions, 256 queries)
  also had 256/256 top-10 parity. FAISS search was ~29.5x faster (0.083s vs
  2.443s), but JSON/cache loading took 6.0s and dominates this one-shot
  command. Face extraction and cache reuse therefore remain higher-value
  runtime targets than adding a persistent index today.
- An identity-level benchmark against the active SQLite database (488
  identities, 29,106 references, 256 queries) retained the current top-10
  identity ranking for 256/256 queries. Exact FAISS search over the complete
  gallery took 7.47s versus 32.25s for the current Python scan (~4.3x faster),
  making retrieval optimization worthwhile after extraction is complete.
- A spread-across-gallery bounded-k probe showed why a shortcut needs a
  safety margin: 256 candidates preserved only 39/64 identity rankings, 512
  preserved 59/64, while 2,048 preserved 64/64 (~75x faster than the current
  scan for this 64-query sample). This is promising for a staged candidate
  retriever, but the sample is not a production accuracy gate.
- The image-disjoint reviewed set supplied 169 query images not present in the
  reference database. At `retrieval_k=2,048`, FAISS preserved all 169 current
  identity top-10 lists and reduced ranking time from 18.55s to 0.35s (~52.6x
  faster). This is a strong candidate-retrieval result, but acceptance still
  requires the existing distance, margin, quality, and held-out gates.
- A larger spread sample from the latest confirmed image ledger supplied 512
  additional disjoint queries. At `retrieval_k=2,048`, all 512 identity
  top-10 lists matched the current ranker; ranking time was 0.83s versus
  51.71s (~62.4x faster). The result strengthens the candidate retriever case
  but does not replace the face-accuracy release gate.

The opt-in adapter scaffold (`faiss_candidate_retriever.py`) validates model,
dimension, metric, normalization, gallery, and database fingerprints before
serving candidates and returns the full brute-force gallery on any mismatch.
The review also tightened the scaffold to include reference paths in the
gallery hash, reject empty/invalid fingerprints, and atomically replace
manifest files; it remains unwired by default.

The benchmark is available through the optional `retrieval` extra:

```bash
UV_CACHE_DIR=/tmp/picorg-uv-cache uv sync --frozen --extra retrieval
PYTHONPATH=. .venv/bin/python faiss_exact_benchmark.py \
  --embeddings .cache/picorg/face-embeddings-native.json \
  --limit 5000 --queries 256 --top-k 10 \
  --output .cache/picorg/faiss-exact-benchmark.json
```

Before production adoption, compare identity candidate sets at a bounded
retrieval `k` (not only nearest-vector rows) on image-disjoint reviewed pairs,
and add an atomic index manifest
containing model ID, dimension, normalization, gallery hash, and build
timestamp. Keep the calibrated verifier and margin gate after any FAISS
retrieval.

## Next five-minute verification

Run the existing held-out benchmark and confirm that its report records model ID, threshold, FMR/FNMR, confidence bounds, and sample counts before any apply run:

```bash
./run_accuracy_benchmark.sh
```

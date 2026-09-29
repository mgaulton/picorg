# PicOrg upstream face-pipeline review

## Question

Which actively maintained upstream components can materially improve PicOrg's face matching, review, scalability, and reliability without replacing its deterministic registry-first workflow?

## Sources

| URL/path | What it proved | Retrieved |
|---|---|---|
| `/opt/picorg/README.md`, `pyproject.toml`, `requirements-face.txt` | PicOrg already has durable audits/caches, a LAN review UI, dlib/InsightFace backends, and Python 3.11 optional face environments. | 2026-08-21 |
| [InsightFace README](https://github.com/deepinsight/insightface/blob/master/README.md) | InsightFace provides RetinaFace/SCRFD detection and evaluation pipelines. | 2026-08-21 |
| [InsightFace licensing](https://github.com/deepinsight/insightface/blob/master/server/LICENSING.md) | Source/SDK are MIT, but public pretrained model packages are generally restricted to non-commercial academic research. | 2026-08-21 |
| [UniFace repository](https://github.com/yakhyo/uniface) | UniFace combines RetinaFace/SCRFD, ArcFace/AdaFace-family recognition, quality assessment, FAISS storage, and verified weight downloads under an MIT project. | 2026-08-21 |
| [FiftyOne documentation](https://docs.voxel51.com/) | FiftyOne provides dataset curation, embedding visualization, similarity search, duplicate discovery, and interactive review. | 2026-08-21 |
| [FiftyOne embeddings guide](https://docs.voxel51.com/tutorials/image_embeddings.html) | Embedding visualization is intended to expose anomalous samples, incorrect labels, and hard examples. | 2026-08-21 |
| [FAISS overview](https://engineering.fb.com/2017/03/29/data-infrastructure/faiss-a-library-for-efficient-similarity-search/) | FAISS is designed for efficient large-scale multimedia/vector similarity search. | 2026-08-21 |
| [DeepFace verification API](https://github.com/serengil/deepface/blob/master/deepface/modules/verification.py) | DeepFace wraps many models and detectors, but exposes many threshold/model combinations. | 2026-08-21 |
| [DeepFace confidence bug](https://github.com/serengil/deepface/issues/1613) | A recent issue reports misleading confidence=100 results for pre-extracted faces with `detector_backend="skip"`. | 2026-08-21 |

## Findings

- [S1] PicOrg already has the important production primitives—registry filtering, preflight, SHA-256/file-fingerprint caches, checkpointed face extraction, audits, and explicit apply gates—so a wholesale replacement would add migration risk (`README.md`, `runweb.sh`, `face_cluster_unmatched.py`).
- [S2] InsightFace is a technically strong backend already integrated locally, but its public model weights carry a materially different license from the MIT code. PicOrg's production gate should continue requiring an explicit model-license decision. [S2]
- [S3] UniFace is a credible test candidate for a quality-aware detector/embedding adapter: its upstream README documents CPU/CUDA support, verified SHA-256 weight downloads, quality assessment, and FAISS storage. It should remain isolated from the production cache until its model licenses and accuracy on PicOrg's held-out pairs are verified. [S3]
- [S4] FiftyOne directly addresses PicOrg's current hardest operational problem—mixed-person clusters and label-quality review—through embedding visualization, similarity search, duplicate discovery, and interactive dataset views. It complements `review_ui.py`; it should not replace the identity/move authorization UI. [S4][S5]
- [S5] FAISS is appropriate only when the reference database grows beyond the current brute-force comparison scale. It can reduce nearest-neighbor search cost, but it does not improve identity correctness or threshold calibration by itself. [S6]
- [S6] DeepFace is useful as an offline comparison harness, not as a production confidence source. Its broad model surface increases calibration burden, and the reported `skip` confidence bug is directly contrary to PicOrg's accuracy-first policy. [S7][S8]
- [S7] The highest-value missing capability is not another model: it is a versioned, image-disjoint evaluation dashboard that records model, detector, threshold, license, preflight coverage, and held-out FMR/FNMR for every rebuild. This is the only reliable way to decide whether a new backend is better for PicOrg.

## Candidate evaluation

| Existing implementation | Candidate | Benefits | Drawbacks | Integration effort | Recommendation |
|---|---|---|---|---|---|
| `face_cluster_unmatched.py` dlib/InsightFace paths | UniFace | Quality scoring, multiple detectors/recognizers, verified weights, optional FAISS | New dependency/model matrix; model-license verification still required; no PicOrg adapter | Medium | **Test, P1** |
| `review_ui.py` plus audit JSON | FiftyOne | Fast visual discovery of mixed clusters, outliers, duplicates, and hard examples | Heavy optional dependency and separate app/database; not an authorization workflow | Medium | **Test, P1** |
| Brute-force face/reference comparisons | FAISS | Faster top-k retrieval at large scale | Retrieval is not verification; index rebuild/versioning and threshold calibration required | Low/Medium | **Monitor, P2** |
| Optional InsightFace backend | DeepFace | Convenient model comparison and API wrappers | Broad dependency surface; confidence semantics and thresholds need careful auditing | Medium | **Reject for production; use offline only, P3** |

## Recommendation

High confidence: keep PicOrg's deterministic pipeline and current InsightFace/dlib adapters as the production path. Add an optional UniFace benchmark adapter and export PicOrg audits/embeddings to a temporary FiftyOne dataset for review of hard clusters. Do not change automatic-move thresholds until a held-out, image-disjoint benchmark demonstrates improvement.

## Final roadmap

### Implement Now

1. Add a `model-run-manifest` record containing backend/model/detector/threshold/license/preflight/cache versions.
2. Add a benchmark command that compares dlib, current InsightFace, and optional UniFace on the existing confirmed pairs plus a held-out split.
3. Keep model-license checks fail-closed; do not treat the InsightFace MIT code license as a model-weight license.
4. Add a read-only export from reconciled audits to a FiftyOne dataset; keep moves/identity assignment in `review_ui.py`.

Status: the run manifest metadata extension is implemented in `pipeline_run_manifest.py` and wired through `run_face_review_pipeline.sh`; the benchmark and FiftyOne export remain future work.

### Test Next

- UniFace CPU adapter against the existing pair benchmark, requiring at least 100 genuine and 100 impostor pairs before any threshold change.
- FiftyOne on a bounded 5,000-image sample to inspect mixed clusters, corrupt media, and generic-folder leakage.
- FAISS only after measuring current nearest-neighbor latency and reference count.

### Future Enhancements

- Versioned embedding indexes with atomic rebuild/swap and rollback.
- Per-identity quality-aware reference selection and drift reports.
- Active-learning export of approved/rejected UI decisions into the held-out benchmark.

### Watch List

- UniFace release/model-license changes.
- InsightFace server model-license tooling and ONNX Runtime GPU support.
- FiftyOne plugins for review and embedding analysis.

### Rejected

- DeepFace as a production confidence oracle: too many model/detector combinations and a current confidence-reporting issue for a high-accuracy automatic-move pipeline.

## Open gaps

- Exact upstream release cadence, issue counts, and hardware benchmarks were not independently normalized across repositories in this pass.
- UniFace model licenses must be inspected per downloaded weight before deployment.
- No local benchmark has yet measured whether FAISS is faster than PicOrg's current workload.

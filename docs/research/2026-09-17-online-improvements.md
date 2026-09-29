# PicOrg online improvement research

## Question

Which actively maintained, directly relevant techniques or components can improve PicOrg's face-quality decisions, face-only clustering, incremental matching speed, and evidence-store reliability without weakening its local, read-only-reference, review-first boundaries?

## Project boundary inspected

PicOrg currently owns intake/name-audit/dedupe, review UI, durable evidence and assignment ledgers, incremental embedding caches, face-only reconciliation, scheduler jobs, and the canonical baseline builder (`review_ui.py`, `picorg_scheduler.py`, `identity_evidence_store.py`, `build_canonical_face_baseline.py`). Face extraction and the legacy high-accuracy database remain compatible with photo_reorg; InsightFace is an optional local backend. MD/RD roots are read-only reference sources. The existing model/library comparison is in `docs/research/2026-09-16-face-model-library-fit.md`.

## Sources

| URL/path | What it proved | Retrieved |
|---|---|---|
| `face_cluster_unmatched.py`, `face_group_unmatched.py`, `identity_evidence_store.py`, `build_canonical_face_baseline.py`, `picorg_scheduler.py` | Current local extraction, strict face-only clustering, durable WAL evidence store, content-addressed baseline, and scheduled incremental jobs | 2026-09-17 |
| [Faiss index-selection guidelines](https://github.com/facebookresearch/faiss/wiki/Guidelines-to-choose-an-index) | `IndexFlat` is exact; HNSW/IVF/PQ trade memory, speed, and recall; HNSW uses `efSearch` to trade speed for accuracy and does not support removal | 2026-09-17 |
| [Faiss index reference](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes) | Exact Flat and approximate HNSW/IVF/PQ index behavior and tuning parameters | 2026-09-17 |
| [HDBSCAN API](https://hdbscan.readthedocs.io/en/latest/api.html) | Cluster membership probabilities, persistence, outlier scores, and approximate prediction are available for diagnostics | 2026-09-17 |
| [HDBSCAN soft clustering](https://hdbscan.readthedocs.io/en/latest/soft_clustering_explanation.html) | Soft membership and outlier scores can distinguish core members from uncertain assignments | 2026-09-17 |
| [HDBSCAN algorithm notes](https://hdbscan.readthedocs.io/en/latest/how_hdbscan_works.html) | Single-link structure can allow noise bridges to join otherwise separate islands | 2026-09-17 |
| [OFIQ Project](https://github.com/BSI-OFIQ/OFIQ-Project) and [OFIQ license](https://github.com/BSI-OFIQ/OFIQ-Project/blob/main/LICENSE.md) | BSI reference implementation for ISO/IEC 29794-5 face-image quality; code is MIT, while model/dependency terms must be reviewed separately | 2026-09-17 |
| [ONNX Runtime graph optimizations](https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html) | Basic, extended, and layout graph optimization can be performed offline and reused at startup | 2026-09-17 |
| [ONNX Runtime execution providers](https://onnxruntime.ai/docs/execution-providers/) | The same ONNX model can target CPU, CUDA, OpenVINO, and other providers through a common interface | 2026-09-17 |
| [ONNX Runtime OpenVINO provider](https://onnxruntime.ai/docs/execution-providers/OpenVINO-ExecutionProvider.html) | OpenVINO is an optional CPU/GPU/NPU provider with version compatibility requirements; OpenVINO should perform its own graph optimization | 2026-09-17 |
| [SQLite WAL documentation](https://sqlite.org/wal.html) | WAL improves reader/writer concurrency, requires same-host access, needs checkpoint management, and should be kept with its `-wal`/`-shm` files when copied | 2026-09-17 |
| [SQLite WAL documentation, reset-bug section](https://sqlite.org/wal.html#the_wal_reset_bug) | A rare WAL-reset race is fixed in SQLite 3.51.3 and backported to 3.44.6/3.50.7; deployments should verify their runtime SQLite version | 2026-09-17 |
| [SQLite Backup API](https://sqlite.org/backup.html) | The online backup API provides a consistent backup path while a database is in use | 2026-09-17 |
| [AdaFace](https://github.com/mk-minchul/AdaFace), [CVLFace](https://github.com/cvl/cvlface), and [paper](https://arxiv.org/abs/2204.00964) | Quality-adaptive face embeddings are a plausible accuracy challenger for low-quality media, but require an isolated adapter, model-license review, and local benchmark | 2026-09-17 |
| `docs/research/2026-09-16-face-model-library-fit.md` | Existing project-specific model comparison and prior AdaFace/MagFace/UniFace/LVFace/DeepFace/CompreFace decisions | 2026-09-17 |

## Findings

- [S1] PicOrg already has the most important safety primitive for approximate retrieval: exact matching remains available, while `faiss_candidate_retriever.py` and related benchmarks can be treated as accelerators rather than authorities. Faiss documentation supports keeping `IndexFlat` for exact results and using HNSW/IVF only when recall is measured on the full gallery.
- [S2] The current strict all-member cluster rule addresses representative-chain merges, but it does not expose enough evidence for a reviewer to distinguish a dense identity cluster from a low-persistence/noise bridge. HDBSCAN's probability, persistence, and outlier fields are suitable diagnostics; its own algorithm notes warn that bridges can connect islands, so HDBSCAN must not be accepted as an unconditional grouping authority.
- [S3] PicOrg's current quality fields are model/backend-specific. OFIQ is directly relevant as an isolated pre-embedding quality gate because it targets standardized face-image quality, but the official implementation is C/C++ and its model/dependency licensing must be recorded separately from its MIT code license.
- [S4] ONNX Runtime's offline graph optimization can reduce repeated model setup cost without changing the model. OpenVINO is a hardware-specific optional benchmark only; it must not be enabled on a host without verifying the installed provider/model compatibility and output parity.
- [S5] PicOrg's evidence database already uses WAL (`identity_evidence_store.py:179-182`). SQLite documents that WAL is same-host only, that checkpoints affect read latency, and that copying a WAL database without its `-wal`/`-shm` state is unsafe. A scheduled online backup plus `quick_check`/`integrity_check` health result is therefore more valuable than adding another database service.
- [S5a] SQLite's current WAL documentation records a rare WAL-reset corruption race fixed in 3.51.3, with backports to 3.44.6 and 3.50.7. PicOrg should expose the runtime SQLite version and fail readiness if it is below an approved fixed version.
- [S6] AdaFace remains the strongest model challenger identified so far for mixed-quality images, but the existing project research already shows that public benchmark claims cannot replace image-disjoint PicOrg labels, per-identity slices, and license review. No new model should be promoted from this research alone.

## Recommendations

### P1 — Test next: standardized quality-gate pilot (OFIQ)

- **Project/module:** OFIQ reference implementation; affects the pre-embedding path in `face_cluster_unmatched.py`, `build_canonical_face_baseline.py`, and `media_preflight.py`.
- **Complements/replaces:** complements existing blur/pose/face-count checks; does not replace InsightFace/dlib or the current preflight.
- **Gap:** a backend-neutral, auditable quality score for selecting exemplars and routing poor images to `needs_attention`.
- **Benefits:** quality-diverse canonical exemplars, fewer low-quality false matches, and a quality signal that can be compared across backends.
- **Drawbacks:** C++/model packaging, extra CPU cost, and separate model/dependency license review.
- **Security/privacy:** keep it local; do not upload images. Pin source revision and model files; verify checksums and licenses.
- **Health/compatibility:** official BSI project is active enough for a bounded pilot; Python wrappers are secondary and should not be trusted without parity tests. Linux CPU integration is feasible but not yet production-approved.
- **Complexity/priority/status:** Medium / P1 / **Test**; deployment **isolated pilot**.
- **Fixture and acceptance:** 1,000 image-disjoint confirmed images plus 200 unreadable/blurred/occluded cases. Require lower FNMR at fixed FMR, no regression in cluster purity, quality-score monotonicity on known corrupt samples, and a documented runtime budget.
- **Rollback/owner:** delete only the adapter/cache and keep the current quality fields; owner is PicOrg face-pipeline maintainer.

### P1 — Test next: cluster-risk diagnostics with HDBSCAN metadata

- **Project/module:** HDBSCAN diagnostics around `face_cluster_unmatched.py` and `face_cluster_purity_report.py`.
- **Complements/replaces:** complements strict all-member clustering; does not replace the current face-only cluster IDs or confirmation workflow.
- **Gap:** large vague clusters lack a visible, machine-readable purity/risk explanation.
- **Benefits:** expose membership probability, cluster persistence, outlier scores, bridge candidates, and a `needs_attention` reason in the UI.
- **Drawbacks:** HDBSCAN's single-link structure can still bridge islands; adding it as the sole clusterer would risk the exact failure the project is trying to prevent.
- **Security/privacy:** local numerical processing only; no new external service.
- **Health/compatibility:** mature Python/scikit-learn ecosystem; pin the version and record the algorithm parameters in each audit.
- **Complexity/priority/status:** Low–Medium / P1 / **Test**; deployment **project-local pilot**.
- **Fixture and acceptance:** replay the current face-only audit; require every proposed cluster to pass the existing 0.90 all-member gate or remain split, and require cluster-risk fields to explain every rejected/bridged member. Compare purity and review count against the current algorithm.
- **Rollback/owner:** disable diagnostics and retain existing cluster manifests; owner is review/clustering maintainer.

### P1 — Implement/test: two-stage Faiss retrieval with exact rerank

- **Project/module:** `faiss_candidate_retriever.py`, `faiss_*_benchmark.py`, and the match path used by `face_group_unmatched.py`.
- **Complements/replaces:** complements exact search; does not replace exact reranking or safety gates.
- **Gap:** repeated full-gallery comparisons become expensive as the durable baseline grows.
- **Benefits:** HNSW/IVF can shortlist candidates while exact distance remains the final authority; batching and `efSearch`/`nprobe` are documented tuning levers.
- **Drawbacks:** index rebuild/staleness, memory use, and possible recall loss; HNSW does not support removal, so incremental updates need a rebuild or immutable-generation strategy.
- **Security/privacy:** local in-process library; pin Faiss and avoid untrusted index files.
- **Health/compatibility:** Faiss is a mature, directly relevant dependency; current gallery size may not justify approximate search yet.
- **Complexity/priority/status:** Medium / P1 / **Test**; deployment **not installed as a production authority**.
- **Fixture and acceptance:** run the full image-disjoint gallery. Candidate recall must be 100% at the selected top-k, exact-reranked predictions must be byte/score-equivalent within tolerance, and wall time/RAM must improve materially before adoption.
- **Rollback/owner:** switch the feature flag to exact search and discard only the generated index; owner is matching-performance maintainer.

### P1 — Implement now: SQLite health, version, and backup routine

- **Project/module:** `identity_evidence_store.py`, scheduler `picorg_scheduler.py`, and the UI `/api/rebuild-status`/health surface.
- **Complements/replaces:** complements the existing WAL store; no replacement database.
- **Gap:** a persistent store can be healthy while a stale WAL copy, failed checkpoint, unsupported SQLite version, or corruption is not surfaced early.
- **Benefits:** consistent online backups, explicit `quick_check`/`integrity_check`, checkpoint size/latency metrics, and recoverable evidence state.
- **Security/privacy:** backups contain face embeddings and identity metadata; restrict permissions, retain locally, and never expose through the LAN UI.
- **Compatibility:** same-host WAL constraint must remain documented; backup must include SQLite's consistent online API output, not a raw main-file copy.
- **Complexity/priority/status:** Low / P2 / **Adopt**; deployment **project-local**.
- **Acceptance:** readiness reports the runtime SQLite version and rejects unapproved pre-fix versions; scheduled backup succeeds during a UI read, integrity check passes after restore into a temporary database, and a simulated interrupted run leaves the previous DB usable.
- **Rollback/owner:** remove the scheduled health job; the primary DB and existing WAL behavior remain unchanged.

### P2 — Test next: offline ONNX optimization/provider benchmark

- **Project/module:** optional optimization in the InsightFace backend and rebuild/match launchers.
- **Complements/replaces:** complements current ONNX Runtime CPU execution; no model or output contract change.
- **Gap:** repeated model initialization and CPU inference overhead.
- **Benefits:** lower startup cost and possibly better throughput; OpenVINO can be tested where Intel hardware is actually present.
- **Drawbacks:** provider-specific output drift, version compatibility, and hard-to-debug hardware differences.
- **Security/privacy:** local inference; pin provider/runtime versions and verify model checksums.
- **Complexity/priority/status:** Low for offline graph optimization, Medium for OpenVINO / P2 / **Test**; deployment **isolated benchmark**.
- **Acceptance:** compare embeddings on a fixed 1,000-image fixture, require cosine/distance drift below a documented tolerance, identical cluster decisions, and a measurable throughput/startup improvement.
- **Rollback/owner:** remove optimized model/provider settings and return to current CPU provider.

### P2 — Test next: AdaFace challenger benchmark

- **Project/module:** isolated adapter beside `insightface_backend.py`, using `split_face_pairs.py`, `face_match_benchmark.py`, and `face_cluster_purity_report.py`.
- **Complements/replaces:** challenger to current InsightFace ArcFace; no production switch in this phase.
- **Gap:** quality-adaptive representation for blurred, occluded, or unusually difficult media.
- **Benefits:** may improve low-quality FNMR and exemplar selection.
- **Drawbacks:** PyTorch/model footprint, alignment differences, and model-license ambiguity.
- **Security/privacy:** no online inference or credentials; pin the exact model artifact and record its license.
- **Complexity/priority/status:** High / P2 / **Test**; deployment **isolated pilot**.
- **Acceptance:** image-disjoint, per-identity and low-quality slices; require improved Wilson bounds and cluster purity without increasing unreadable/error rates or reducing recovery behavior.
- **Rollback/owner:** remove only the adapter and cache; current backend/database remains authoritative.

## Existing capabilities already covered

PicOrg already covers incremental hashing/embedding caches, face-only reconciliation, strict all-member similarity, durable assignment/marker ledgers, canonical baseline construction, bounded workers, UI rebuild status, and optional exact/FAISS benchmark tooling (`README.md`, `identity_evidence_store.py`, `face_cluster_unmatched.py`, `picorg_scheduler.py`). New recommendations must therefore be measurable improvements, not parallel orchestration stacks.

## Duplicates or rejected candidates

- **DeepFace/CompreFace:** rejected for production adoption because they duplicate backend/orchestration capabilities and add a service/API/credential boundary; see `docs/research/2026-09-16-face-model-library-fit.md`.
- **UniFace:** monitor/test only as a benchmark adapter; it overlaps existing detector/embedding/FAISS paths and does not justify replacing them without a controlled win.
- **MagFace/LVFace:** monitor or reject for now because official code/model maturity or license evidence is weaker than the current backend and AdaFace challenger.
- **Whole-library replacement:** reject. PicOrg's durable evidence and review safety model is project-specific and should not be displaced by a generic face API.

## Security and trust-boundary changes required before adoption

1. Keep MD/RD and sorted reference roots read-only; only hashes, embeddings, quality metadata, and provenance enter the local evidence DB.
2. Never expose raw embeddings or backup files through the LAN UI.
3. Pin every model/runtime/index revision and record checksums, license, provider, and parameters in the audit manifest.
4. Keep approximate indexes advisory; exact rerank plus existing safety gates remain authoritative.
5. Back up SQLite through the online backup path and preserve WAL state; do not copy only the main database file.

## Prioritized roadmap

1. Add SQLite version, online backup, and integrity health and surface it in scheduler/UI status (P1, low risk).
2. Add HDBSCAN risk metadata without changing cluster membership, then evaluate strict split rules (P1).
3. Run Faiss shortlist/exact-rerank recall benchmark on the current full gallery (P1).
4. Run OFIQ quality-gate pilot on a fixed, image-disjoint fixture (P1).
5. Benchmark offline ONNX optimization and, only on compatible hardware, OpenVINO (P2).
6. Run AdaFace as an isolated challenger; promote only after purity, error-bound, license, and recovery gates pass (P2).

## What remains unverified

- No candidate above has been installed or enabled by this research.
- OFIQ model/dependency terms still need a project-local license decision.
- Approximate-index recall and ONNX/OpenVINO parity have not been measured on the current full gallery.
- Current labels remain the limiting factor for production accuracy sign-off; public benchmark scores are not substitutes for PicOrg's image-disjoint held-out evidence.

## Change report

- **Entries added:** Faiss exact-rerank strategy, HDBSCAN risk diagnostics, OFIQ quality gate, ONNX offline/provider benchmark, SQLite version/online-backup/integrity health.
- **Entries enriched:** AdaFace challenger and existing model-library decisions with current official sources and bounded acceptance criteria.
- **Duplicates merged:** generic face API/orchestration replacements grouped under rejected/monitor candidates.
- **Conflicts found:** approximate retrieval and HDBSCAN can improve speed/diagnostics but must not replace exact/safety-authoritative decisions; SQLite WAL requires same-host handling and careful backup.
- **Deferred/rejected:** production model replacement, generic face API services, and any online identity-search dependency.
- **Evidence used:** project source paths listed above plus official Faiss, HDBSCAN, OFIQ, ONNX Runtime, SQLite, AdaFace, and CVLFace sources.
- **Residual risks:** label contamination, model-weight licensing, provider output drift, stale approximate indexes, and storage faults remain unresolved until the fixtures pass.

## Recommendation

Implement the SQLite version/backup/integrity health check first, then run one bounded benchmark covering HDBSCAN risk metadata, Faiss exact-rerank recall, and OFIQ quality gating; verify in under five minutes with a 1,000-image fixture before considering any production dependency change. Confidence: high.

## Open gaps

- The exact fixture path and current gallery generation must be selected at execution time because the active face database may be rebuilding or unavailable.
- A legal/operations owner still needs to approve any downloaded OFIQ/AdaFace model artifacts.
- No production apply or dependency installation was performed for this research.

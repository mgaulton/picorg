# AI face-matching and local-agent support for PicOrg

## Question

Which AI components and agent patterns can improve PicOrg's face-cluster accuracy and review efficiency without turning an LLM into an identity authority or uploading private media?

## Scope and current implementation

This review is limited to PicOrg and its directly coupled `photo_reorg` face-database workflow. The current implementation has:

- dlib/`face_recognition` extraction plus optional InsightFace/SCRFD/ArcFace extraction in `face_cluster_unmatched.py` (`220:334`, `503:566`).
- bounded representative/complete-link candidate clustering; it is explicitly a review grouping pass, not a calibrated identity verifier (`face_cluster_unmatched.py:226-232`).
- SQLite face records, FAISS benchmarks, held-out pair calibration, preflight checks, and safety gates (`run_ai_matching_experiments.sh:15-69`, `pipeline_safety_gate.py:1-80`).
- a custom Flask review UI with image-level decisions and identity-folder moves (`review_ui.py:708-828`).
- a provider-neutral online-evidence contract that is currently mock-only, report-only, hash-keyed, and forbids identity assignment (`online_evidence.py:1-8`, `56-70`, `90-130`).

The main accuracy problem is therefore calibration and gallery/cluster quality, not a missing conversational model. The current 0.90 similarity setting is a cosine threshold, not a literal 90% probability.

## Sources

| ID | URL/path | What it proved | Retrieved |
|---|---|---|---|
| S1 | https://github.com/deepinsight/insightface | InsightFace provides ArcFace recognition, SCRFD/RetinaFace detection, ONNX deployment, evaluation tooling, and active repository history; its model weights have separate non-commercial/research licensing notices. | 2026-09-02 |
| S2 | https://onnxruntime.ai/docs/performance/tune-performance/threading.html | ONNX Runtime supports configurable intra/inter-op pools, graph optimization, affinity, and spinning controls; defaults should be benchmarked rather than blindly increasing workers. | 2026-09-02 |
| S3 | https://onnxruntime.ai/docs/execution-providers/ | Execution Providers allow the same model to run on CPU, CUDA, TensorRT, OpenVINO and other accelerators, with provider fallback behavior. | 2026-09-02 |
| S4 | https://docs.ollama.com/capabilities/structured-outputs | Ollama vision models can emit JSON constrained by a JSON schema; the docs recommend Pydantic validation and low temperature for reliable structured output. | 2026-09-02 |
| S5 | https://docs.ollama.com/api/generate | Ollama's API accepts image inputs, a system prompt, a timeout-compatible HTTP interface, and a JSON-schema `format`. | 2026-09-02 |
| S6 | https://github.com/facebookresearch/faiss | FAISS is a library for efficient dense-vector search and clustering; it is already represented by PicOrg's FAISS benchmark scripts. | 2026-09-02 |
| S7 | https://qdrant.tech/documentation/search/filtering/ | Qdrant supports payload filtering and recommends payload indexes for performant filtered vector search. | 2026-09-02 |
| S8 | https://github.com/cleanlab/cleanlab | Cleanlab detects label errors, outliers, duplicates, and supports multi-annotator/active-learning workflows; it is relevant to auditing confirmed review labels, not to generating biometric identities. | 2026-09-02 |
| S9 | https://docs.voxel51.com/ | FiftyOne provides embedding visualizations and dataset curation, useful for diagnosing mixed clusters, but it would duplicate much of PicOrg's review UI. | 2026-09-02 |
| S10 | https://github.com/exadel-inc/CompreFace | CompreFace supplies a Docker/REST face-recognition service; it is a competing service boundary rather than a focused improvement to PicOrg's existing local backends. | 2026-09-02 |
| S11 | https://docs.nvidia.com/deeplearning/triton-inference-server/archives/triton-inference-server-2630/user-guide/docs/user_guide/batcher.html | Triton dynamic batching can increase stateless inference throughput, but requires a separate model-serving deployment and tuning. | 2026-09-02 |
| S12 | https://github.com/facebookresearch/dinov2 | DINOv2 produces general visual embeddings; these are useful for duplicate/near-duplicate and scene grouping, not as a replacement for face embeddings. | 2026-09-02 |
| S13 | https://docs.cvat.ai/docs/api_sdk/sdk/auto-annotation/ | CVAT can accept local auto-annotation functions and has a REST API, but introduces a second annotation UI/service. | 2026-09-02 |

## Findings

- [S1][repo] InsightFace is the strongest technical candidate for the face path because PicOrg already has an optional `insightface_backend.py` and model-specific cache. It should be benchmarked against the current dlib path on image-disjoint, human-confirmed pairs before becoming the default.
- [S2][S3][repo] Speed improvements should come from one shared ONNX session, bounded batching, and measured execution-provider/thread settings. Increasing Python worker count while each worker creates its own detector/session risks CPU, RAM, and disk contention.
- [S4][S5][repo] A local Ollama/OmniRoute agent can reliably return a validated quality report, but a language/vision model is not an independent biometric verifier. Repeated polling of one model is correlated evidence and must not raise a face-match score.
- [S6][repo] FAISS is already a lower-complexity candidate-retrieval layer. A vector database is not required for the current SQLite/FAISS scale unless metadata-filtered, multi-vector search becomes a measured bottleneck.
- [S8][repo] Human confirmations are valuable labels. They should be treated as a versioned, image-disjoint evaluation set and checked for label conflicts/outliers before being used as exemplars. Never train and evaluate on the same confirmed images.
- [S12][repo] Whole-image embeddings can help detect near-duplicate downloads and expose visually inconsistent members, but using them to identify a person would reintroduce scene/clothing bias and worsen mixed clusters.
- [S10][S11][S13] CompreFace, Triton, and CVAT are viable standalone products, but each adds a service or UI boundary that duplicates existing PicOrg capabilities. None is justified before a measured gap is demonstrated.

## Agent-verification design (OmniRoute/Ollama)

Use agents as a bounded *review-assistance* stage, never as an identity or move authority:

1. Deterministic pipeline computes detector count, face box, blur/occlusion/pose quality, dlib and/or ArcFace similarities, nearest-neighbor margin, cluster size, and cluster-purity statistics.
2. Only uncertain or high-impact items are sampled (multi-face images, low margin, large clusters, or disagreement between dlib and ArcFace). Do not send all 50k images through an agent.
3. Generate a local face crop or thumbnail and call Ollama through the local route. Use a Pydantic/JSON schema with fields such as `face_count`, `occlusion`, `pose`, `blur`, `crop_quality`, `possible_duplicate`, `abstain`, `reason_codes`, `model`, and `model_digest`. Do not include or request a person's name.
4. Require schema validation, `temperature=0`, a short timeout, retry limits, and an audit record keyed by content hash plus face index. Store the model/version and prompt schema so results are reproducible.
5. Treat agent disagreement, malformed output, timeout, or low quality as `abstain` and prioritize the item for human review. Agent consensus may downgrade/queue a candidate; it must never promote a candidate to an automatic move.
6. Only deterministic, held-out-calibrated thresholds plus explicit human confirmation can create an identity marker or move. Confirmations should feed the next benchmark after an image-disjoint split.

This gives OmniRoute a useful role—quality triage, duplicate/caption anomaly detection, and reviewer explanations—without making an LLM's subjective visual guess a biometric fact.

## Recommendations

### P0 — Critical

**Preserve the current fail-closed policy.** Keep agent output, online evidence, and face-cluster suggestions review-only. No provider, LLM, or reverse-image service should be able to write identity markers or move files. This is already enforced by `online_evidence.py` and the apply safety gates. Recommendation: **Adopt/keep** (low integration effort).

### P1 — High value

**InsightFace/SCRFD/ArcFace A/B path** — [S1]. Affects `insightface_backend.py`, `face_cluster_unmatched.py`, `backend_parity_benchmark.py`, and `run_ai_matching_experiments.sh`. Advantages: stronger detector/alignment options and one ONNX deployment path. Drawbacks: model-license restrictions, larger model/runtime, and no guarantee of better results on this gallery. Security: pin model files and verify hashes; do not auto-download unreviewed weights. Health: **Healthy/Acceptable** (active project, but model licensing needs explicit confirmation). Integration: **Medium**. Recommendation: **Adopt as a measured candidate**, not an unconditional replacement.

**Measured ONNX Runtime tuning** — [S2][S3]. Affects `insightface_backend.py` and rebuild settings. Benchmark CPU thread counts, shared-session batching, and available EPs; record the actual provider used and peak memory. Advantages: speed without changing match semantics. Drawbacks: hardware-specific tuning and possible silent CPU fallback. Integration: **Low/Medium**. Recommendation: **Adopt now as a benchmarked optimization**.

**Local structured agent sidecar** — [S4][S5]. Affects a future `agent_review.py`, `run_ai_matching_experiments.sh`, `review_ui.py`, and audit schema. Advantages: explainable quality triage and faster reviewer prioritization; private local processing; deterministic JSON contract. Drawbacks: correlated model errors, latency, and prompt/model drift. Security: keep OmniRoute/Ollama loopback-only, send hashes/crops only when explicitly enabled, enforce timeouts and size limits, and redact paths from prompts. Integration: **Medium**. Recommendation: **Test** on a small, human-reviewed sample; never use it for identity assignment.

**Human-label quality/evaluation pass with Cleanlab-style methods** — [S8]. Affects `build_face_pairs.py`, `split_face_pairs.py`, `production_readiness.py`, and `run_ai_matching_experiments.sh`. Use nearest-neighbor probabilities and confirmed decisions to rank suspected mislabeled exemplars and conflicts. Advantages: targets the current source of error (noisy exemplars and mixed clusters). Drawbacks: requires enough independent labels and an appropriate probabilistic adapter; Cleanlab does not understand face identity automatically. Integration: **Medium**. Recommendation: **Test**.

### P2 — Useful

**DINOv2 auxiliary duplicate/consistency signal** — [S12]. Affects preflight/duplicate reporting only. Use it to flag near-duplicate downloads, scene changes, or visually inconsistent cluster members; never use it as the face identity score. Integration: **Medium**. Recommendation: **Test**.

**FiftyOne or CVAT export for difficult batches** — [S9][S13]. Export only a bounded review batch when PicOrg's UI cannot efficiently inspect a cluster. Advantages: mature curation/annotation tools. Drawbacks: another service, authentication surface, and duplicated state. Integration: **Medium/High**. Recommendation: **Monitor**, not install by default.

### P3 — Experimental/watch

**Qdrant** — [S7]. It could replace the FAISS/SQLite retrieval layer if filtered multi-vector search or concurrent API access becomes a measured bottleneck. Current scale and existing FAISS benchmarks do not justify the service overhead. Recommendation: **Monitor**.

**Triton** — [S11]. Consider only if a GPU is available and profiling shows a sustained inference-throughput bottleneck. Dynamic batching is useful for a stateless model server, but deployment, model repository, metrics, and GPU scheduling are substantial new operations. Recommendation: **Monitor**.

### Rejected

**CompreFace as a replacement backend** — [S10]. It duplicates local recognition, adds Docker/REST latency and another database/service boundary, and does not solve PicOrg's calibration, exemplar contamination, or cluster-purity problems. Recommendation: **Reject for now**.

**Cloud/open-web facial-identification services**. They would require uploading biometric media, have uncertain coverage/terms, and provide leads rather than defensible ground truth. They are incompatible with PicOrg's local, human-reviewed, privacy-preserving design. Recommendation: **Reject**. Exact-image source lookup can remain a separately approved, non-biometric evidence workflow, with no automatic identity assignment.

## Final roadmap

### Implement now

- Add an agent-review contract and local-only feature flag, but run it only on uncertain/large clusters.
- Add ONNX provider/thread/session telemetry and benchmark records.
- Keep deterministic face scores and human decisions authoritative; keep image-disjoint benchmark gates.

### Test next

- Run dlib versus InsightFace on a newly expanded, image-disjoint set of at least 100 genuine and 100 impostor pairs.
- Test two local Ollama models or two independent prompt configurations for quality/occlusion triage; measure abstention rate and reviewer-time reduction, not “identity accuracy.”
- Test Cleanlab-style label-error ranking on confirmed markers and manually verify the top 50 conflicts.

### Future enhancements

- Strict all-member cluster validation or graph partitioning with a calibrated margin, rather than relying only on a bounded representative approximation.
- Incremental vector index updates keyed by SHA-256/model digest, with automatic re-clustering only for affected connected components.
- Optional DINOv2 duplicate/consistency diagnostics and a batch export to an annotation tool.

### Watch list

- Qdrant Edge if PicOrg needs embedded filtered vector search without a service.
- Triton if GPU throughput becomes the dominant cost.
- InsightFace Server only if its licensing and operational model are acceptable for this private deployment.

### Rejected

- CompreFace replacement deployment.
- Any cloud/open-web facial-identification API as an automatic evidence or assignment source.

## Open gaps

- The current gallery still needs a larger, independently reviewed and image-disjoint benchmark before any threshold can be called production-grade.
- Hardware/provider availability on `server6` was not assumed; run a measured ONNX provider check before selecting CUDA, TensorRT, OpenVINO, or CPU settings.
- No agent sidecar was implemented in this research pass; the next safe step is a report-only prototype with schema validation and no identity fields.

## Implementation status and verification

The report-only prototype is now implemented in `agent_review.py` and exposed
through `run_agent_review.sh`, the optional `RUN_AGENT_REVIEW=1` experiment
step, and TUI action **a**. It samples at most 50 uncertain items, supports
mock, local Ollama, and explicitly opted-in OpenAI-compatible endpoints,
rejects non-loopback endpoints by default, validates the quality-only schema,
and writes atomically. The opinion schema has no identity, decision, marker, or
move fields. Focused tests and the full repository suite pass.

## Recommended next action

Run the new action against a small, already-reviewed face-cluster audit and
compare agent abstention/quality flags with human review. Keep the provider at
`mock` until a local Ollama model is explicitly selected and its latency,
abstention rate, and reviewer-time reduction are measured.

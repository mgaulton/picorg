# PicOrg scoped architecture and upstream research

## Question

Which changes and maintained upstream projects can improve PicOrg's accuracy, speed, reliability, security, and review workflow without replacing its deterministic registry-first design?

## Scope and current boundary

PicOrg is the owner of intake, priority-aware deduplication, filename/alias matching, read-only reference coalescing, audit ledgers, face-group review, and the LAN review UI. The directly related projects are:

- `/opt/photo_reorg`: the current default face-database builder and legacy SQLite consumer.
- `/opt/metadaily`: authoritative identity/alias registry and profile evidence producer.
- `/opt/redditdaily`: protected download store and Reddit identity/source metadata.
- `/opt/move_downloads_remote.sh`: intake mover invoked by the PicOrg pipeline.

PicOrg must continue to treat Metadaily/Redditdaily media as read-only reference evidence, keep identity registry ownership outside PicOrg, and require review/benchmark gates before moves. This report does not recommend unrelated server or home-lab changes.

## Existing implementation baseline

| Area | Current implementation | Strength | Remaining gap |
|---|---|---|---|
| Intake/dedupe | `run_picorg.sh`, `dedupe_priority.py`, SHA-256/fingerprint cache | Priority sources and incremental rescans are explicit and auditable | Near-duplicate and decoder coverage are separate from exact dedupe |
| Identity matching | `picorg_sorter.py`, `project_registry.json`, Metadaily/Redditdaily loaders | Registry-first, generic-token blocklist, dry-run/apply separation | Registry freshness and alias provenance need run manifests |
| Face extraction | `face_cluster_unmatched.py`, dlib and optional InsightFace; bounded workers/checkpoints | Resumable caches, preflight, multi-face handling, model-specific caches | Current held-out calibration fails production FNMR/CI gates; extraction/decode failures remain material |
| Face database | PicOrg native SQLite plus `/opt/photo_reorg` compatibility path | Atomic replacement, schema validation, cache reuse | Default backend remains legacy photo_reorg pending parity evidence |
| Review | `review_ui.py`, reconciled audit, image-level decisions, append-only ledger | Identity drill-down and confirmed moves are auditable | Flask dev server is unsafe; CIDR-aware remote authentication must match the trusted LAN boundary |
| Operations | `run_picorg_menu.sh`, `picorg_tui.py`, `screen` wrappers, timing JSONL | Long runs can resume and report throughput/ETA | No first-class metrics endpoint or production WSGI launcher |
| Quality/security | preflight, benchmark safety gate, Bandit/pytest CI, pip-audit history | Fail-closed move gates and corruption handling | Dependency ranges are not a locked, hash-verified runtime set |

## Sources

| ID | Source | Relevant evidence | Retrieved |
|---|---|---|---|
| R1 | [`README.md`](../../README.md), [`runweb.sh`](../../runweb.sh), [`review_ui.py`](../../review_ui.py), [`face_cluster_unmatched.py`](../../face_cluster_unmatched.py) | Current PicOrg interfaces, caches, review mutations, LAN binding, and face backends | 2026-08-31 |
| R2 | [`/opt/photo_reorg`](file:///opt/photo_reorg), [`/opt/metadaily`](file:///opt/metadaily), [`/opt/redditdaily`](file:///opt/redditdaily) | Directly related face builder, identity registry, and protected source metadata | 2026-08-31 |
| S1 | [Flask deployment documentation](https://flask.palletsprojects.com/en/latest/tutorial/deploy/) | Flask's built-in server is for development, not efficient/stable/secure production serving | 2026-08-31 |
| S2 | [Waitress documentation](https://docs.pylonsproject.org/projects/waitress/en/latest/) and [repository](https://github.com/Pylons/waitress) | Mature pure-Python WSGI server; 3.0.2 supports Python 3.9+ and validates proxy headers | 2026-08-31 |
| S3 | [FAISS repository](https://github.com/facebookresearch/faiss) and [changelog](https://github.com/facebookresearch/faiss/blob/main/CHANGELOG.md) | Maintained dense-vector similarity search/clustering library; 1.14.x/1.15 development is active, MIT | 2026-08-31 |
| S4 | [scikit-learn HDBSCAN](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.HDBSCAN.html) | Variable-density clustering, explicit noise, `min_cluster_size`, and leaf/EOM selection | 2026-08-31 |
| S5 | [Czkawka](https://github.com/qarmin/czkawka), [core instructions](https://github.com/qarmin/czkawka/blob/master/instructions/Instruction_Core.md), [releases](https://github.com/qarmin/czkawka/releases) | CLI/core for exact duplicates, similar images, broken files, bad extensions, deterministic cached hashes; v11 released Feb 2026 and project remains active | 2026-08-31 |
| S6 | [pyvips](https://github.com/libvips/pyvips) and [libvips](https://github.com/libvips/libvips) | Threaded, streaming, low-memory image decoding; broad HEIC/AVIF/WebP/TIFF support | 2026-08-31 |
| S7 | [InsightFace](https://github.com/deepinsight/insightface) and [model licensing](https://github.com/deepinsight/insightface/blob/master/server/LICENSING.md) | Strong detector/ArcFace stack already integrated by PicOrg/photo_reorg; code MIT but public model packages are generally non-commercial academic research only | 2026-08-31 |
| S8 | [DeepFace](https://github.com/serengil/deepface) and [releases](https://github.com/serengil/deepface/releases) | Broad model/detector comparison wrapper, active releases, but inherited model licenses and many confidence/threshold combinations | 2026-08-31 |
| S9 | [AdaFace](https://arxiv.org/abs/2204.00964) and [MagFace paper/code](https://github.com/IrvingMeng/MagFace) | Quality-aware embedding research candidates; must be evaluated on PicOrg labels, not assumed superior | 2026-08-31 |
| S10 | [pip-audit](https://github.com/pypa/pip-audit) | PyPA vulnerability scanner usable in CI and with JSON/SARIF output | 2026-08-31 |
| S11 | [Prometheus Python client](https://github.com/prometheus/client_python) | Lightweight counters/histograms and `/metrics` integration | 2026-08-31 |
| S12 | [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) | Standard traces/metrics for a future multi-process or multi-service workflow | 2026-08-31 |

## Candidate health and fit

The health labels below separate upstream maintenance from PicOrg fit. A healthy project can still be a poor choice for this workflow.

| Candidate | Release/activity and community signal | Runtime/deployment fit | License/security notes | Health |
|---|---|---|---|---|
| Waitress | v3.0.2 (Nov 2024); mature Pylons project with maintained documentation | Python 3.9–3.13 class runtime; in-process WSGI, no container required | ZPL-2.1; trusted-proxy settings must be configured correctly; no extra network privilege | **Acceptable** |
| FAISS | v1.14.3 (Jun 2026), 40.3k stars/4.4k forks, active issues/PRs | Python/NumPy bindings; CPU first, optional GPU; local sidecar file | MIT; index files are sensitive because they encode biometric vectors | **Healthy** |
| scikit-learn HDBSCAN | First-party implementation in current scikit-learn docs, version 1.3+ | Pure Python/compiled wheels; no service | BSD-3-Clause; ordinary local dependency | **Healthy** |
| Czkawka core/CLI | v11.0.0 (Feb 2026) and commits through Jun 2026 | Rust binary or Rust core adapter; no network service | MIT for core/CLI; Krokiet GUI is GPL-3.0; project states no telemetry | **Healthy** |
| pyvips/libvips | Active upstream ecosystem; native libvips plus Python binding | Requires system libvips and format loaders; local process | LGPL-2.1+ library / MIT-style Python binding; native decoder supply-chain needs pinning | **Acceptable** |
| pip-audit | v2.10.1 (Jun 2026) | CI/venv command; no runtime service | Apache-2.0; reads dependency metadata and vulnerability feeds, so pin/report feed provenance | **Healthy** |
| Prometheus Python client | v0.25.0 (Apr 2026) | In-process `/metrics`; scrape endpoint only | Apache-2.0; avoid path/identity labels to prevent sensitive-cardinality leaks | **Healthy** |
| OpenTelemetry Python | 1.42.1/0.63b1 (May 2026); active CNCF ecosystem | Adds exporters/collector configuration | Apache-2.0; exporters can exfiltrate paths or biometric metadata unless restricted | **Healthy, overkill for now** |
| InsightFace | Active code and model/server documentation | Already used through ONNX Runtime in PicOrg/photo_reorg | MIT code is separate from generally non-commercial research-only public model packages | **Acceptable technically; licensing risk** |
| DeepFace | Active release line (0.0.100 in 2026) | Heavy TensorFlow/Keras stack, local-only A/B harness | MIT wrapper, but each backend/model carries its own terms; confidence semantics need calibration | **Acceptable; poor production fit** |
| AdaFace/MagFace | Research implementations; MagFace repository has no stable release line and describes abridged code | Requires an isolated model benchmark and new cache namespace | Weight/dataset terms vary; no automatic deployment without provenance | **Questionable/experimental** |

The release dates, stars/forks, and activity claims above are snapshots, not guarantees; re-check them before adding a dependency. For face models, license the exact weight files, not just the repository source.

## Findings

1. **Accuracy is currently limited by evidence quality, not a missing model.** PicOrg's held-out reports fail the FNMR/95% CI gate, and the gallery has decode/missing/no-face coverage. Changing a threshold or adding an ensemble before label-purity cleanup would risk false moves. [R1]
2. **The UI has a production-serving boundary.** `runweb.sh` binds `0.0.0.0`, while `review_ui.py` previously used Flask's development server. The default trusted network is the operator's `192.168.2.0/24` LAN plus loopback/link-local; non-LAN clients are token-gated, with an explicit force-auth override available. Flask explicitly warns against the built-in server for production. [R1][S1]
3. **Reference search and face verification should remain separate.** A vector index can make candidate retrieval fast, but acceptance still needs calibrated distance, margin, identity consistency, quality, and multi-face rules. [R1][S3]
4. **Clustering needs explicit noise and purity controls.** HDBSCAN can expose variable-density clusters and noise, but known-identity references must remain partitioned and cluster membership must never be treated as identity proof. [R1][S4]
5. **Czkawka is useful as an evidence-only preflight tool, not as a replacement for priority dedupe.** Its core has cached exact/similar-image and broken-file scans, but similar-image thresholds can miss formats or small files; PicOrg must retain its protected-root priority policy. [R1][S5]
6. **Decoder work may improve throughput and coverage, but must be measured.** libvips/pyvips is a plausible bounded-image preflight/thumb path; face_recognition still needs NumPy-compatible pixels, so this is not a drop-in face extractor replacement. [R1][S6]
7. **InsightFace licensing is a release blocker for redistribution or commercial use.** The code license does not automatically grant the same rights to downloaded model weights. Keep the existing explicit license gate. [S7]
8. **Operational telemetry is now sufficient for a first benchmark, but not for fleet-level observability.** PicOrg already records timing JSONL, cache stats, preflight, and audit manifests. Prometheus is a small next step; OpenTelemetry is only justified if multiple services/jobs need correlated traces. [R1][S11][S12]
9. **Dependency reproducibility is weaker than the code's safety gates.** `pyproject.toml` uses ranges and optional requirements without a committed lock/hashes. pip-audit should be paired with a constraints/lock artifact and model checksum manifest. [R1][S10]

## Candidate evaluations

### P0 — Production WSGI and mutation boundary

| Existing implementation → candidate | Benefits | Drawbacks | Integration effort | Recommendation |
|---|---|---|---|---|
| `runweb.sh` → [Waitress](https://github.com/Pylons/waitress) | Removes Flask dev-server warning; pure Python; bounded threads; minimal change to `review_ui:app` | One process does not solve long-running face jobs; must document trusted proxy/bind behavior | Low | **Adopt** |
| Optional `PICORG_UI_AUTH` → CIDR-aware policy in `review_ui.py` | Keep trusted-LAN review and moves convenient while requiring token/Bearer for non-LAN clients; `PICORG_UI_AUTH=1` forces auth everywhere | Default is intentionally one LAN CIDR; configure `PICORG_UI_TRUSTED_CIDRS` for another topology and do not trust forwarded headers implicitly | Low | **Adopt** |
| Optional `flask-limiter` → mutation rate limits | Limits accidental/replayed move requests; supports memory/Redis backends | Extra dependency and state; not a substitute for auth or CSRF design | Low/Medium | **Test** |

Security note: embeddings, face boxes, paths, and review decisions are sensitive. Never expose them to the public Internet or send images to an online service by default. If auth is intentionally disabled, bind to a trusted LAN/VPN and keep all write routes disabled.

### P1 — Faster retrieval without weakening verification

| Existing implementation → candidate | Benefits | Drawbacks | Integration effort | Recommendation |
|---|---|---|---|---|
| SQLite/brute-force reference comparisons → [FAISS](https://github.com/facebookresearch/faiss) `IndexFlatIP`/`IndexFlatL2` | Batched exact top-k search first; later approximate indexes if profiling proves necessary; mature MIT project | New index manifest, atomic swap, model/dimension/normalization compatibility, no inherent accuracy gain | Medium | **Test** |
| Native cache/SQLite → FAISS sidecar | Preserve SQLite as source of truth and use FAISS only for candidate IDs; exact rerank and existing gates remain authoritative | Two artifacts to validate and back up | Medium | **Adopt only after benchmark** |

Acceptance test: compare current retrieval and FAISS on identical embeddings, require identical top-k candidates and zero change in held-out FMR/FNMR before enabling approximate search.

### P1 — Better data quality and clustering

| Existing implementation → candidate | Benefits | Drawbacks | Integration effort | Recommendation |
|---|---|---|---|---|
| `media_preflight.py`/`dedupe_priority.py` → [Czkawka CLI/core](https://github.com/qarmin/czkawka) read-only import | Finds broken files, bad extensions, near-duplicates, and cached image signatures; active and no telemetry | Rust binary/core integration, output adapter, similar-image false positives, Krokiet GUI is GPL-3 while core/CLI are MIT | Medium | **Test** |
| Pillow decode/thumb path → [pyvips/libvips](https://github.com/libvips/pyvips) bounded adapter | Lower memory and potentially higher decode throughput for thumbnails/preflight; HEIC/AVIF coverage | Native package/ABI, conversion cost before dlib/ONNX, another decoder to validate | Medium | **Test** |
| Current representative/complete-link face clusters → [scikit-learn HDBSCAN](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.HDBSCAN.html) | Noise label and variable-density/leaf clusters may reduce giant mixed clusters | Hyperparameter sensitivity; can fragment genuine identities; scikit-learn version requirement | Low/Medium | **Test, review-only** |

All clustering candidates must run identity-partitioned, preserve image-level face assignments, and report purity/coverage. No candidate may enable automatic moves by itself.

### P1 — Supply-chain and release discipline

| Existing implementation → candidate | Benefits | Drawbacks | Integration effort | Recommendation |
|---|---|---|---|---|
| Range-only requirements → [pip-audit](https://github.com/pypa/pip-audit) plus committed constraints/hashes | Finds known Python vulnerabilities; JSON/SARIF CI output; reproducible installs | Lock regeneration and occasional dependency pin maintenance | Low | **Adopt** |
| Model download by environment → signed/checksummed model manifest | Detects drift or replacement of InsightFace/ONNX artifacts; supports rollback | Requires recording model licenses and checksums per backend | Low | **Adopt** |

### P2 — Observability and testing

| Existing implementation → candidate | Benefits | Drawbacks | Integration effort | Recommendation |
|---|---|---|---|---|
| Timing JSONL/log parsing → [Prometheus client](https://github.com/prometheus/client_python) | `/metrics` for extraction rate, cache hit rate, decode failures, queue age, and UI request errors | Requires cardinality discipline; another optional dependency | Low | **Adopt after metric names are fixed** |
| Stage logs → [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) | Correlates intake, rebuild, matching, and UI actions across services | More configuration/storage than PicOrg currently needs; logs support is less mature than traces/metrics | Medium/High | **Monitor** |
| Hand-written invariants → [Hypothesis](https://github.com/HypothesisWorks/hypothesis) | Finds normalization, priority-dedupe, path-rewrite, and cluster-partition edge cases | Test-only dependency and learning curve | Low | **Adopt for core invariants** |

### P3 — Model experiments only

| Existing implementation → candidate | Benefits | Drawbacks | Integration effort | Recommendation |
|---|---|---|---|---|
| dlib/InsightFace comparison → AdaFace/MagFace research implementations | Quality-adaptive embeddings may help difficult/low-quality faces | Weights/datasets/licensing vary; no PicOrg evidence yet; model migration invalidates caches | High | **Test in isolated benchmark** |
| Local adapters → [DeepFace](https://github.com/serengil/deepface) | Convenient model/detector matrix for offline A/B testing | Heavy dependency surface, inherited model licenses, confidence semantics are too broad for a move gate | Medium/High | **Monitor; not production oracle** |

## Directly related project recommendations

1. **Keep Metadaily authoritative for canonical identity and aliases.** PicOrg should snapshot registry version/hash in each run manifest, not copy ownership into a second mutable registry. This directly prevents stale names such as old aliases from becoming face identities. [R1][R2]
2. **Keep Redditdaily and Metadaily media read-only in face reference coalescing.** Record skipped EIO/unreadable paths and source-root health in the audit; do not silently treat a partial source tree as a complete gallery. [R1][R2]
3. **Treat photo_reorg as a compatibility backend while PicOrg native SQLite is validated.** Run backend-parity benchmarks on the same coalesced manifest, then promote only the backend with equivalent or better held-out FNMR/FMR and complete schema validation. [R1][R2]

## Final roadmap

### Implement Now

1. **Implemented:** `review_ui:app` now launches under Waitress from `runweb.sh`; the Flask server remains an explicit development-only fallback.
2. **Implemented:** `192.168.2.0/24`/loopback/link-local clients are unrestricted; non-LAN clients require a token, with `PICORG_UI_AUTH=1` available to force auth everywhere.
3. Add a run manifest containing registry hash, source-root health, backend/model/detector IDs, model checksum/license, cache versions, and benchmark ID.
4. Commit a constraints/lock and hash-verified install path; run pip-audit in CI and before rebuilds.
5. Keep the existing benchmark gate unchanged: no automatic moves while held-out FNMR/FMR confidence bounds fail.

### Test Next

1. FAISS exact top-k sidecar with exact reranking and a rollback-able index manifest.
2. Czkawka CLI/core read-only preflight import for near-duplicates and broken files.
3. pyvips bounded thumbnail/preflight benchmark against Pillow on representative formats.
4. HDBSCAN leaf/noise clustering benchmark on known-identity partitions and hard mixed clusters.
5. AdaFace/MagFace and InsightFace/dlib A/B runs on image-disjoint, reviewer-purified pairs; include model-license metadata.
6. Hypothesis tests for alias normalization, protected roots, duplicate priority, and cluster purity.

### Future Enhancements

- Versioned, atomic vector index rebuild/swap with a SQLite source-of-truth and rollback.
- Per-identity quality-calibrated thresholds and gallery selection after enough confirmed positives.
- A read-only FiftyOne-style embedding exploration export only if the current UI cannot expose hard examples efficiently; keep all moves in PicOrg.
- Deferred shared face-evidence integration with Metadaily/Redditdaily, following `docs/IDENTITY_FACE_EVIDENCE_INTEGRATION.md`; do not merge ownership prematurely.

### Watch List

- InsightFace model licensing and officially licensed weight options.
- FAISS 1.15 memory-mapped/index improvements and Python packaging compatibility.
- Czkawka/Krokiet core changes, especially format support and deterministic grouping.
- OpenTelemetry/structlog adoption only if PicOrg becomes a multi-service job system.
- UniFace/AdaFace/MagFace releases and reproducible, clearly licensed weights.

### Rejected

- **CompreFace** as a new service: useful REST shape, but its last tagged release is old relative to this project and it duplicates local inference while adding a container/API boundary.
- **DeepFace as production confidence authority:** model/detector/license matrix and confidence semantics add calibration risk; keep it offline-only if used.
- **Cloud reverse-image/face APIs:** privacy, terms, rate limits, unverifiable evidence provenance, and no safe automatic identity authority.
- **Perceptual hash as identity evidence:** useful for duplicate/near-duplicate discovery, never proof that two faces are the same person.
- **Replacing PicOrg with a generic photo manager:** would discard the registry-first, protected-root, and audit/move controls that are specific to this workflow.

## Open gaps

- The current benchmark labels are still not clean/large enough for production threshold claims; the immediate accuracy project is review-label purification and hard-negative sampling.
- No local profile yet proves retrieval is the dominant runtime cost; measure decode, embedding, and candidate-search stages separately before adding FAISS.
- Model-weight licenses and intended use must be recorded for every backend; code licenses alone are insufficient.
- Waitress and default mutation-auth changes are implemented in `review_ui.py`, `runweb.sh`, and `requirements-review.txt`; the full test suite and shell checks pass.

## Five-minute verification after the first implementation slice

1. Start the UI with the new launcher and verify `GET /healthz` returns 200 on the LAN bind. A bounded localhost smoke test passed on 2026-08-31.
2. Verify a non-LAN request without a token returns 401 while a trusted-LAN request remains unrestricted; regression coverage now enforces this.
3. Run `pip-audit` and confirm the lock/hash manifest plus model checksum are recorded in the run manifest.
4. Run the existing held-out benchmark and confirm no automatic-move gate changes.

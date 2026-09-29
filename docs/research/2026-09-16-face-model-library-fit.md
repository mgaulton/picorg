# Face-model and library fit for PicOrg

## Question

Which current face-recognition models or libraries can improve PicOrg's identity matching and face-only clustering without sacrificing its local, read-only-reference and review-first safety boundaries?

## Sources

| URL/path | What it proved | Retrieved |
|---|---|---|
| https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md | `buffalo_l` is the default server-side pack, uses SCRFD + ArcFace, publishes benchmark tables, and model weights are non-commercial research only | 2026-09-16 |
| https://www.insightface.ai/guides/choose-face-recognition-model-and-evaluate | InsightFace positions `buffalo_l` for server-side 1:1/1:N workloads and `buffalo_s` for latency-constrained devices | 2026-09-16 |
| https://github.com/mk-minchul/AdaFace | AdaFace adapts the margin to image quality; current repository points new work to CVLFace, has 37 commits/98 issues, and supplies R18/R50/R100 weights | 2026-09-16 |
| https://github.com/IrvingMeng/MagFace | MagFace provides quality-aware embeddings; official repository is Apache-2.0 but has no published releases and is an abridged research codebase | 2026-09-16 |
| https://github.com/yakhyo/uniface | UniFace unifies SCRFD/RetinaFace detection, ArcFace/AdaFace recognition, quality assessment, and FAISS; MIT code but pretrained-weight licenses vary | 2026-09-16 |
| https://github.com/bytedance/LVFace | LVFace is a recent ICCV 2025 research model with ONNX inference and strong reported IJB-C results, but its README contains conflicting code/model licensing language and only 14 commits/3 issues | 2026-09-16 |
| https://github.com/serengil/deepface | DeepFace is an MIT orchestration library wrapping many backends, but explicitly says external model licenses are inherited | 2026-09-16 |
| https://github.com/exadel-inc/CompreFace | CompreFace is an Apache-2.0 Docker REST service with role management and CPU/GPU deployment, based on FaceNet/InsightFace | 2026-09-16 |
| https://github.com/timesler/facenet-pytorch | FaceNet/Inception-ResNet-v1 and MTCNN are MIT and easy to benchmark, but represent an older alternative rather than a clear accuracy upgrade | 2026-09-16 |
| `face_cluster_unmatched.py`, `face_cluster_unmatched.py:630-704` | PicOrg already supports dlib and optional InsightFace extraction, persistent cache/checkpoints, cosine floors, strict all-member clustering, and backend selection | 2026-09-16 |
| `face_group_unmatched.py`, `run_existing_face_db.sh`, `face_embedding_store.py` | PicOrg now uses quality-weighted gallery matching, adaptive dlib retries, fingerprinted incremental matching, and a WAL SQLite embedding store | 2026-09-16 |

## Findings

- [S1][S2] PicOrg's current InsightFace option (`buffalo_l`) is already the strongest low-integration baseline: SCRFD detection plus ArcFace embeddings, with a model pack intended for server-side 1:N search. The project should not replace it based on public benchmark scores alone; PicOrg's own held-out labels and cluster-purity reports are the acceptance authority.
- [S3] AdaFace is the most relevant accuracy challenger because its training objective explicitly incorporates image quality, which matches PicOrg's blurry, occluded, mixed-quality downloads. The official implementation is PyTorch and expects aligned 112×112 BGR input, so it is not a drop-in replacement for the existing ONNX backend.
- [S4] MagFace is useful primarily as a quality signal or research comparator. Its official repository is Apache-2.0, but it is explicitly abridged, has no releases, and distributes weights through external links; integration and supply-chain verification are higher risk than AdaFace.
- [S5] UniFace is a practical adapter candidate: it exposes both ArcFace and AdaFace, quality scoring (eDifFIQA), multiple detectors, and FAISS. It overlaps heavily with PicOrg's existing InsightFace/dlib/cache code, so adopting the whole library would add dependency and model-license surface without proving better identity accuracy.
- [S6] LVFace is a promising 2025 research challenger and reports strong IJB-C numbers, but its small repository and contradictory license wording (MIT code versus research-only model language) make it unsuitable for direct adoption before a pinned isolated benchmark and legal review.
- [S7][S8] DeepFace and CompreFace solve API/orchestration/deployment problems, not PicOrg's primary accuracy bottleneck. DeepFace would duplicate backend selection; CompreFace would introduce a separate Docker service, database, API boundary, and credential surface. Neither should replace the local pipeline for this project.
- [S9] FaceNet-PyTorch is a useful reproducible fallback benchmark, not a likely production improvement over ArcFace/SCRFD. It adds PyTorch/MTCNN dependencies and a second alignment/preprocessing path.
- [S10] PicOrg's current clustering controls are more important than adding a model: `--min-similarity 0.90` plus `--strict-all-members` prevents representative-chain merges, while `face_embedding_store.py` and fingerprinted incremental matching make repeated evaluation affordable.

## Recommendation

Adopt no new model immediately. Keep InsightFace `buffalo_l`/SCRFD as the primary backend and run one isolated AdaFace challenger benchmark using the existing image-disjoint confirmed-pair fixture. Compare per-identity FMR/FNMR, 95% Wilson bounds, low-quality subsets, multi-face rejection, cluster purity, CPU throughput, and memory. Only promote AdaFace if it improves the held-out error bounds without reducing purity or operational recoverability. Confidence: high.

Suggested bounded fixture:

```text
existing confirmed image decisions
→ split_face_pairs.py (image-disjoint train/evaluation split)
→ current InsightFace baseline
→ AdaFace R50 or R100 isolated adapter
→ face_match_benchmark.py + face_cluster_purity_report.py
```

No production credentials, online reverse-image services, or reference-root writes are needed. Roll back by deleting only the isolated adapter/cache and retaining the current database/gallery.

## Open gaps

- Public LFW/IJB-C scores do not predict PicOrg's domain accuracy; the current confirmed labels remain too contaminated/small for production sign-off.
- The exact license terms of each downloaded model weight must be recorded and accepted separately from repository code licenses.
- AdaFace and LVFace have not yet been converted to PicOrg's backend interface or benchmarked on the full image-disjoint fixture.
- No claim is made that any model can identify a person from an arbitrary web image; model outputs remain probabilistic review evidence.

## Isolated benchmark update (2026-09-16)

The temporary coalesced fixture had been cleaned or moved, so its original
paths produced zero coverage. A deterministic suffix relink against
`/mnt/elements16/@mixedpics_sorted` recovered 35 unique images and a balanced
160-pair subset (80 genuine, 80 impostor). On that small, local sample:

| Backend | Coverage | Errors at selected point | Runtime | Score ranges |
|---|---:|---:|---:|---|
| InsightFace `buffalo_l`/SCRFD/ArcFace | 100% | 0/160 (Wilson upper FMR/FNMR 4.58%) | 13.2 s | not exported by legacy report |
| UniFace AdaFace | 100% | 0/160 at cosine threshold 0.30 | 5.65 s | genuine 0.319–0.649; impostor −0.154–0.095 |

This is a feasibility result only: the pairs are derived from the same local
review corpus, and the initial AdaFace report did not emit Wilson intervals.
It did not justify changing the production backend. The experiment launcher
now accepts `UNIFACE_SEARCH_ROOT` so moved fixtures can be relinked
reproducibly, and the report now includes Wilson intervals.

The full experiment suite subsequently evaluated the existing 21,528-pair
image-disjoint manifest. AdaFace relinked 208 moved images, scored 21,115
pairs (98.08% coverage), and at cosine threshold 0.30 measured FMR 0.0581%
and FNMR 0.1822%. The corresponding 95% Wilson intervals were FMR
0.0325–0.1041% and FNMR 0.0709–0.4677%. The existing dlib calibration on the same manifest measured FMR
0.0985% and FNMR 27.5647% at its selected point. This is a strong challenger
signal, not a release decision: the relinked gallery and label provenance must
be audited, low-quality and per-identity slices must be checked, and AdaFace
model-weight licensing must be approved before any production use.

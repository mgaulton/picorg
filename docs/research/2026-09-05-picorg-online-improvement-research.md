# PicOrg online improvement research — 2026-09-05

## Scope

This review is limited to PicOrg and its direct face-matching dependencies:

- PicOrg intake, priority dedupe, name/alias audit, review UI, move ledger, and JSON marker files.
- `photo_reorg` face extraction/database rebuilds.
- PicOrg's native SQLite embedding backend and face grouping scripts.

It does not recommend unrelated server, home-lab, or general AI infrastructure.

## Current architecture and gaps

PicOrg currently uses name/alias matching before face review, `photo_reorg` or
native SQLite face embeddings, a quality/diversity reference gallery, face-only
review clusters, and a Flask/Waitress UI. Confirmed assignments are now
persisted as SHA-verified face markers and follow moved canonical files.

The main remaining accuracy risks are:

1. A similarity score is being treated as an intuitive percentage rather than a
   calibrated model-specific score.
2. Transitive cluster growth can join vaguely similar faces into large mixed
   groups.
3. Low-quality, multi-face, and outlier images can enter an identity gallery.
4. Full database rebuilds are expensive even when only a small number of files
   changed.
5. Review labels are useful training/evaluation data but are not yet treated as
   explicit must-link/cannot-link constraints and active-learning priorities.

## Candidate comparison

### 1. OFIQ / SER-FIQ / MagFace quality scoring — P1, Test then Adopt

**Projects:** [BSI OFIQ](https://github.com/BSI-OFIQ/OFIQ-Project),
[SER-FIQ](https://github.com/pterhoer/FaceImageQuality),
[MagFace](https://github.com/IrvingMeng/MagFace)

OFIQ is the reference implementation for ISO/IEC 29794-5 face-image quality.
SER-FIQ estimates quality from embedding robustness, and MagFace incorporates
quality information into face representations. These are directly relevant to
PicOrg's existing `face_quality` and exemplar selection logic.

**Integration:** add a quality record beside each extracted face in
`face_database.sqlite3`/the photo_reorg export and expose quality reasons in
`select_reference_gallery.py` and the UI. Keep the current detector/recognizer
unchanged initially; use the new score as an exemplar and review gate.

**Benefits:** fewer blurry, occluded, profile, tiny-face, and multi-face
exemplars; better gallery purity.

**Drawbacks:** another model, CPU cost, and score calibration work. OFIQ
bindings and model files need platform/license verification.

**Recommendation:** benchmark OFIQ and SER-FIQ on the confirmed/rejected local
set. Adopt only if they improve held-out precision or reduce cluster impurity.

### 2. Calibrated score policy — P0, Adopt

**References:** [NIST FRTE](https://pages.nist.gov/frvt/html/frvt11.html),
[NIST SP 500-343](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.500-343.pdf),
[scikit-learn calibration](https://scikit-learn.org/stable/modules/calibration.html)

NIST evaluates thresholds using false match rate (FMR) and false non-match rate
(FNMR). InsightFace documentation also describes cosine similarity as a raw
score, not a probability.

**Integration:** extend `run_accuracy_benchmark.sh` and the existing face-pair
reports to calculate genuine/impostor distributions, score histograms, FMR,
FNMR, precision, recall, and confidence intervals. Store a versioned threshold
profile in the run manifest. Use separate thresholds for automatic assignment,
review candidate, and unknown.

**Recommendation:** do this before changing model or threshold. Never display
“90% match” unless it is a calibrated probability backed by held-out data.

### 3. Constrained clustering — P0, Adopt

**References:** [scikit-learn agglomerative clustering](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.AgglomerativeClustering.html),
[HDBSCAN parameter selection](https://hdbscan.readthedocs.io/en/latest/parameter_selection.html),
[constrained face clustering paper](https://biometrics.cse.msu.edu/Publications/Face/ShiOttoJain_FaceClusteringRepresentationAndPairwiseConstraints_TIFS2018.pdf)

Replace unrestricted transitive clustering with a similarity graph plus explicit
constraints:

- must-link: confirmed images for the same canonical identity;
- cannot-link: rejected assignments, different confirmed identities, and faces
  from an image that contains distinct people;
- complete-linkage or maximum-radius checks so every member remains close to
  the cluster medoid;
- split clusters when internal similarity falls below the calibrated floor;
- preserve noise/unknown nodes instead of forcing them into a cluster.

**Integration:** `face_cluster_unmatched.py`, `reconcile_review_clusters.py`,
and the UI cluster actions. Add cluster purity, diameter, margin, and split
reason to the audit JSON.

HDBSCAN's `leaf` selection is useful for fine-grained review clusters, but its
output must still pass PicOrg's pairwise purity rules. It must not assign an
identity by itself.

### 4. FAISS or HNSW index — P1, Adopt when retrieval is the bottleneck

**Projects:** [FAISS](https://github.com/facebookresearch/faiss),
[hnswlib](https://github.com/nmslib/hnswlib)

FAISS supports exact and approximate L2/dot-product search, cosine search on
normalized vectors, clustering, and optional GPU indexes. HNSWlib is a smaller
approximate-nearest-neighbor library with cosine distance.

**Integration:** add a disposable index generated from the authoritative SQLite
rows. Keep SQLite plus JSON manifests as the source of truth; rebuild the index
when the model fingerprint or embedding set changes. Use exact search first so
accuracy is unchanged, then benchmark HNSW/FAISS approximate search.

**Recommendation:** FAISS is the stronger maintained choice for future scale;
it is not urgent for roughly 50k images unless matching becomes the runtime
bottleneck.

### 5. InsightFace Server — P2, Test only

**Project:** [InsightFace Server](https://github.com/deepinsight/insightface/tree/master/server)

The current server release documents SCRFD detection, ArcFace embeddings,
L2-normalized cosine search, strict enrollment modes, model-bound collections,
SQLite durability, health checks, and a typed API.

**Benefits:** provides a maintained reference implementation for enrollment,
quality checks, model/version binding, and exact person search.

**Drawbacks:** high integration cost, Docker/GPU requirements for best speed,
and separate licensing obligations for model weights. It would duplicate much
of the current `photo_reorg` backend.

**Recommendation:** use as an isolated parity benchmark, not an immediate
replacement.

### 6. FiftyOne — P2, Test as an analysis companion

**Project:** [FiftyOne](https://github.com/voxel51/fiftyone)

FiftyOne provides embedding visualization, similarity search, dataset-quality
views, and interactive inspection. Its App could expose UMAP-style embedding
maps and outlier browsing for difficult PicOrg clusters.

**Integration:** export a read-only dataset from PicOrg audit JSON, embeddings,
markers, and thumbnails. Do not replace the PicOrg move/confirmation UI or
allow FiftyOne to mutate canonical folders.

**Recommendation:** useful for model debugging and cluster investigation, not
required for the production path.

### 7. Cleanlab — P2, Test for review prioritization

**Project:** [Cleanlab](https://github.com/cleanlab/cleanlab)

Cleanlab can rank outliers, duplicate-like samples, label errors, class overlap,
and active-learning candidates from embeddings and review labels.

**Integration:** feed it only local PicOrg embeddings and confirmed/rejected
labels. Export a bounded “needs attention” queue; never let it move files or
override the identity registry.

**Drawbacks:** it is a general data-quality framework, not a face-recognition
threshold engine; additional dependencies and score interpretation are needed.

### 8. Near-duplicate and decoder tooling — P2, Test

**Projects:** [imagededup](https://github.com/idealo/imagededup),
[OpenImageIO](https://github.com/AcademySoftwareFoundation/OpenImageIO),
[OFIQ](https://github.com/BSI-OFIQ/OFIQ-Project)

Use perceptual hashes and visual duplicate scores as a separate evidence layer,
never as identity evidence. OpenImageIO/libvips-style bounded decoding can
improve preflight and thumbnail coverage for unusual formats, but it is not a
drop-in face extractor replacement.

PicOrg's protected-root, exact-hash priority dedupe remains authoritative.

### 9. OCR and semantic image embeddings — P2/P3, Limited use

**Projects:** [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR),
[DINOv2](https://github.com/facebookresearch/dinov2),
[OpenCLIP](https://github.com/mlfoundations/open_clip)

PaddleOCR can improve text/title extraction for low-confidence name matching.
DINOv2/OpenCLIP embeddings can group near-duplicate sets, scenes, outfits, or
download batches.

They must remain secondary signals. General visual embeddings and OCR do not
prove that two faces are the same person.

### 10. Ollama/VLM review assistant — P3, Test only

Use a local vision-language model only to summarize review context, detect
obvious multi-person images, explain why a cluster is risky, or suggest which
items to inspect next. It must not identify a person, create a canonical
identity, or authorize a move. All decisions remain face-score- and
human-confirmation-gated.

## Features worth adding without new dependencies

1. **Identity enrollment state:** `candidate`, `confirmed`, `trusted`, `retired`.
2. **Exemplar provenance:** marker SHA, model ID, quality scores, source family,
   confirmation event, and last validation timestamp.
3. **Cluster risk panel:** score range, second-best margin, purity, diameter,
   multi-face count, unreadable count, and outlier thumbnails.
4. **Split/merge/split-again UI:** split is conservative and creates a new
   review event; merge requires explicit identity confirmation.
5. **Active-learning queue:** prioritize low-margin matches, cluster outliers,
   conflicting names/faces, and new identities with no confirmed exemplar.
6. **Incremental extraction:** hash/fingerprint new files, append embeddings,
   and rebuild only affected identity indexes; reserve full rebuilds for model
   or source-root changes.
7. **Run manifest locking:** bind audit, DB, gallery, model, thresholds, and
   marker snapshot together so UI cannot mix generations.
8. **Model parity benchmark:** run dlib/photo_reorg, InsightFace, and any test
   model on the same labeled pairs; compare accuracy, decode coverage, and
   throughput before adoption.
9. **Review export/import:** export confirmed/rejected pair labels as a stable
   JSONL dataset for calibration and regression tests.
10. **Unknown preservation:** never create a named identity from a filename-only
    cluster or a single weak face match.

## Implement now

- Calibrated FMR/FNMR threshold report and versioned threshold profile.
- Complete-linkage/max-radius cluster purity enforcement.
- Explicit must-link/cannot-link constraints from UI decisions.
- Model/detector/preprocessing fingerprint in SQLite metadata and gallery/run
  manifests.
- Quality/risk fields in the UI and active-learning review queue.
- Incremental cache/index invalidation based on file SHA and model fingerprint.

## Test next

- OFIQ and SER-FIQ as exemplar-quality gates.
- FAISS exact index as a retrieval accelerator.
- HDBSCAN leaf clustering plus PicOrg purity checks.
- FiftyOne read-only embedding/outlier inspection.
- InsightFace Server parity benchmark.
- PaddleOCR only for opt-in low-confidence title evidence.

## Future enhancements

- AdaFace/MagFace model benchmark and possible model migration.
- Incremental identity-specific index updates.
- Local VLM-assisted review explanations and queue ranking.
- GPU FAISS or InsightFace Server if corpus size or throughput justifies it.

## Reject or defer

- Automatic public reverse-image/face-search services: privacy, rate-limit,
  provenance, false-positive, and terms-of-service risks.
- Replacing the current stack with DeepFace/CompreFace solely for convenience;
  they duplicate existing capabilities and would require new calibration.
- Using CLIP/DINO/VLM similarity as identity proof.
- Training a custom face model before PicOrg has a clean, sufficiently large,
  versioned confirmed-pair dataset.

## Source and maintenance notes

The cited projects are active upstream references, but model weights and
licenses must be checked separately from source-code licenses. In particular,
InsightFace public model packages may have more restrictive terms than the
code. Pin model files by checksum and record the exact runtime/model version in
each gallery manifest.

# Face-match accuracy for generic unmatched clusters

## Question

How should picorg identify generic filename clusters using face matching while minimizing false identity assignments?

## Sources

| URL/path | What it proved | Retrieved |
|---|---|---|
| https://pages.nist.gov/frvt/html/frvt11.html | Face verification errors depend strongly on image quality; operating thresholds must be evaluated against false-match and false-nonmatch tradeoffs. | 2026-07-25 |
| https://pages.nist.gov/frvt/html/frvt1N.html | One-to-many identification is a distinct open-set problem; rejecting unknowns is part of the operating design. | 2026-07-25 |
| https://github.com/ageitgey/face_recognition | The library's tolerance controls strictness; lower tolerance reduces false matches but increases misses. | 2026-07-25 |
| https://dlib.net/face_clustering.py.html | Face embeddings can cluster images, including images containing multiple people; cluster-level reasoning is useful when names are absent. | 2026-07-25 |
| https://www.dlib.net/python/ | dlib descriptors are 128-dimensional vectors; `num_jitters` averages repeated perturbed descriptors and can improve descriptor stability at additional cost. | 2026-07-25 |
| https://arxiv.org/abs/1801.07698 | ArcFace improves embedding separability through an additive angular margin; it is a candidate for an A/B model upgrade, not a drop-in replacement for dlib embeddings. | 2026-07-25 |
| `/opt/picorg/face_group_unmatched.py` | Current prototype now quality-gates faces, defers multi-face images by default, supports jitter, and exposes threshold/margin controls; defaults remain 0.48/0.04 pending calibration. | 2026-07-25 |
| `/opt/photo_reorg/data/high_accuracy_faces.db` | Current reference DB contains 82 people and 188 encodings; this is a small and uneven gallery for broad identification. | 2026-07-25 |

## Findings

- [S1] A single global distance threshold is not an accuracy guarantee. It must be selected from local genuine/impostor distributions at the desired false-match rate. NIST explicitly treats image quality and operating error rates as central evaluation factors. [S1]
- [S2] Generic clusters require open-set rejection: “best candidate” is not sufficient because the correct identity may not be in the gallery. [S2]
- [S3] The current prototype is not yet safe for automatic assignment: it defers multi-face images by default, but still applies one threshold to every person and has no learned quality calibration. [S7]
- [S4] The current gallery averages about 2.3 encodings per person. More importantly, a count alone is not enough: each identity needs clean, varied, non-duplicate references across pose, lighting, age, expression, and image source. [S8]
- [S5] Quality gating should happen before matching. Reject or defer tiny, blurred, heavily occluded, profile-only, or multi-face images unless the selected face and cluster evidence are stable. Poor photography is a known source of false nonmatches. [S1]
- [S6] Cluster consensus is safer than independent image assignment: require several independent images in a generic cluster to support the same identity, a clear best-vs-second margin, and no conflicting high-confidence identity. [S4][S2]
- [S7] `num_jitters` and better face localization can improve descriptor stability, but they do not repair bad enrollment data or an uncalibrated threshold. [S5]
- [S8] ArcFace/modern embedding models may outperform the current dlib space, but embeddings cannot be mixed. Any upgrade needs a held-out A/B benchmark and a rebuilt reference gallery. [S6]

## Recommendation

High confidence: implement a staged matcher before any automatic moves:

1. Build a labeled calibration set from known source identities: genuine pairs, hard impostor pairs, and generic-cluster samples. Keep identities and clusters split between calibration and evaluation.
2. Improve enrollment first: deduplicate references, remove outliers, retain quality-diverse exemplars, and target at least 10–20 clean references for identities with enough source material.
3. Extract every detected face, score face quality, and defer multi-face/low-quality images unless one face clearly dominates.
4. Use per-identity exemplar scoring plus a global open-set threshold and a candidate margin. Calibrate the threshold to a very low false-match rate; do not use `.48/.04` as a permanent policy.
5. Aggregate at the generic-cluster level. Auto-suggest only when multiple independent images agree on one identity; keep ambiguous or conflicting clusters in review.
6. Record match distance, margin, quality, face count, model version, reference IDs, and calibration version in the audit. Only a later explicit apply step may move files.

Verify in five minutes with a small benchmark: run the matcher on 20 known positives, 20 hard negatives, and 20 generic-cluster images; report false matches, false nonmatches, abstentions, and cluster-consensus accuracy separately.

## Open gaps

- The current DB does not yet have a reliable per-person quality/coverage report or a held-out labeled evaluation set.
- The current prototype now performs basic face-size quality gating, multi-face deferral, jitter control, and cluster-consensus reporting, but it does not yet have per-person thresholds or a learned quality model.
- The current OpenCV detector warning indicates one optional detector model is unavailable; the active dlib face path still runs, but detector fallback coverage should be benchmarked.
- It is not yet verified whether a modern ArcFace/InsightFace model is available in the deployment environment; a model change would require a separate benchmark and reference rebuild.

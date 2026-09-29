# Online face evidence and autonomous mapping

## Question

How can PicOrg use online face/reverse-image services and autonomous evidence mapping to improve clustering without turning uncertain web results into automatic identity assignments?

## Sources

| ID | URL/path | What it proved | Retrieved |
|---|---|---|---|
| S1 | `https://docs.aws.amazon.com/rekognition/latest/APIReference/API_SearchFacesByImage.html` | AWS SearchFacesByImage searches an enrolled collection, returns similarity values, supports quality filtering, and searches the largest face unless faces are cropped first. | 2026-09-01 |
| S2 | `https://learn.microsoft.com/en-us/azure/ai-services/cognitive-services-limited-access` | Azure Face Identify/Verify and celebrity recognition are Limited Access features requiring eligibility/registration. | 2026-09-01 |
| S3 | `https://docs.cloud.google.com/vision/docs/detecting-faces` | Google Cloud Vision detects multiple faces and attributes but does not support specific-individual facial recognition. | 2026-09-01 |
| S4 | `https://learn.microsoft.com/en-us/bing/search-apis/bing-visual-search/overview` | Bing Visual Search provides image insights/similar images through a subscription API; it is not an enrolled-identity verifier. | 2026-09-01 |
| S5 | `https://aistudio.yandex.ru/en/docs/search-api/operations/search-images-by-pic` | Yandex Search API supports image-to-image search returning results from its image index. | 2026-09-01 |
| S6 | `https://services.tineye.com/TinEyeAPI` | TinEye offers a programmatic reverse-image search API for multiple image formats. | 2026-09-01 |
| S7 | `https://github.com/deepinsight/insightface` | InsightFace provides local detection, alignment, recognition, and an optional self-hosted server/API; model licensing must be checked. | 2026-09-01 |
| S8 | `https://github.com/exadel-inc/CompreFace` | CompreFace is a self-hosted Docker REST system for recognition, verification, detection, landmarks, and head pose. | 2026-09-01 |
| S9 | `https://commons.wikimedia.org/w/api.php` | Wikimedia Commons exposes a public API and media metadata suitable for explicitly licensed/public reference sampling. | 2026-09-01 |
| R1 | `/opt/picorg/face_cluster_unmatched.py` | PicOrg performs local face extraction, caching, geometry quality filtering, and face-only clustering. | 2026-09-01 |
| R2 | `/opt/picorg/face_group_unmatched.py` | PicOrg performs quality-weighted identity ranking, top-two margin gating, and adaptive-jitter retries. | 2026-09-01 |
| R3 | `/opt/picorg/review_ui.py`, `/opt/picorg/review_decision_ledger.jsonl` | Confirmed image decisions and provenance are persisted locally for review and calibration. | 2026-09-01 |
| R4 | `/opt/picorg/reverse_search_queue.py`, `/opt/picorg/reverse_search_contact_sheet.py` | PicOrg already has a report-only reverse-search queue/contact-sheet path that can host provider adapters. | 2026-09-01 |
| R5 | `/opt/picorg/identity_face_markers.json`, `/opt/picorg/project_registry.json` | Identity markers and the MD/RD-derived registry are local controlled evidence; they should remain the authority for canonical names. | 2026-09-01 |

## Findings

- [S1] Cloud face-search scores are provider-specific similarity values, not interchangeable with PicOrg's dlib/ArcFace distances. They require an enrolled provider collection and external image disclosure.
- [S1][R1] AWS searches the largest face by default; PicOrg must crop and hash each detected face before any multi-face online query. A whole group image must never be sent as a single identity query.
- [S2] Azure Identify/Verify is not a frictionless free option: access is restricted and requires registration. It should not be a production dependency for this private workflow.
- [S3] Google Cloud Vision is useful for face-count/landmark metadata but cannot identify a specific person, so it cannot improve identity matching directly.
- [S4][S5][S6] Bing, Yandex, and TinEye are reverse-image evidence sources. They can return visually similar images or web occurrences, but their results are not proof of identity and should be treated as candidate URLs.
- [S7][S8][R1][R2] Local/self-hosted inference is the safest path for private media. InsightFace is the strongest existing candidate in this repository; CompreFace is a possible REST comparison service but would duplicate local face infrastructure.
- [S9][R5] Wikimedia is appropriate for public/licensed reference sampling when the operator explicitly chooses a canonical identity and the license/provenance is stored. It is not a general people-search database.
- [R3][R4] PicOrg already has the correct safety boundary for online evidence: queue candidates, preserve URLs and timestamps, and require a human confirmation before markers or moves change.

## Recommended autonomous mapping architecture

Use a staged evidence graph, not an autonomous name assignment loop:

1. **Local face stage:** detect and crop every face; store crop SHA-256, geometry, quality, model ID, and embedding locally.
2. **Local candidate stage:** retrieve identity candidates with the calibrated gallery, top-two margin, and face-only clustering rules.
3. **Online evidence stage (opt-in):** send only an operator-approved face crop or a public URL to one provider at a time. Never upload the private original gallery automatically.
4. **Evidence normalization:** store provider, request ID, query-crop hash, result URL, returned score/type, retrieval time, HTTP status, and terms/license notes.
5. **Graph mapping:** represent `image -> face -> candidate identity -> evidence URL` edges with provenance and confidence. A web result can support a review queue but cannot create a confirmed edge.
6. **Human confirmation:** only the review UI can promote an edge to `confirmed`, write a face marker, and make it eligible for future exemplars or moves.
7. **Active learning:** rebuild identity prototypes and held-out genuine/impostor pairs from confirmed edges; keep rejected/contradictory evidence as hard negatives.

## Provider disposition

| Provider | Use in PicOrg | Privacy/cost/accuracy concern | Decision |
|---|---|---|---|
| InsightFace local/server | Consistent local detector/embedding/search backend | Model-license confirmation and full-gallery rebuild required | **Test, then possibly adopt** |
| CompreFace | Isolated REST A/B test only | Duplicates local stack; compatibility and model calibration unknown | **Test** |
| AWS Rekognition | Manual evidence adapter for consented/public crops | Paid cloud upload; largest-face default; provider score not locally calibrated | **Reject as default; optional evidence** |
| Azure Face | Manual one-to-one/one-to-many experiment only | Limited Access and registration; cloud disclosure | **Reject as default** |
| Google Cloud Vision | Face count/landmarks only | Cannot identify specific individuals | **Use only for metadata if needed** |
| Bing/Yandex/TinEye | Reverse-image candidate URLs | Hosted disclosure, terms/rate limits, no identity guarantee | **Manual/opt-in evidence** |
| Wikimedia Commons | Public/licensed reference sampling | Coverage is incomplete; identity metadata still needs verification | **Adopt as a curated source** |

## Implementation recommendations

### P0 — Privacy boundary

Add an explicit `ONLINE_EVIDENCE_ENABLED=0` default and require an operator action for every upload. Reject private/local paths unless the user confirms the crop is authorized for external processing. Store only hashes and returned metadata by default; make raw provider responses opt-in and retention-limited.

### P1 — Evidence adapter contract

Implement a provider-neutral adapter behind the existing reverse-search queue. Required fields: `provider`, `query_sha256`, `face_box`, `requested_at`, `result_url`, `result_type`, `provider_score`, `http_status`, `license_note`, and `provenance`. Adapters must be report-only and idempotent by `(provider, query_sha256)`.

### P1 — Multi-face correctness

Use one face crop per query and maintain `face_index` in the audit. Do not let a provider that searches only the largest face label an entire multi-person image.

### P1 — Evidence-aware review UI

Show web evidence as corroboration beside local face scores, never as an auto-assign button. Require an explicit “confirm identity from local face + evidence” action before writing markers.

### P1 — Autonomous research loop

Run scheduled, bounded jobs that select only high-value unresolved clusters, query at most a configured number per provider, deduplicate URLs, and stop on rate-limit/error thresholds. The job should create a review queue and summary report, not modify identities.

### P2 — Graph export

Export the local evidence graph as JSONL or SQLite tables for auditability. Keep canonical identity names sourced from the MD/RD registry and store aliases separately.

## One recommended next action

Implement the provider-neutral, opt-in evidence adapter around `reverse_search_queue.py` using a dry-run/mock provider first. Verify in five minutes by submitting one synthetic/local test crop, confirming that only a hashed query and candidate metadata are written, and that no identity marker or file move occurs.

## Open gaps

- No official provider examined here offers a free, unrestricted, web-wide “identify this person” API.
- Provider terms, retention, regional availability, and pricing need review before enabling any cloud adapter.
- Online results have not been benchmarked against PicOrg's confirmed image-disjoint pairs.
- Model-license suitability for InsightFace/CompreFace has not been approved for this deployment.
- The active face rebuild must finish before benchmarking a new embedding backend against the resulting gallery.

## Implementation status

`online_evidence.py` and `tests/test_online_evidence.py` now implement the
recommended safe first step. The default/mock provider is deliberately
no-network and produces hash-keyed, report-only records. No cloud provider
adapter is enabled; adding one requires a separate privacy and calibration
review plus an explicit operator flag.

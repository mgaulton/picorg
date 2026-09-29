# Face accuracy and evidence methods review

## Question

Which local models, sampling/evaluation methods, retrieval tools, and optional online evidence sources can improve PicOrg accuracy without making identity assignment unsafe or privacy-hostile?

## Sources

| URL/path | What it proved | Retrieved |
|---|---|---|
| `insightface_backend.py`, `picorg_face_database.py` | PicOrg currently supports dlib extraction, an opt-in InsightFace/ArcFace backend, durable caches, and SQLite face records. | 2026-08-26 |
| `.cache/picorg/face_database.sqlite3` | Current native database validates structurally at 488 identities and 29,106 faces; this is not an accuracy guarantee. | 2026-08-26 |
| `/tmp/picorg-heldout50-backend-parity.json` | Current image-disjoint comparison has 120 genuine and 1,258 impostor pairs; both tested backends fail the production FNMR confidence gate. | 2026-08-26 |
| [ArcFace CVPR 2019](https://openaccess.thecvf.com/content_CVPR_2019/html/Deng_ArcFace_Additive_Angular_Margin_Loss_for_Deep_Face_Recognition_CVPR_2019_paper.html) | Additive angular-margin embeddings are designed to improve inter-class separation; this supports ArcFace as a candidate verifier, not automatic acceptance without local calibration. | 2026-08-26 |
| [InsightFace model zoo](https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md) | The released model packs, including `buffalo_l`, are non-commercial research-only; the page reports model-pack benchmark results and must govern deployment licensing. | 2026-08-26 |
| [AdaFace CVPR 2022](https://openaccess.thecvf.com/content/CVPR2022/html/Kim_AdaFace_Quality_Adaptive_Margin_for_Face_Recognition_CVPR_2022_paper.html) | Face-quality-aware margins improve recognition on difficult imagery; quality should be a first-class match feature and poor images should be deferred. | 2026-08-26 |
| [RetinaFace](https://arxiv.org/abs/1905.00641) | Landmark-assisted dense face localization improves hard-face detection and can improve downstream verification. | 2026-08-26 |
| [FAISS](https://github.com/facebookresearch/faiss) | FAISS provides efficient vector search/clustering, including normalized-vector cosine search and indexes that scale beyond RAM. | 2026-08-26 |
| [DINOv2](https://github.com/facebookresearch/dinov2) | DINOv2 provides Apache-2.0 general visual features for image retrieval; it is suitable as a non-face/duplicate/context signal, not as a person-identity verifier. | 2026-08-26 |
| [HDBSCAN documentation](https://github.com/scikit-learn-contrib/hdbscan/blob/master/docs/how_hdbscan_works.rst) | HDBSCAN exposes noise/probability behavior and is appropriate for variable-density candidate discovery, but clustering output is not identity truth. | 2026-08-26 |
| [LightGlue](https://openaccess.thecvf.com/content/ICCV2023/papers/Lindenberger_LightGlue_Local_Feature_Matching_at_Light_Speed_ICCV_2023_paper.pdf) | Adaptive local-feature matching is useful for exact/near-duplicate evidence and can reject hard matches; it complements face embeddings. | 2026-08-26 |
| [TinEye API](https://services.tineye.com/TinEyeAPI) | Programmatic reverse-image search exists, but it is a paid hosted API; uploads/URLs are external disclosures and results are web-occurrence evidence, not person identity. | 2026-08-26 |
| [Wikimedia API](https://www.mediawiki.org/wiki/API:REST_API) | Wikimedia exposes an official API and openly licensed/public media metadata, making it a safer optional source for public-figure reference images when licensing is recorded. | 2026-08-26 |
| [NIST FRVT demographics](https://www.nist.gov/publications/face-recognition-vendor-test-part-3-demographic-effects) | Face-recognition error rates vary by algorithm, image quality, and demographic group; aggregate accuracy alone is insufficient for sign-off. | 2026-08-26 |

## Findings

- [S1][S2][R1] PicOrg already has the right separation for a safe experiment: dlib remains the default, InsightFace is opt-in, and the face database is replaceable. Do not switch the production path to InsightFace until licensing and a defensible local benchmark pass.
- [S3][S4] The largest accuracy opportunity is quality-aware enrollment and matching: retain multiple high-quality poses per identity, down-weight or defer low-quality/occluded/small faces, and use detector landmarks before embedding. A single gallery centroid is not enough for identities with pose, age, lighting, or styling variation.
- [S5][R1] The current linear representative clustering is a candidate-grouping heuristic. A FAISS cosine index can make nearest-neighbor retrieval fast, while a separate verifier applies quality, margin, mutual-nearest-neighbor, and identity-consistency gates. Retrieval speed must not change the acceptance threshold.
- [S6][S7] Add a non-face visual index and density-aware noise detector for captions, memes, screenshots, and repeated scene backgrounds. These signals should prioritize review or detect duplicates; they must never override a face contradiction.
- [S8][R1] Exact/near-duplicate matching should be a separate early branch using perceptual hashes plus local-feature verification. It can identify the same source image after crop/resize/watermark changes, but it is not evidence that two different photos depict the same person.
- [S9][R1] Online reverse-image lookup is useful only as an evidence queue. It can confirm that a file appears on a known public profile or source page, but it cannot safely establish identity from a search result. Hosted search must be opt-in, rate-limited, logged, and limited to a redacted/authorized sample.
- [S10] The current 120-genuine/1,258-impostor benchmark is large enough to expose a serious problem but not clean enough to tune thresholds: the confirmed cluster ledger contains mixed people and stale paths. Image-level confirmations remapped by SHA should become the evaluation source, with cluster-level labels excluded unless every member was individually confirmed.
- [R2] After switching to the image-level ledger, the held-out set reached 1,108 genuine and 4,887 impostor pairs and FNMR fell to 21.7% at the selected operating point. This validates label cleanup as a high-impact intervention, but the result still fails the production gate. The weakest confirmed identities were `thenewgingercoug`, `underthescrubs`, and `phoebeisginger`, which need exemplar/outlier review.
- [S11][R1] Optional public-image sampling should use only URLs already present in the MD/RD identity registry or explicitly supplied by the operator. Store URL, retrieval time, HTTP status, license/terms, SHA-256, and identity provenance; never silently scrape or upload the private gallery.
- [S10] Do not use inferred age, gender, race, or other sensitive attributes as identity features. Use quality/pose/face-count metadata only to explain uncertainty and route review.

## Recommendation

**High confidence:** implement a three-tier local verifier before adding online sampling: (1) quality-gated face detection and multiple enrollment exemplars, (2) FAISS-backed nearest-neighbor retrieval with mutual-neighbor and identity-consistency checks, and (3) a calibrated verifier trained/evaluated only on individually confirmed, image-disjoint pairs. Keep all uncertain, multi-face, low-quality, and contradictory cases in the review UI. Verify by producing at least 100 genuine and 100 impostor pairs with ≤1% Wilson 95% upper bounds for both FMR and FNMR.

Online sampling should be a later, explicit evidence adapter—not part of automatic assignment. Start with operator-provided MD/RD URLs and Wikimedia public/licensed pages; treat TinEye or another hosted reverse-image API as manual/paid evidence only, with a hard “never upload private media” policy.

## Open gaps

- The current confirmed ledger still needs member-level purity review; the measured false-nonmatch rates cannot be interpreted as model quality until labels are repaired.
- No independent, image-disjoint benchmark currently passes the production confidence gates.
- GPU availability, disk latency, and the actual face-quality distribution have not been profiled on the full gallery.
- Online provider terms, rate limits, and legal/privacy requirements must be reviewed before any adapter is enabled.

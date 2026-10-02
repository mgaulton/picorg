# Why many face clusters have generic labels

## Question

Why does the cluster review show generic names or clusters that appear only loosely related to identity markers?

## Sources

| Source | Evidence used |
| --- | --- |
| `.cache/picorg/current-run.json` | The pointer initially selected a generic-only face report and omitted identity matches. It now points to the matching identity-first face report, its candidate artifact, and regenerated reconciliation. |
| `.cache/picorg/audits/20260916T114034Z.reorg.face-clusters.json` | `source=face_embedding_cluster`, 36,065 candidates, 19,410 embedded, 1,734 clusters; 10,321 no-face, 2,514 multi-face deferred, 2,326 low-quality, 4 errors. Threshold is 0.52. |
| `.cache/picorg/audits/20260916T114034Z.identity-candidates.json` and `.face-clusters.identity-first.json` | 49,495 candidate rows; 1,493 matched images across 103 historical identity labels. The identity-first report combines these with 19,410 generic face results. |
| `.cache/picorg/audits/20260916T114034Z.reorg.reconciled.json` | 20,903 rows in 1,837 clusters; only 29 rows carried `expected_identity`; every row was marked `name+face`. |
| `reconcile_review_clusters.py:58-120` | Reconciliation builds face clusters first, gathers title context, and labels clusters. Previously any title made the method `name+face` and the first title was appended to a face label. |
| `run_existing_face_db.sh:148-174` | The known-identity matching stage runs `incremental_face_match.py` and then refreshes the web review data. |

## Findings

- **Confirmed — the pointer selected the wrong face report.** An identity-first report and candidate artifact already existed, but the pointer selected the generic-only face report and left `identity_matches` empty. This hid 1,493 existing face matches from review.
- **Confirmed — local alias reconciliation leaves historical matches unlinked.** Only 12 of the 103 historical face-match labels resolve through the repo's `manual` project-registry overlay. The shared registry could not be read through the host command bridge during this pass, so the full current canonical alias coverage remains unknown. Unresolved labels are not promoted to identity assignments automatically.
- **Confirmed — reconciliation mislabeled weak title context as identity linkage.** Every row in the current reconciled report said `name+face`, although only 29 rows had an expected identity. The previous rule treated any title overlap, including download names and generic `FB IMG` text, as identity evidence and could append the first title to the cluster label.
- **Confirmed — a large share of candidates cannot contribute to face clusters in this report.** The report excludes or defers images with no detected face, multiple faces, low quality, or decoding errors. Its preflight also recorded corrupt, missing, thumbnail, unsupported, and skipped inputs.
- **Likely — the perceived generic or loose relation is a combination of absent identity matching and conservative/weak face grouping.** The report uses a legacy distance threshold and does not provide a fresh known-identity matching result. Earlier cluster-accuracy review documented fragmentation and order sensitivity; lowering thresholds without image-disjoint validation risks merging different people.

## Change made

Reconciliation now accepts a face match only when its identity resolves through the current registry; it distinguishes `face-identity`, `name+face`, and `face-only`. Arbitrary file and folder titles no longer label identities. Tests cover untrusted titles, name-resolved identities, and face-resolved identities. I regenerated an immutable report from the identity-first face artifact and republished the pointer with its candidate artifact: 1,837 clusters and 20,903 rows, including 113 registry-resolved face-identity rows and 29 name-plus-face rows. The remaining historical face-match labels do not resolve through the current registry and remain unconfirmed. Systemd now reports a new review process; its readiness response has not yet been verified.

## Recommendation

Let the current face-reference append finish. Then run the existing face database matching workflow against the current shared registry so it creates a fresh identity-match artifact and refreshes the review audit. Compare the regenerated pointer and per-identity match counts before treating old labels as confirmed identities. Keep the current similarity threshold until a held-out, image-disjoint evaluation supports a change.

## Open gaps

- The append process was still live at 21:20 UTC (7h34m elapsed). The latest readable progress file was 12,297/96,786 at 20:51 UTC, so matches are historical and not a post-append refresh.
- The shared registry is 471,078 bytes, but reading its JSON through the host command bridge stalled; its contents were not changed.
- Review unresolved historical labels before adding any as aliases or canonical identities.
- No quality-diverse sample of the generic clusters was visually inspected in this investigation; confidence in the likely cause is high, but the exact face-clustering precision still needs evaluation.

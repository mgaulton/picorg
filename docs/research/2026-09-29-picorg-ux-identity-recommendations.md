# PicOrg UX, workflow, and identity-handling recommendations

## Question

Which changes would most reduce review effort and identity mistakes while preserving PicOrg's current queue, provenance, and source-registry safeguards?

## Sources

| Source | What it establishes | Retrieved |
|---|---|---|
| `review_ui.py:417-431`, `review_ui.py:3510-3514` | Existing cluster purity flags and the image-review warning for risky clusters. | 2026-09-29 |
| `review_ui.py:1981-2088`, `review_ui.py:3521-3544` | Identity groups combine catalog, review, assignment, marker, and cluster evidence; disk scanning is opt-in; the identity list supports active, registry, and all scopes. | 2026-09-29 |
| `review_ui.py:2241-2305`, `review_ui.py:3597-3603`, `identity_evidence_store.py:504-560` | Image assignments can be durably queued with a file hash and provenance; queue review is currently under Settings. | 2026-09-29 |
| `review_ui.py:2860-2870` | Identity search matches aliases, recently used identities are persisted in browser storage, and identity creation/assignment are separate actions. | 2026-09-29 |
| `docs/IDENTITY_PROMOTION_WORKFLOW.md:10-27`, `:64-90` | Shared registry, local overlay, provisional identities, markers, and organized media have separate authorities and promotion rules. | 2026-09-29 |
| `docs/IDENTITY_EVIDENCE_STORE.md:27-51` | Queue state is durable; source-specific canonical folder and database updates are future work and must use stable source keys and retryable adapters. | 2026-09-29 |
| `docs/IDENTITY_FACE_EVIDENCE_INTEGRATION.md:29-90` | Human confirmation is required; marker evidence is local, model namespaces must remain separate, and biometric evidence should stay out of the LAN UI by default. | 2026-09-29 |

## Findings

- **[S1] Cluster size is only one purity signal.** PicOrg flags clusters of at least 100 items, absent expected identities or face labels, multiple face labels, and mixed source families. A smaller cluster can still mix people; a large one can still be clean. The UI should communicate measured cohesion and outliers instead of making size the user's proxy for accuracy.
- **[S2] Identity counts describe recorded evidence, not necessarily the full sorted folder.** The groups endpoint joins registry and review identities with paths from decisions, markers, queues, and clusters. It deliberately avoids a sorted-tree scan by default. Label these counts as reviewed/known evidence and show scan coverage separately.
- **[S3] The durable assignment queue is a strong workflow foundation but is tucked under Settings.** Queue records already include expected SHA-256, identity, and provenance, while the Settings panel exposes pending, applying, error, and conflict items. Review work should be centered on that durable state rather than requiring the operator to remember which view contains the next item.
- **[S4] Identity families and promotion stages carry important meaning.** A shared canonical identity, local manual identity, and provisional review label have different authority. The UI already distinguishes families and aliases, but the workflow can make the source and promotion status visible at selection and confirmation time.
- **[S5] Similar people across separate clusters need a cross-cluster comparison path.** The current cluster flags detect several risks within one cluster; they do not provide a ranked way to compare one cluster's representative images with other clusters or confirmed identity examples. Keep suggestions review-only so cross-cluster similarity never silently merges identities.
- **[S6] File moves and source database updates are separate lifecycle stages.** The docs explicitly say MetaDaily/RedditDaily source databases and canonical folders are not updated by PicOrg. Before source-aware moves are enabled, the operator needs a preview that states the source family, target identity, file operation, and metadata rows that would change.

## Recommendations

| Priority | Recommendation | User impact | Effort |
|---|---|---|---|
| 1 | **Make a resumable review queue the primary work surface.** Show pending, queued, conflict, and completed counts; provide “open next”; retain the current cluster/image and batch progress; keep undo visible. Use the existing durable assignment IDs and hashes. | Fewer lost-place and repeat-review errors. | Medium |
| 2 | **Add an identity detail panel at the decision point.** Show canonical name, family/source, aliases, provisional/registry status, confirmed-example count, and whether the displayed media count is partial. Offer “select existing,” “add alias,” and “create new” as distinct actions with normalized collision previews. | Reduces duplicate identities and mistaken alias/canonical choices. | Medium |
| 3 | **Add cross-cluster compare suggestions.** For the current image or a cluster representative, show the closest confirmed examples and likely sibling clusters with scores, model/backend, and match reasons. Offer compare/assign actions, but require explicit human confirmation. | Helps resolve recurring people spread across many clusters without risky automatic merges. | Medium-high |
| 4 | **Upgrade cluster risk from a size cutoff to measurable review cues.** Show face count, cluster cohesion/spread, source-family mix, missing-face coverage, and likely outliers; let the operator split a cluster into review groups. Keep bulk actions disabled when evidence is weak or the scan is incomplete. | Finds mixed clusters that fall below the current size threshold and makes large clusters easier to review. | Medium-high |
| 5 | **Put a source-aware move preview before reconciliation.** Preview source path, destination path, identity family, stable source-row key, marker/evidence changes, and rollback plan. Keep each source adapter report-only until its database writes are validated and idempotent. | Prevents incorrect MD/RD rows or folder moves as that future feature is added. | High; required before source writes |
| 6 | **Make pipeline actions explain their scope and effects.** Separate “refresh matches,” “reconcile confirmed,” “rebuild references,” and “full cycle” into named stages with expected duration, affected stores, current run ID, progress, and cancel/rollback status. | Makes operational controls safer and reduces unnecessary full refreshes. | Medium |

## Recommendation

Start with **Priority 1: a resumable review queue**. It reuses the durable assignment records already in place and addresses the most frequent interaction cost: keeping one's place while processing many images. Prototype it as a read-only queue view first, then connect its “open next” action to the existing modal. Confidence: high.

**Five-minute verification:** create two queued assignments in a test app, open the queue, confirm counts and order, open the first item, advance to the second, and verify reload resumes at the expected item without changing any source files.

## Open gaps

- No live browser usability session or click-through timing study was performed for this note.
- Exact source database schemas and stable row keys still need inspection before implementing source-aware writes.
- Cluster cohesion and cross-cluster suggestion quality need a labeled, held-out sample before any automated action is considered.

## Implementation update — 2026-09-29

- Added a dedicated review-queue view with durable assignment status counts, resume-at-last-opened behavior, ordered image inspection, rejection for pending items, and a report-only destination/source preview. The durable record lacks the family, exact destination, and stable source row key, so the UI names these gaps instead of guessing.
- Identity details now show aliases, family/promotion context, and the scope of the unique-path count. This is evidence coverage from the configured folder scan and PicOrg records, not a completeness claim.
- Cluster review now shows observed label/family/method coverage and can browse confirmed identity examples. This is manual comparison, not a similarity-ranked recommendation: the UI has no supported model/version-scoped similarity endpoint. No scores or automatic assignments are fabricated.
- Pipeline status now surfaces the current stage and run ID when available and disables duplicate launch controls while a job is active.
- These changes do not implement MetaDaily/RedditDaily source database writes, identity promotion, face-cluster splitting, or measured cohesion/outlier scores. Those require stable source-row keys or validated model evidence and remain follow-up work.

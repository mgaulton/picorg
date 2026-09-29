# PicOrg identity evidence store

`identity_evidence_store.py` maintains the durable operational metadata store
at `.cache/picorg/identity_evidence.sqlite3` (override with
`PICORG_IDENTITY_STORE`). It imports the confirmed MetaDaily registry, PicOrg
face markers, and review assignments idempotently while retaining source and
record provenance.

The store is deliberately separate from the protected media trees:

- `/mnt/elements16a/Pron/metadaily/downloads` and
  `/mnt/elements16a/Pron/redditdaily/downloads` are read-only evidence inputs.
- `/mnt/elements16/@mixedpics_sorted` is the organized local reference tree.
- The selected face backend owns generated embeddings and remains compatible
  with photo_reorg's database schema.
- JSON files remain import/export and audit artifacts; SQLite is the mutable
  PicOrg metadata authority.

Each completed review pipeline also publishes an atomic
`.cache/picorg/current-run.json` pointer.  It records SHA-256 hashes for the
primary audit, face-cluster audit, and face-only reconciled audit.  This is a
stable current-view selector for `runweb.sh` and `review_ui.py`; it does not
replace the immutable dated snapshots needed for replay and rollback.  A
pointer is rejected if any referenced file changes, disappears, or is not a
face-only reconciled report.

The queue-only review path persists assignments in `assignment_queue` with the
expected SHA-256, identity, provenance, and lifecycle status. The JSON pending
file remains a compatibility export, while `reconcile_confirmed.py` reads the
SQLite queue and updates it to `applied`, `conflict`, or `error` only during an
explicit `--apply` run. A protected MetaDaily/RedditDaily root health failure
blocks that apply step without discarding the queue.

## Future: source-aware canonical database updates

PicOrg currently records and applies reviewed assignments in its own evidence
store and organized tree. It does not move media into MetaDaily or RedditDaily
canonical identity folders or write assignment rows into either source
system's database. Keep those databases read-only until this feature is
implemented.

Before enabling source writes, identify each media row by a stable source
database key captured at intake, inspect the MetaDaily and RedditDaily schemas,
and define separate adapters for their required path, identity, and related
row updates. Extend the durable assignment record with that source key and
explicit target folder. Apply each file move and database update through a
retryable operation journal: verify the expected SHA-256, make updates
idempotent, record each stage, and recover safely if the filesystem move
succeeds but the database transaction fails. Run adapters in report-only mode
and compare proposed updates with source rows before enabling writes per
family. This must not implicitly modify the shared identity registry.

Incremental face refreshes append the new/changed result set to `media`,
`face_observations`, and `matches` in one batch transaction, keyed by the
audit's SHA-256 fingerprint and a run ID. Reused unchanged results are not
rewritten, so daily runs remain incremental while the match history stays
auditable.

Scheduler jobs also write stage, processed-count, and terminal status to
`pipeline_runs`; the JSON scheduler-status file remains the live UI heartbeat
and fallback if SQLite telemetry is unavailable.

Rebuilds sync this store before coalescing and pass it as the confirmed-only
external reference gate. A failed rebuild cannot alter the source trees or
replace the active face database because promotion remains atomic and
validated.

## Trust model

1. Confirmed MD registry identities and confirmed PicOrg markers are eligible
   for external exemplars.
2. Pending/rejected registry rows and unconfirmed MD/RD folders are excluded.
3. Organized-folder content is still read-only and is coalesced by canonical
   identity; future policy can attach an explicit `trusted` flag without
   changing source paths.
4. Face embeddings are generated from the coalesced manifest, not from live
   source-tree mutations.

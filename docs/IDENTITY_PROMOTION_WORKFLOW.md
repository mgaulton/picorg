# Manual identity promotion workflow

This document defines how PicOrg turns a locally reviewed identity into a
candidate for the shared canonical identity registry. PicOrg never writes
`/opt/shared/identity_aliases.json`; non-provisional identities created in the
web UI are now persisted immediately to the project-local overlay.

## Authorities and ownership

| Layer | Location | Role | PicOrg write policy |
|---|---|---|---|
| Shared canonical registry | `/opt/shared/identity_aliases.json` | MD/RD-approved canonical IDs, display names, aliases, and provenance | Read-only; registry owner approves changes |
| Local review identities | `/opt/picorg/review_identities.json` | Provisional identities created during image/folder review | PicOrg may append local records |
| Project identity overlay | `/opt/picorg/project_registry.json` | Local non-provisional identities and aliases created in UI | PicOrg may atomically add UI-created identities; `review` family stays provisional |
| Assorted associations | `.cache/picorg/assorted-folder-associations.json` | Metadata linking a person-like `/mnt/assorted` folder to an identity | Metadata only; no media moves |
| Evidence authority | `.cache/picorg/identity_evidence.sqlite3` | Hashes, markers, assignments, matches, pipeline runs, and provenance | PicOrg-owned WAL database |
| Face markers | `/opt/picorg/identity_face_markers.json` | Fingerprinted confirmed exemplars used by matching | Updated only by explicit reconciliation/review workflows |
| Organized media | `/mnt/elements16/@mixedpics_sorted` | Destination for explicitly confirmed movable media | Never treated as registry approval by itself |

The shared registry is the only canonical identity authority. A folder name,
filename, face cluster, or local review identity is evidence, not canonical
approval.

When the UI creates a new identity in any family except `review`, it saves the
identity and its submitted aliases to `project_registry.json` as part of the
create operation. Existing aliases and unrelated overlay settings are
preserved. A `review`-family identity remains provisional and must be promoted
explicitly after confirmation. Neither path modifies the shared MD/RD registry.

## End-to-end lifecycle

### 1. Intake and dedupe

Run the normal intake first. MetaDaily and RedditDaily downloads remain
protected, read-only reference sources. Exact duplicates are handled by the
priority dedupe report; no registry promotion occurs at this stage.

```bash
cd /opt/picorg
./run_picorg.sh --dry-run
```

Use apply/dedupe options only when the normal PicOrg safety gates pass. A
failed or partial mount must remain in the attention queue; do not infer a new
identity from an incomplete scan.

### 2. Associate assorted folders (metadata-only)

Open the LAN UI and select **Assorted folders**. Select one or more non-generic
leaf folders, choose a canonical identity, or enter a new local identity, then
choose **Save association (no moves)**.

The tab writes only:

- `.cache/picorg/assorted-folder-associations.json`
- an append-only `assorted_folder_association` event in the review ledger
- a local pending record in `review_identities.json` when a new identity is created

The source folder and all contained files remain untouched. These associations
are candidate evidence and still require face/image review before promotion.

### 3. Build and review evidence

After the identity has enough verified examples, refresh the durable evidence
store and canonical face baseline. The baseline reads the shared registry,
confirmed markers, organized folders, MD/RD reference roots, and assorted
folders without modifying those sources.

```bash
./build_canonical_face_baseline.sh
```

The baseline must remain complete and healthy for production matching. Generic
or ambiguous labels are excluded. A degraded run is report-only and must not
be used for automatic moves. An assorted-folder association by itself is not
trusted baseline evidence; the folder becomes canonical face-reference input
only after the identity is approved in the shared registry or has separately
verified PicOrg markers.

Run the matching/review cycle against the validated database:

```bash
./run_existing_face_db.sh --ingest
```

Use the review UI to inspect image-level evidence. Confirmed assignments add
fingerprinted marker candidates; low-confidence, multi-person, unreadable, and
conflicting media remain review-only.

### 4. Apply confirmed local assignments

Assignments made while a rebuild is active are queued. Review the queue first:

```bash
./.venv/bin/python - <<'PY'
from pathlib import Path
from identity_evidence_store import list_assignment_queue
for row in list_assignment_queue(Path('.cache/picorg/identity_evidence.sqlite3'), ['pending', 'error', 'conflict']):
    print(row)
PY
```

Report-only reconciliation:

```bash
./.venv/bin/python reconcile_confirmed.py \
  --pending-assignments .cache/picorg/pending-review-assignments.json \
  --evidence-db .cache/picorg/identity_evidence.sqlite3
```

Only after reviewing the report, explicitly apply confirmed media moves and
marker updates:

```bash
./.venv/bin/python reconcile_confirmed.py --apply \
  --pending-assignments .cache/picorg/pending-review-assignments.json \
  --evidence-db .cache/picorg/identity_evidence.sqlite3
```

Protected MD/RD roots are rejected, SHA-256 changes produce conflicts, and
move/undo provenance is retained in the review ledger and move history.

### 5. Project-local promotion preview

Inspect what confirmed decisions could be exported to the project overlay:

```bash
curl -s http://127.0.0.1:8787/api/export-preview | .venv/bin/python -m json.tool
```

Only confirmed decisions with a non-`review` family are eligible. Generic
identities, unresolved aliases, pending assignments, and provisional review
identities are skipped.

This export remains available for confirmed decisions created before automatic
UI registration or for importing aliases recorded later. New non-provisional
UI identities are already present in the local overlay. Explicit export:

```bash
./.venv/bin/python review_ui.py \
  --export-registry \
  --decisions /opt/picorg/review_decisions.json \
  --registry /opt/picorg/project_registry.json
```

This writes `project_registry.json`, not the shared canonical registry. Keep a
copy of the previous overlay and review the diff before using it in a run.

### 6. Canonical registry promotion (manual owner approval)

PicOrg currently stops before this boundary. A proposed canonical promotion
must be reviewed by the MD/RD registry owner and include:

1. One stable canonical spelling and friendly display name.
2. Explicit aliases and source provenance.
3. At least two independent evidence sources, including reviewed face evidence
   where available.
4. A marker/hash count and a list of the evidence audits used.
5. A check that the label is not generic, ambiguous, or a subreddit/topic.
6. A registry backup and an atomic update plan.
7. A post-update baseline refresh and matching smoke test.

The owner then updates `/opt/shared/identity_aliases.json` through the MD/RD
registry process. PicOrg must not edit that file directly. The next baseline
run imports the new canonical identity into the local evidence database; the
next match run can then use it as a trusted identity.

### 7. Verify and monitor

After a canonical registry update:

```bash
./.venv/bin/python identity_evidence_health.py \
  --db .cache/picorg/identity_evidence.sqlite3 \
  --output .cache/picorg/evidence-health.json \
  --backup-dir .cache/picorg/evidence-backups \
  --full
./build_canonical_face_baseline.sh
./run_existing_face_db.sh --ingest
```

Confirm that the new identity appears in the UI picker, has expected face
markers, and does not create mixed or generic clusters. Keep automatic moves
disabled until the held-out FMR/FNMR gates pass.

## Scheduler mapping

The Settings / pipeline tab and `picorg_scheduler.py` use the same boundaries:

| Job | Promotion-workflow role |
|---|---|
| `ingest` | Intake completed downloads; no canonical writes |
| `name_audit` | Produce filename/alias suggestions; never approval |
| `reconcile_confirmed` | Apply explicitly confirmed queued media and markers |
| `canonical_baseline` | Sync shared registry into local evidence and build face evidence |
| `evidence_health` | Check/backup the local SQLite authority |
| `refresh_matches` | Match new content against the validated existing face database |
| `rebuild_faces` | Full accuracy-first rebuild; UI becomes read-only for normal writes |
| `refresh_ui` | Reload the UI against the newest valid audit |
| `cycle` / `full_pipeline` | Orchestrate the above in the configured order |

Scheduling remains disabled by default. A scheduled cycle may queue and apply
confirmed local assignments, but it never writes the shared canonical registry.

## Rollback and recovery

- **Assorted association:** edit/revert the association ledger entry; media are
  unchanged.
- **Local review identity:** remove or correct the provisional record before
  the next evidence sync; do not delete evidence rows blindly.
- **Queued assignment:** reject it through the Assignment queue or mark the
  SHA conflict; no file is moved.
- **Applied media move:** use the recorded move ID and UI Undo/move history;
  verify the restored SHA before retrying.
- **Project overlay:** restore the saved `project_registry.json` copy.
- **Shared registry:** only the MD/RD registry owner rolls back the atomic
  registry update, then reruns evidence sync and baseline generation.
- **Face database:** retain the previous validated database until the new
  baseline, extractor report, and face-count validation pass.

## Current boundary

There is no PicOrg web form that directly promotes a local identity into
`/opt/shared/identity_aliases.json`. The workflow is **create locally → review
and reconcile confirmed evidence → refresh the local face baseline → obtain
registry-owner approval if shared MD/RD promotion is needed**.

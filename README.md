# picorg

Deterministic organizer for mixed Reddit media intake.

## What it does

- Reads identity sources from local files and existing registry trees.
- Resolves filenames and parent folders into canonical identity folders.
- Supports dry-run matching, manifest export, and optional apply mode.
- Keeps unmatched files and duplicate handling separate from canonical folders.
- Uses Reddit context when available: subreddit, author, title, filename, and aliases.
- On apply, intact source folders are moved where possible, identical folder content is routed to `duplicates/`, and same-name collisions get a hashed filename.
- Uses a repo-local overlay registry in [`project_registry.json`](/opt/picorg/project_registry.json) for project-only aliases and blocked generic tokens.
- `run_picorg.sh` is the recommended one-command baseline: it refreshes intake, runs hash-priority dedupe, creates the PicOrg audit, performs face grouping, and starts the LAN review UI. The default does not move library files.
- `/mnt/elements16a/Pron/metadaily` and `/mnt/elements16a/Pron/redditdaily` are permanently separate protected source stores. They may be read for identity/profile references, but ingest, dedupe, and picorg apply never move or modify them.
- Generic unmatched clusters can be sampled through `face_group_unmatched.py`; it uses the high-accuracy face DB to produce report-only identity groups for review before aliases or apply decisions.
- Apply mode skips matches below `0.95` confidence and reports them for review.
- Face grouping supports deterministic `--offset`, `--max-files`, and `--checkpoint` batching so the full unmatched set can be processed and resumed without repeating earlier work.
- `runweb.sh` stores face embeddings in the durable repo-local `.cache/picorg/` directory and migrates a matching legacy `/tmp` cache automatically; set `CACHE_ROOT` or `FACE_CACHE` to use another durable location.
- `picorg_manual.sh dry-run` stores audit JSON in durable `.cache/picorg/audits/` by default; set `AUDIT_ROOT` to choose another location.
- `runweb.sh` automatically selects the newest audit from that durable directory when `AUDIT` is not explicitly set.
- `runweb.sh` also writes a durable live log under `.cache/picorg/logs/` while continuing to stream output to screen; set `LOG_FILE` or `LOG_ROOT` to override.
- `runweb.sh` writes a non-destructive `*.preflight.json` report before face extraction, separating missing, unsupported, empty, corrupt, oversized, and candidate inputs from model accuracy.
- The face-grouping environment requires `setuptools<81` because the installed `face_recognition_models` package imports the legacy `pkg_resources` API.
- `run_face_group_batches.sh` resumes all batches and finishes with `reconcile_face_group_batches.py`, which deduplicates paths and evaluates cross-batch cluster consensus.
- Bare generic words are not treated as identities unless they are part of a username/handle-shaped string.
- Caches the derived identity catalog in `/tmp/picorg_identity_catalog_cache.json` by default and reuses it until the watched sources change. Set `PICORG_CATALOG_CACHE` to move the snapshot.
- Caches completed dry-run results in `/tmp/picorg_dry_run_cache.json` by default and reuses them when the roots, catalog state, and OCR settings match. Set `PICORG_DRY_RUN_CACHE` to move the cache.
- Repeat dry-runs print `cached: True` when they reused a prior result set.
- Dry-run cache entries are also reused per root, so interrupted runs can resume from completed roots.
- Gallery variants that only differ by numbered suffixes share the same in-run matcher cache key.
- Optional OCR fallback is available for low-confidence image matches when `PICORG_OCR_IMAGE` or `PICORG_OCR_COMMAND_JSON` is set.

See [`OPERATING_POLICY.md`](/opt/picorg/OPERATING_POLICY.md) for the manual workflow and confidence rubric.

## Input sources

- `/mnt/elements16/@mixedpics`
- `/mnt/elements16a/Pron/jdownloaderscomplete`
- `/mnt/desktop/Pictures`

## Identity sources

- `/opt/redditgrab/friend.txt`
- `/opt/pscrape/redditors.txt`
- `/opt/list.imdburl`
- `/opt/metadaily/social_accounts.txt`
- Confirmed profiles and aliases from `/opt/metadaily/data/identity_aliases.json`
- Confirmed profile evidence from [`identity_profile_verification.json`](/opt/picorg/identity_profile_verification.json); only `confirmed` records with at least two evidence URLs import handles
- `/opt/redditdaily/redditsubs.txt`
- `/opt/redditdaily/data/`
- Existing folder names under `/mnt/elements16a/Pron/redditdaily`
- Existing folder names under `/mnt/elements16a/Pron/pscrape`
- Follow/friend lists under `/opt/reddit/`, `/opt/redditgrab/`, `/opt/grabplaylist/`, and `/opt/photo_reorg/`

## Usage

For the conservative end-to-end workflow:

```bash
./run_picorg.sh
```

For an interactive menu with the same safe defaults and explicit confirmation
for mutating actions:

```bash
./run_picorg_menu.sh
```

Useful deliberate variants:

```bash
./run_picorg.sh --dry-run                      # no intake, moves, or quarantine
./run_picorg.sh --no-ingest --reuse-faces       # open the latest complete face audit
./run_picorg.sh --apply-high-confidence         # safety-gated name moves + face review
./run_picorg.sh --dedupe-apply                  # quarantine exact target duplicates
```

The lower-level `run_media_pipeline.sh` and `run_face_review_pipeline.sh`
remain available for debugging and individual stages.

Dry run:

```bash
python3 picorg_sorter.py dry-run --audit-out /tmp/picorg-dry-run.json
```

Export manifest:

```bash
python3 picorg_sorter.py manifest --output /tmp/picorg-manifest.json
```

Inspect catalog:

```bash
python3 picorg_sorter.py inspect --limit 20
```

Run input coverage preflight directly:

```bash
python3 media_preflight.py /path/to/audit.json --output /path/to/audit.preflight.json
```

Priority-aware exact dedupe (report first):

```bash
python3 dedupe_priority.py --output /tmp/picorg-priority-dedupe.json
```

The protected `/mnt/elements16a/Pron/redditdaily` and `metadaily` trees are
always treated as priority and are never modified. To quarantine verified exact
duplicates from `/mnt/elements16/@mixedpics` and `/mnt/desktop/Pictures` after
reviewing the report:

```bash
python3 dedupe_priority.py --apply
```

Run tests with the repository wrapper (it disables unrelated globally installed
pytest plugins and prefers `.venv` when present):

```bash
./run_tests.sh
```

## Profile verification and image references

Use [`/opt/redditdaily/docs/IDENTITY_PROFILE_VERIFICATION.md`](/opt/redditdaily/docs/IDENTITY_PROFILE_VERIFICATION.md)
to review public profile candidates. Record only confirmed accounts in
[`identity_profile_verification.json`](/opt/picorg/identity_profile_verification.json), with a canonical URL,
verification date, and first-party/independent evidence. Candidate and probable
accounts remain review-only.

For image corroboration, download or otherwise obtain permitted public reference
images manually, place them under `<family>__<canonical>/`, and run the offline
reporter:

```bash
python3 profile_image_match.py \
  --references /tmp/photo_reorg_social_references \
  --root /mnt/elements16/@mixedpics \
  --output /tmp/picorg_profile_image_matches.json \
  --index-output /opt/picorg/profile_image_index.json
```

This uses ImageMagick-normalized image fingerprints, never contacts websites,
and never changes files. A match is corroborating evidence only; it does not
create an identity alias or authorize apply mode.
When `profile_image_index.json` exists, picorg uses unique exact SHA-256
reference hits at `0.99` confidence; ambiguous collisions remain unmatched.
Enable it explicitly for a run with:

```bash
export PICORG_PROFILE_IMAGE_INDEX=/opt/picorg/profile_image_index.json
```

The index is local-only and excluded from Git; ordinary dry runs do not hash
every unmatched file.

For unmatched images that need external review, export a privacy-preserving
queue containing paths, hashes, and title hints:

```bash
python3 reverse_search_queue.py \
  /tmp/picorg_sorted_audit/20260731T152301Z.json \
  --output /tmp/picorg_reverse_search_queue.json \
  --limit 100 \
  --per-gallery 3
```

Use a permitted reverse-image provider manually, record candidate URLs and
independent corroboration in the queue, and promote only confirmed identities
to `identity_profile_verification.json`. `FB IMG` and `RDT` are retained and
marked as Facebook/Reddit download sources; `--per-gallery` keeps large source
clusters from monopolizing review. The queue tool never uploads media.

Create a labeled local review sheet, skipping corrupt files:

```bash
python3 reverse_search_contact_sheet.py \
  /tmp/picorg_reverse_search_queue.json \
  --output /tmp/picorg_reverse_search_contact_sheet.jpg \
  --limit 40
```

Launch the candidate-cluster review UI (binds to all host interfaces for LAN access by default):

```bash
python3 review_ui.py \
  --audit /tmp/picorg_sorted_audit/20260731T170645Z.json \
  --decisions /opt/picorg/review_decisions.json
```

Open `http://<picorg-host-ip>:8787/` from a LAN device. The UI loads 50 clusters
at a time and requests more pages on demand; the cluster index is cached beside
the audit as `*.clusters.json`, so restarts avoid rebuilding unchanged audits.
It previews allowlisted local media and records explicit assignments in
`review_decisions.json`. It never moves files or edits the registry; promote
reviewed decisions only after recording independent evidence.

The service has no login layer, so keep port 8787 restricted to a trusted LAN or
run with `--host 127.0.0.1` for local-only access. Do not expose it directly to
the internet.

Operational probes are available at `/healthz` and `/readyz`. API responses are
bounded, carry an `X-Request-ID`, disable caching, and reject request bodies
larger than 64 KiB. The UI includes retry feedback, accessible live regions,
keyboard focus states, responsive layout behavior, and reduced-motion support.

Decisions support `pending`, `needs-evidence`, `confirmed`, and `rejected`.
Only confirmed decisions can be promoted explicitly:

```bash
.venv/bin/python review_ui.py \
  --export-registry \
  --decisions /opt/picorg/review_decisions.json \
  --registry /opt/picorg/project_registry.json
```

The bulk endpoint (`POST /api/decisions/bulk`) can assign the same identity to
several selected cluster IDs; it still records the chosen status and requires
an explicit later export for registry changes.

`POST /api/clusters/<cluster_id>/members` with `action: add` or `remove` stores
reviewer membership overrides in `review_overrides.json`. Use `target_cluster_id`
to move an image to another candidate cluster. Removing a decision is supported
with `DELETE /api/decisions/<cluster_id>`.

For face-based candidate grouping within the unmatched collection, install the
optional dependencies and generate a review audit:

```bash
.venv/bin/pip install -r requirements-face.txt
.venv/bin/python face_cluster_unmatched.py \
  --audit /tmp/picorg_sorted_audit/20260731T170645Z.json \
  --output /tmp/picorg_sorted_audit/20260731T170645Z.face-clusters.json
```

For the complete name+face reconciled workflow, use `runweb.sh` after installing
the optional dependency once:

```bash
INSTALL_FACE_DEPS=1 ./runweb.sh
```

For a single safe command that runs the dry-run pipeline, builds face groups,
and starts the review UI on all LAN interfaces:

```bash
./run_face_review_pipeline.sh
```

It defaults to `HOST=0.0.0.0`, `PORT=8787`, skips `photo_reorg`, and never
applies organizer moves. Override `PORT` or set `HOST=127.0.0.1` for local-only
access.
Authentication is disabled by default. To enable it explicitly, set both
`PICORG_UI_AUTH=1` and `PICORG_UI_TOKEN`; clients then send `X-Picorg-Token`
(or a Bearer token). Health probes remain unauthenticated.

Add `--ingest` when incoming files should be fetched first:

```bash
./run_face_review_pipeline.sh --ingest
```

To explicitly enable live high-confidence processing:

```bash
./run_face_review_pipeline.sh --apply-high-confidence
```

This first applies picorg's high-confidence name matches (`>=0.95`), then
rebuilds photo_reorg's face database from `REFERENCE_ROOT` (default
`/mnt/elements16/@mixedpics_sorted`), matches remaining audit items against
that database, and finally builds review-only face groups and starts the LAN UI.
Review the dry-run results before enabling this mode; it is the only mode that
moves files. Override `REFERENCE_ROOT` and `FACE_DB` when your canonical tree
or database lives elsewhere.

Subsequent starts reuse the cached face audit. Set `FORCE_FACE_REBUILD=1` after
changing the source audit. Face embeddings are checkpointed every 500 images in
`*.face-embeddings.json`, keyed by path and SHA-256 file fingerprint; unchanged
files reuse their encodings while replaced/modified files are rescanned.
Stopping and rerunning resumes completed work.

To launch immediately without extracting faces again, point `AUDIT` at the
same source audit and reuse its existing face-cluster report:

```bash
AUDIT=/path/to/audit.json \
FACE_AUDIT=/path/to/audit.face-clusters.json \
USE_EXISTING_FACE_AUDIT=1 \
PICORG_UI_TOKEN='use-a-long-random-secret' ./runweb.sh
```

When `AUDIT` and `FACE_AUDIT` are omitted in reuse mode, `runweb.sh` selects
the newest primary audit that has a non-empty companion face-cluster report.

This mode refuses to start if the face-cluster report is absent or empty.
The
script writes a reconciled audit and starts the LAN UI against it. It is a similarity candidate
queue, not identity confirmation: multi-face/low-quality images are deferred,
and no face result is exported automatically. Within a cluster, select several
images and use “Assign selected to identity”; type a new identity and use “Save
typed identity as new” to record it in the separate review identity ledger.

### Face extraction speed

Extraction is resumable and now skips files already classified by preflight as
missing, corrupt, unsupported, or oversized. Keep `num-jitters=1` for normal
operation, reuse the embedding cache, and benchmark `FACE_BACKEND=insightface`
separately; the current CPU benchmark was about 2.2× faster than dlib, but it
requires independent accuracy and model-license approval.
For accuracy-priority production runs, retain the default dlib
`--upsample-times 1`. A value of `0` is a speed benchmark only and must not be
used for automatic moves without a held-out recall comparison.
When name and face groupings disagree, face-cluster membership drives the
review cluster; name titles remain supporting context instead of merging faces.

Use `GET /api/export-preview` to inspect which confirmed decisions are
promotable. Decisions with the provisional `review` family are never exported.

Apply mode exists, but should be used only when the destination tree is writable and the audit output has been reviewed.

For manual operator runs, use [`RUNBOOK.md`](/opt/picorg/RUNBOOK.md), [`OPERATING_POLICY.md`](/opt/picorg/OPERATING_POLICY.md), and the wrapper script [`picorg_manual.sh`](/opt/picorg/picorg_manual.sh).

## Manual production flow

Use this when you want a periodic run without automation:

1. Inspect the catalog.
2. Run a dry pass.
3. Review `ground_truth_precision`, `ground_truth_recall`, `match_coverage`, `source_metrics`, `top_unmatched`, and any medium-confidence or precedence-sensitive matches.
4. Apply only when the dry run is stable.
5. Re-check the apply audit and duplicates.

Recommended commands:

```bash
./picorg_manual.sh inspect
./picorg_manual.sh dry-run
./picorg_manual.sh apply
```

Optional OCR-assisted review for low-confidence cases:

```bash
./picorg_manual.sh dry-run --ocr-image yock1/embycreditocr:latest
```

Production-ready runs should meet the same acceptance criteria described in [`RUNBOOK.md`](/opt/picorg/RUNBOOK.md).

## Dry-run baseline

Latest full audit (`20260731T170645Z`):

- Scanned: 49,995
- Matched: 1,303
- Unmatched: 48,692
- High confidence: 827
- Labeled precision: 1.0
- Labeled recall: 0.9195
- Match coverage: 0.0261

The audit also reports labeled precision (correct predictions / predictions), labeled recall
(correct predictions / labeled cases), and coverage by intake source. `ground_truth_accuracy`
is retained as a backwards-compatible alias for labeled recall.

This baseline is intentionally conservative: it prioritizes correct identity placement over forcing a guess on caption-only files, and it no longer lets bare generic words become identities. It is not production-ready for broad automatic sorting until the runbook thresholds are met.

### Calibrate face confirmation thresholds

Face clustering and identity confirmation use different operating points. Build
a small local labelled-pair file, then calibrate a strict confirmation threshold
from the cached embeddings:

Generate the pair file reproducibly from confirmed review decisions:

```bash
python3 build_face_pairs.py \
  --decisions review_decisions.json \
  --embeddings /path/to/face-embeddings.json \
  --output /tmp/picorg-labelled-pairs.json
```

Create a deterministic image-disjoint held-out set before calibration:

```bash
.venv/bin/python split_face_pairs.py \
  --pairs /tmp/picorg-labelled-pairs.json \
  --train-output .cache/picorg/face-train.json \
  --heldout-output .cache/picorg/face-heldout.json
```

```bash
.venv/bin/python face_match_benchmark.py \
  --pairs /path/to/face-pairs.jsonl \
  --embeddings /tmp/picorg_sorted_audit/20260731T170645Z.face-embeddings.json \
  --preflight /tmp/picorg_sorted_audit/20260731T170645Z.preflight.json \
  --max-fmr 0.001 \
  --output /tmp/picorg-face-calibration.json
```

Use `selected.threshold` only for identity suggestions after review. Keep the
face-cluster threshold broader for candidate discovery, and recalibrate when
the embedding model, image population, or quality gates change.
The report's `preflight_counts` show how many inputs were eligible candidates
versus excluded before extraction; do not treat excluded files as model false
nonmatches.
The selected operating point also includes `fmr_ci95` and `fnmr_ci95`; zero
observed errors do not imply zero real-world error with a small labeled set.

### Optional InsightFace benchmark backend

An isolated SCRFD/ArcFace backend is available for side-by-side evaluation:

```bash
.venv/bin/pip install -r requirements-insightface.txt
```

Use [insightface_backend.py](/opt/picorg/insightface_backend.py) only for a
separate benchmark first. Its model pack has non-commercial research-use
restrictions; do not replace the default dlib backend until licensing and a
held-out local benchmark are both approved.

Run the reproducible comparison with:

```bash
.venv/bin/python insightface_pair_benchmark.py \
  --pairs /tmp/picorg-labelled-pairs.json \
  --output /tmp/picorg-face-calibration-insightface.json
```

After a held-out benchmark approves the model, run a review-only cluster pass
with `FACE_BACKEND=insightface`; this uses a separate cache and never reuses
dlib embeddings:

```bash
FACE_BACKEND=insightface ./run_face_review_pipeline.sh
```

### Optional UniFace benchmark backend

UniFace can be evaluated locally without changing the production cache. Install
it in an isolated environment (its model downloads are separate from `.venv`):

```bash
uv venv /tmp/picorg-uniface-venv --python 3.11
UV_CACHE_DIR=/tmp/picorg-uv-cache uv pip install \
  --python /tmp/picorg-uniface-venv/bin/python -r requirements-uniface.txt
```

Build labeled pairs from review decisions, then compare ArcFace (the default)
or AdaFace embeddings:

```bash
./.venv/bin/python build_face_pairs.py \
  --decisions review_decisions.json \
  --embeddings .cache/picorg/20260802T004521Z.face-embeddings.json \
  --output /tmp/picorg-labelled-pairs.json
/tmp/picorg-uniface-venv/bin/python uniface_pair_benchmark.py \
  --pairs /tmp/picorg-labelled-pairs.json \
  --backend adaface \
  --output /tmp/picorg-face-calibration-uniface.json
```

This is benchmark-only until a sufficiently large held-out set confirms error
bounds and the individual model licenses are approved. UniFace itself is MIT,
but its bundled model choices have separate licenses; do not treat a good small
pair score as production evidence.

## Reproducibility and evaluation

The repository includes `pyproject.toml` for reproducible `uv` environments and
optional face backends. Generate a local Promptfoo regression dataset from
confirmed pairs without uploading image bytes:

```bash
.venv/bin/python tools/export_promptfoo_dataset.py \
  --pairs /tmp/picorg-labelled-pairs.json \
  --output .cache/promptfoo/face-pairs.jsonl
```

Each review run also writes a privacy-preserving `*.run-manifest.json` beside
the audit, including an audit SHA-256, backend, counts, and measured accuracy.
These manifests are suitable for local OpenTelemetry/Langfuse ingestion while
keeping image paths and image contents out of remote services.

Check whether a specific run meets all automatic-move gates:

```bash
.venv/bin/python production_readiness.py \
  --audit /path/to/audit.json \
  --preflight /path/to/audit.preflight.json \
  --benchmark /path/to/face-calibration.json \
  --ui-token "$PICORG_UI_TOKEN"
```

The default benchmark gate requires at least 100 genuine and 100 impostor
pairs and 95% confidence-interval upper bounds no higher than 1% for both FMR
and FNMR. Override those values only with documented evidence.

For InsightFace, add `--backend insightface --model-license-confirmed` only
after the model licensing terms have been reviewed for your deployment.

## Reddit matching order

1. Explicit `u/` or `r/` markers in the file path or name.
2. Exact alias match from the identity registry.
3. Canonical name match in the title or filename.
4. Parent folder hint.
5. OCR text from the image when configured.
6. Fallback to `unmatched`.

Numbered filename variants like `(1)`, `(2)`, etc. are treated as the same gallery title for matching and review.
Plain repeated download suffixes like `Scene00001.jpg` and `Scene00002.jpg` are also grouped as one gallery set.

Confirmed metadaily aliases are read as catalog input only. Generic exact words such as `pov`,
`daddy`, and `stacked` are treated as ambiguous and require stronger Reddit context before they
can resolve an identity.

### OCR fallback

OCR is off by default. To enable it against a local Tesseract Docker image, set `PICORG_OCR_IMAGE` and let the sorter use the built-in `docker run` wrapper:

```bash
export PICORG_OCR_IMAGE=my-local-tesseract-image
python3 picorg_sorter.py dry-run
```

For a custom Docker invocation, set `PICORG_OCR_COMMAND_JSON` to a JSON list of argv items. The placeholders `{path}`, `{dir}`, `{name}`, and `{stem}` are expanded per file.

The wrapper script also accepts OCR flags:

```bash
./picorg_manual.sh dry-run --ocr-image yock1/embycreditocr:latest
```

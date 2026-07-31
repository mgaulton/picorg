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
- `run_media_pipeline.sh` can sequence remote intake, picorg dry-run/apply, and the photo_reorg dry-run; intake and apply are opt-in.
- `/mnt/elements16a/Pron/metadaily` and `/mnt/elements16a/Pron/redditdaily` are permanently separate protected source stores. They may be read for identity/profile references, but ingest, dedupe, and picorg apply never move or modify them.
- Generic unmatched clusters can be sampled through `face_group_unmatched.py`; it uses the high-accuracy face DB to produce report-only identity groups for review before aliases or apply decisions.
- Apply mode skips matches below `0.95` confidence and reports them for review.
- Face grouping supports deterministic `--offset`, `--max-files`, and `--checkpoint` batching so the full unmatched set can be processed and resumed without repeating earlier work.
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

For the staged end-to-end workflow:

```bash
/opt/picorg/run_media_pipeline.sh
```

Add `--ingest` to run `/opt/move_downloads_remote.sh`, `--apply` to apply picorg moves, and omit neither unless the preceding dry-run is acceptable.

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
  --output /tmp/picorg_profile_image_matches.json
```

This uses ImageMagick-normalized image fingerprints, never contacts websites,
and never changes files. A match is corroborating evidence only; it does not
create an identity alias or authorize apply mode.

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

Latest full audit (`20260731T151913Z`):

- Scanned: 49,995
- Matched: 1,157
- Unmatched: 48,838
- High confidence: 689
- Labeled precision: 1.0
- Labeled recall: 0.7335
- Match coverage: 0.0231

The audit also reports labeled precision (correct predictions / predictions), labeled recall
(correct predictions / labeled cases), and coverage by intake source. `ground_truth_accuracy`
is retained as a backwards-compatible alias for labeled recall.

This baseline is intentionally conservative: it prioritizes correct identity placement over forcing a guess on caption-only files, and it no longer lets bare generic words become identities. It is not production-ready for broad automatic sorting until the runbook thresholds are met.

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

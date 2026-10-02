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
- Supports manual visual collections (for example, `gothgroup` or `redheadgroup`) separately from identities. Select individual images in a cluster or add the current image from the model viewer; membership is stored in `manual_groups.json`. Collection membership creates no face markers and does not move media; individual identity matches remain authoritative for organization.
- `run_picorg.sh` is the recommended one-command baseline: it refreshes intake, runs hash-priority dedupe, creates the PicOrg audit, performs face grouping, and starts the LAN review UI. The default does not move library files.
- `/mnt/elements16a/Pron/metadaily/downloads` and `/mnt/elements16a/Pron/redditdaily/downloads` are permanently separate protected download stores. They may be read for identity/profile references, but ingest, dedupe, and PicOrg apply never move or modify them.

### Read-only release check

Before promoting a workflow or UI change, run the local release smoke check:

```bash
./picorg_release_check.sh          # safety, clustering, UI, and observer tests
./picorg_release_check.sh --full   # the complete repository test suite
```

The check validates launcher shell syntax and runs tests only; it does not
ingest, move, rebuild, start services, or contact external AI providers.
- Generic unmatched clusters can be sampled through `face_group_unmatched.py`; it uses the high-accuracy face DB to produce report-only identity groups for review before aliases or apply decisions.
- Apply mode skips matches below `0.95` confidence and reports them for review.
- Face grouping supports deterministic `--offset`, `--max-files`, and `--checkpoint` batching so the full unmatched set can be processed and resumed without repeating earlier work.
- `runweb.sh` stores face embeddings in the durable repo-local `.cache/picorg/` directory and migrates a matching legacy `/tmp` cache automatically; set `CACHE_ROOT` or `FACE_CACHE` to use another durable location.
- `picorg_manual.sh dry-run` stores audit JSON in durable `.cache/picorg/audits/` by default; set `AUDIT_ROOT` to choose another location.
- `runweb.sh` automatically selects the newest audit from that durable directory when `AUDIT` is not explicitly set.
- `runweb.sh` also writes a durable live log under `.cache/picorg/logs/` while continuing to stream output to screen; set `LOG_FILE` or `LOG_ROOT` to override.
- `runweb.sh` writes a non-destructive `*.preflight.json` report before face extraction, separating missing, unsupported, empty, corrupt, oversized, and candidate inputs from model accuracy.
- Fresh UI launches bound `PICORG_PREFLIGHT_TIMEOUT` (default 1800 seconds) to media preflight; set it to `0` only for diagnostics. Existing-audit launches reuse a valid preflight sidecar and avoid rescanning degraded mounts.
- Rebuilds are strict by default: any timed-out source root prevents database promotion. For a deliberately usable but incomplete database while a mount is unavailable, set `PICORG_ALLOW_DEGRADED_REBUILD=1`; the coalescer records `complete: false` and the missing roots, and a later strict rebuild is still required for production completeness.
- If a mounted tree responds to `stat` but hangs during traversal, set `PICORG_COALESCE_SKIP_ROOTS` to a colon-separated root list together with `PICORG_ALLOW_DEGRADED_REBUILD=1`; skipped roots are recorded in `.coalesce-report.json` and must be included in the later strict rebuild.
- Degraded rebuilds also tolerate extractor-level unreadable-image errors (`PICORG_ALLOW_FACE_ERRORS` defaults to `1` when degraded); strict rebuilds keep the fail-closed error gate.
- Reference coalescing validates image payloads when repair is enabled (`PICORG_REPAIR_CORRUPT=1`, the default). Invalid files are never overwritten blindly: a verified same-name local copy is staged atomically, the damaged original is quarantined, and the repair ledger records the replacement; without a valid copy the file is skipped and remains in the attention queue.
- MetaDaily and RedditDaily are read-only face-reference authorities: rebuilds include their folders only when the identity is confirmed in `identity_face_markers.json` or the canonical shared `/opt/shared/identity_aliases.json` registry. The former `/opt/metadaily/data/identity_aliases.json` path is legacy and is not selected implicitly. Their download paths are never moved or renamed; the coalescer creates temporary symlinks under `.cache/picorg/` and reports excluded unconfirmed folders in `.coalesce-report.json`.
- `identity_evidence_store.py` maintains the durable PicOrg metadata authority at `.cache/picorg/identity_evidence.sqlite3`, importing MD confirmation, face markers, and review assignments with provenance. JSON ledgers remain interchange formats/audits; embeddings remain in the selected face backend for compatibility.
- `identity_evidence_health.py` performs a read-only SQLite quick/full integrity check, reports the runtime SQLite version and WAL files, and can create a consistent online backup. The scheduler's `evidence_health` job runs it with a backup under `.cache/picorg/evidence-backups/`; the Settings / pipeline tab exposes the latest result. SQLite WAL databases must be backed up through SQLite rather than by copying only the main file.
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
- Confirmed profiles and aliases from `/opt/shared/identity_aliases.json`
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

For long runs over SSH, start the menu inside GNU screen so disconnecting does
not stop the worker:

```bash
screen -S picorg
cd /opt/picorg && ./run_picorg_menu.sh
# detach with Ctrl-A, then D; reconnect with:
screen -r picorg
```

The fixed-mode launcher starts every long job in a detached `screen` session
(falling back to `tmux`):

```bash
./picorg_screen.sh                            # open the menu (default)
./picorg_screen.sh quick                      # fresh cached name audit + new/changed face matches + UI
./picorg_screen.sh quick --ingest              # intake/dedupe/name gate, then incremental face matches + UI
./picorg_screen.sh full                       # full safe ingest/rebuild/review pipeline
./picorg_screen.sh ui                         # restart UI only
```

Equivalent explicit modes are still available:

```bash
./picorg_screen.sh rebuild
./picorg_screen.sh existing
./picorg_screen.sh benchmark
./picorg_screen.sh pipeline --apply-high-confidence
./picorg_screen.sh menu
python3 picorg_scheduler.py status
```

Use `screen -r picorg-job` (or the printed attach command) to watch output.
If `/run` is read-only, the launcher automatically places tmux sockets under
`/tmp/picorg-tmux`. The launcher verifies that screen/tmux stayed alive; if
both cannot allocate a session, it falls back to a detached PID/log pair under
`.cache/picorg/logs` and prints the exact `tail -f` command.

The web UI's **Settings / pipeline** tab persists the scheduler configuration
under `.cache/picorg/scheduler.json`. Scheduling is disabled by default; when
enabled, each cycle ingests (if enabled), reconciles confirmed assignments,
refreshes the incremental canonical face baseline, and uses the incremental
existing-DB matcher by default. Set
`rebuild_faces: true` only for an explicit periodic full rebuild. The live
status is stored in `.cache/picorg/scheduler-status.json` and is polled by the UI.

The scheduler also runs the evidence-store health job by default. Run it manually
without starting the pipeline when diagnosing the metadata store:

```bash
./.venv/bin/python identity_evidence_health.py \
  --db .cache/picorg/identity_evidence.sqlite3 \
  --output .cache/picorg/evidence-health.json \
  --backup-dir .cache/picorg/evidence-backups \
  --full
```

The health check does not inspect or modify protected media roots. A warning
about an unapproved SQLite version should be resolved before relying on WAL
under concurrent UI/scheduler writes; it does not itself rewrite the database.

To establish the durable canonical face baseline used by future matching, run
the single resumable launcher after the MD/RD registry or organized identity
folders change:

```bash
./picorg_screen.sh baseline
```

This reads `/mnt/elements16/@mixedpics_sorted`,
`/mnt/elements16a/Pron/metadaily/downloads`, and
`/mnt/elements16a/Pron/redditdaily/downloads`, plus person-named folders under
`/mnt/assorted`, without modifying them. Hashes
and model-versioned observations are stored in
`.cache/picorg/identity_evidence.sqlite3`; incremental hash/embedding caches
and `.cache/picorg/canonical-face-baseline.json` make retries safe. Unhealthy
or unreadable paths are reported and skipped, not treated as valid evidence.
Set `PICORG_BASELINE_EXTRACT=0` for a cache-only report.
Before scanning, the launcher synchronizes the local evidence database from
the canonical `/opt/shared/identity_aliases.json` registry; set
`PICORG_BASELINE_SYNC_EVIDENCE=0` only for a deliberate cache-only diagnostic.

Face-reference rebuilds automatically repair Reddit HTML error pages saved with
an image extension. They first try a valid same-name copy in the priority roots,
then use only allow-listed Reddit preview hosts, quarantine the original HTML,
append a per-run JSONL repair ledger, and atomically write the validated
replacement to the PicOrg repair-staging tree. Redirects are re-checked against
the HTTPS host allowlist. The protected Metadaily and Redditdaily source files
are never overwritten by this step. Set `PICORG_REPAIR_HTML=0` to disable repair
entirely.

The menu is a split-pane curses TUI: actions and keyboard shortcuts are on the
left, while the live command output is streamed on the right. Choose option 1
for normal new-download intake, option 3 to reuse an existing face audit,
option 4 only when you want benchmark-gated safety moves (run **b** first), option 7 for a slow face-DB
rebuild, option 8 for a review-only identity match against the latest audit,
option 9 for the slowest full-accuracy run, and **c** for identity-isolated
reference clustering. Option **8** uses an existing validated face database to
generate the gallery, match identities, rebuild face clusters, and start the
UI. Option **f** runs the complete ingest/name/face-database/match/UI pipeline.
Option **e** builds a hash-only reverse-search queue and
mock online-evidence report; it never contacts a provider or uploads media.
Options 8, **c**, and **e** never move files; **f** runs the explicit
benchmark/safety-gated full workflow. Use number keys
`1`–`9`, letter shortcuts, `j`/`k` or arrow keys to move, `x` to stop the
current operation, and `q` or `0` to exit when idle. In the log pane, use
PageUp/PageDown to scroll through prior output and Left/Right (or `h`/`l`) to
inspect long lines.

Useful deliberate variants:

```bash
./run_picorg.sh --dry-run                      # no intake, moves, or quarantine
./run_picorg.sh --no-ingest --reuse-faces       # open the latest complete face audit
./run_picorg.sh --apply-high-confidence         # safety-gated name moves + face review
./run_picorg.sh --dedupe-apply                  # quarantine exact target duplicates
./run_name_org.sh                              # fresh name/alias moves only; no face stage or UI
./run_name_org.sh --ingest                     # ingest, priority-quarantine dupes, then name moves
./picorg_screen.sh rebuild                       # persistent rebuild with health/recovery retries
./rebuild_face_data.sh                           # direct fail-closed rebuild
./run_existing_face_db.sh                        # existing DB -> gallery -> match -> clusters -> LAN UI
FACE_DATABASE_BACKEND=picorg ./rebuild_face_data.sh # PicOrg-owned backend (parity test)
```

The coalescer records and skips per-file `EIO`/unreadable-stat failures in the
repair ledger without aborting the gallery pass. The screen rebuild wrapper
also records recoverable unreadable source paths in
`.cache/picorg/rebuild-recovery-skips.txt`, retries up to three times, and
continues without deleting the damaged file. Use `./rebuild_face_data.sh`
directly when retries are not desired; clear a previously repaired skip with
`PICORG_FACE_RECOVERY_REUSE_SKIPS=0`. Each retry also writes an attempt-scoped
log under `.cache/picorg/rebuild-reports/`; the recovery heartbeat reports the
latest stage, `folder X/Y`, and extraction counters, and the LAN review
banner/API exposes that progress while the UI is safely read-only.
Unchanged normal media is classified from a metadata-validated repair-scan
cache (`.cache/picorg/repair-scan-cache.json`), avoiding repeated prefix reads;
device, inode, size, mtime, or ctime changes invalidate an entry.
Each source root is also bounded by `PICORG_COALESCE_ROOT_TIMEOUT` (default
one hour). A stalled FUSE/network root fails the attempt closed and leaves the
previous database active; set it to `0` only for a deliberate diagnostic run.

Long stages now emit explicit heartbeats even when a source tree has no
per-file output. Coalescing reports the current identity, file count, elapsed
time, and rate; name audits and priority dedupe report root/stage counters;
confirmed-assignment reconciliation reports records, moves, and errors;
face matching and face extraction report bounded file counters, rates, and
ETAs; and the scheduler writes a process-alive heartbeat to its status JSON and
console every 30 seconds. Adjust the common cadence with `PICORG_PROGRESS_SECONDS` or
the scheduler-specific `PICORG_SCHEDULER_HEARTBEAT_SECONDS`; baseline extraction
uses `PICORG_BASELINE_PROGRESS_SECONDS`. Coalescing also accepts
`--progress-seconds`.

The default photo_reorg backend requires `setuptools<81` in its virtualenv
because `face_recognition_models` still imports `pkg_resources`; the rebuild
preflight now detects this before touching the existing database.

Future bounded throughput work is documented in
[`docs/FUTURE_THROUGHPUT_PLAN.md`](/opt/picorg/docs/FUTURE_THROUGHPUT_PLAN.md).
It is not enabled: any parallel matcher must first pass resource, restart,
and single-worker accuracy-parity gates.
```bash
./restart_picorg_review.sh                       # stop UI, rebuild/validate newest audit, restart LAN UI in screen
./restart_picorg_review.sh --no-rebuild           # restart UI using an existing face/reconciled audit
./run_accuracy_benchmark.sh                       # held-out image-level calibration; review-only
FACE_BENCHMARK_REPORT=.cache/picorg/image-face-calibration.json \
  ./run_face_review_pipeline.sh --apply-high-confidence # require held-out gate
```

Face-reference staging defaults to the persistent PicOrg cache (not `/tmp`) so
multi-hour rebuilds are not affected by temporary-file cleanup. Override
`REFERENCE_ROOT` only when the alternate filesystem is known to remain mounted.

Every image or cluster review now also appends an event to
`review_decision_ledger.jsonl` (override with `REVIEW_LEDGER`). This preserves
confirmed and rejected examples for hard-negative calibration without changing
the current decision files.

The native rebuild stores a fingerprint-keyed extraction cache at
`.cache/picorg/face-embeddings-native.json`; unchanged images are reused on
later coalescing runs. Fingerprinting uses two bounded workers by default;
adjust `PICORG_FACE_HASH_WORKERS` for slower or faster storage. Set `FACE_CACHE`
to relocate the cache or delete it when the face model/configuration changes.
Native rebuilds leave the current SQLite database in place until the replacement
passes validation, so an interrupted or resource-limited rebuild cannot erase
the last known-good database.

For accuracy-first speed testing, `face_group_unmatched.py --adaptive-jitters`
uses one jitter pass for clear matches and retries only borderline faces with
higher jitter. Compare its held-out FMR/FNMR against the default before using
it in an automatic workflow.

Run the optional AI experiment suite with:

```bash
./run_ai_matching_experiments.sh
```

This is review-only: it compares the available embedding caches, runs the
held-out calibration, and benchmarks exact FAISS retrieval when dependencies
are present. It refuses to run while face extraction or database rebuilding is
active, and writes timestamped results under `.cache/picorg/ai-experiments/`.

The optional local agent review is also report-only. It samples at most 50
uncertain items and asks a local Ollama-compatible vision model about technical
quality (face count, blur, pose, occlusion, and possible duplicates); it cannot
assign identities, write markers, change decisions, or move files. The default
provider is a no-network mock:

```bash
RUN_AGENT_REVIEW=1 \
AGENT_AUDIT=/path/to/face-clusters.json \
./run_ai_matching_experiments.sh
```

For explicit local Ollama inference, use `AGENT_PROVIDER=ollama` and a
loopback endpoint, for example
`AGENT_ENDPOINT=http://127.0.0.1:11434/api/chat` and
`AGENT_MODEL=gemma3:4b`. OpenAI-compatible OmniRoute endpoints are supported with
`AGENT_PROVIDER=openai-compatible`; remote endpoints are rejected unless
`AGENT_ALLOW_REMOTE=1` is explicitly set.

Face-group runs persist start/completion timing records in
`.cache/picorg/face-match-timings.jsonl` and print a prior-duration estimate at
startup. The estimate uses the median throughput of the five most recent
completed runs with matching database and quality settings; an unavailable or
unwritable timing log never stops matching. Override it with
`--timing-log PATH` or `PICORG_FACE_TIMING_LOG`.

Multi-face policy is identity-aware: a media item may be auto-assigned when
exactly one detected face clears the distance and margin gates and all other
faces remain unresolved. Items with two or more confident identities, or with
no confident identity, remain in the web review queue. A multi-face image is
not rejected merely because it contains additional unknown people.

To inspect a smaller quality-diverse reference gallery without changing the
database:

```bash
.venv/bin/python select_reference_gallery.py \
  --db /opt/photo_reorg/data/high_accuracy_faces.db \
  --max-per-person 12 \
  --min-quality 0.70 \
  --output /tmp/picorg-reference-gallery.json
```

The rebuild workflow now writes `.cache/picorg/reference-gallery.json` using
the canonical identities coalesced from the sorted tree plus Metadaily and
Redditdaily downloads. It keeps up to 24 quality-diverse exemplars per
identity (configurable with `MAX_EXEMPLARS_PER_IDENTITY`) and promotes only
confirmed review markers that carry a SHA-256 ahead of other references. The
optional `--verify-markers` selector flag rehashes them when a fresh integrity
check is required. The full
database remains available for auditability; pass the manifest to report-only
matching when you want the conservative exemplar subset:

```bash
.venv/bin/python face_group_unmatched.py \
  --audit /path/to/audit.json \
  --db .cache/picorg/face_database.sqlite3 \
  --gallery-manifest .cache/picorg/reference-gallery.json \
  --output /tmp/picorg-identity-candidates.json
```

For an existing photo_reorg database, `run_existing_face_db.sh` reuses a
prior dlib embedding cache when available.  Normal mode accepts only cache
records whose SHA-256 is present in the audit; if source paths are known to be
unchanged, `PICORG_TRUST_FACE_CACHE=1` reuses all matching path records and
avoids expensive decoder/stat calls.  This is report-only and never changes
the face database; disable it when files may have been replaced.

The full review pipeline uses a conservative identity candidate policy by
default: distance `0.45`, top-two margin `0.08`, quality-weighted ranking, and
adaptive jitter retries for borderline faces. Override these with
`IDENTITY_MATCH_THRESHOLD` and `IDENTITY_MATCH_MARGIN`; they affect review
candidate generation only and never bypass the live-apply safety gate.

Name-match results are cached persistently in `.cache/picorg/dry-run-cache.json`
and invalidated by source/catalog state changes. Re-running the name stage
reuses unchanged results instead of rescanning every file; override the
location with `PICORG_DRY_RUN_CACHE` when relocating the project.

Face embeddings can also be kept in the indexed SQLite store
`.cache/picorg/face_embeddings.sqlite3`. `run_existing_face_db.sh` imports the
legacy JSON cache once and queries the store by path; set
`PICORG_EMBEDDING_STORE` to relocate it. The JSON cache remains available as a
portable backup.

`run_existing_face_db.sh` refreshes the fingerprinted name audit by default and
uses an incremental matcher. It retains results whose audit fingerprint still
matches, decodes and matches only new or changed image paths, and writes newly
decoded single-face embeddings/statuses to the SQLite store. The matcher also
records a fingerprint of the known-identity gallery. When exemplars or
identities change, valid existing matches are retained while ambiguous,
unknown, no-face, and matches to removed identities are re-scored against the
new gallery using cached embeddings; the whole library is not re-embedded.
Its output reports `reused`, `pending`, `processed`, `gallery_changed`, and
`reprocessed_unresolved`. The default is to cover every unmatched image; set
`LIMIT_PER_CLUSTER` to a positive number only for a bounded pilot. This mode
never resets the identity database.

Unhashed legacy confirmations are never promoted automatically. Re-confirm
those images in the UI so their SHA-256 and current embedding can be recorded.

The interactive menu also exposes face-data maintenance: choose **6** to stop
an active rebuild, **7** for a clean face-data rebuild, or **9** for the full
accuracy run (face rebuild, latest-audit face matching, and LAN review UI).
Choose **a** for the report-only local-agent quality triage described above.
Choose **b** in the TUI, or click **Run image-level benchmark** in the web UI,
to build an image-disjoint held-out report from confirmed image decisions. This
action is review-only: it never moves files, changes the face database, or
changes review decisions. Its bounded status/log output is also available at
`GET /api/accuracy-benchmark` after starting it with `POST`.
The web UI's **Refresh identities** control rereads review identities and the
MD/RD catalog without restarting the server; use it after another process adds
or edits identity records.
The restart helper binds to `0.0.0.0` by default even if the shell exports an
unresolvable `HOST` value; set `PICORG_UI_HOST=127.0.0.1` for local-only access.
These actions ask for explicit confirmation because a clean rebuild can take
hours.

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

Write a report-only proposal for consolidating an existing sorted tree under
shared identity IDs. This command never moves media:

```bash
python3 picorg_sorter.py consolidation-manifest \
  --root /mnt/elements16/@mixedpics_sorted \
  --dest-root /mnt/elements16/@mixedpics_sorted \
  --output /tmp/picorg-consolidation-manifest.json
```

See [linked identity consolidation](docs/IDENTITY_CONSOLIDATION.md) for
registry field handling, collision statuses, and the deferred RD/MD migration.

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

Validate the opt-in online-evidence contract without making a network request:

```bash
python3 online_evidence.py \
  /tmp/picorg_reverse_search_queue.json \
  --provider mock \
  --output .cache/picorg/online-evidence.json
```

The mock provider writes one idempotent record per `(provider, query_sha256)`.
It stores hashes and metadata only, marks identity assignment as forbidden, and
never changes markers, decisions, or files. Network providers are intentionally
not enabled until their privacy, terms, calibration, and retention are reviewed.

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
`review_decisions.json`. “Assign selected to identity” and “Save typed identity
as new” move selected files into the canonical tree under
`PICORG_REVIEW_DEST_ROOT` (default `/mnt/elements16/@mixedpics_sorted`), with
collision-safe names. Pick existing identities from the assignment dropdown;
the separate “New identity” field creates a review identity and can move the
selected images in the same action. Protected `redditdaily` and `metadaily`
source roots are never moved.
Each assigned thumbnail displays its identity and a per-image **Confirm**
button; confirming records the image decision and moves that file. Assigning
selected images or creating a new identity is treated as confirmed immediately.
The assignment panel also provides **Undo last move**; move provenance is
stored in `review_move_history.json`, and an undone assignment returns to
`pending` rather than remaining confirmed.
Each assigned thumbnail also has **Unassign** (restore its prior location and
clear the assignment) and **Reassign** (select it for a new identity).
Use **Hide confirmed** in the thumbnail tools to focus the grid on pending
images; it is a display filter and does not delete or alter decisions.
Unassign and Reassign are grouped under each thumbnail’s `⋯` menu, while the
bulk toolbar remains available during scrolling. Keyboard shortcuts are
`Space` (select), `C` (confirm), `R` (reassign), and `U` (unassign).

The cluster-card **Approve** action prompts for the identity and an explicit
move confirmation, then records a confirmed cluster decision and moves every
file in the cluster into that identity folder. Use per-image assignment when a
cluster contains multiple people.
For large face clusters, the UI shows an additional warning before approval
because a large similarity group still needs image-level review. Filename-only
groups are not emitted as review clusters; names such as `FB_IMG` remain
supporting evidence only and never define cluster membership.
The API enforces the same purity acknowledgement (`purity_ack`) so scripted
clients cannot silently bypass this safeguard; inspect the sample thumbnails
before confirming a flagged cluster.
Face-derived groups are keyed by their stable `face_cluster_id`, so separate
face matches never merge merely because they share a provisional label. Small,
high-quality groups use `fbunknown###`; large or ambiguous groups use a neutral
`facegroup-*` title. Registry/name evidence annotates a face group but never
changes its membership.

The **Assorted folders** tab is a metadata-only inventory of person-like leaf
folders below `/mnt/assorted`. Select one or more folders, choose an existing
canonical identity, or enter a new local identity, then use **Save association
(no moves)**. Associations are stored in
`.cache/picorg/assorted-folder-associations.json` and the review ledger; the
source folders and every file in them remain untouched. Generic containers such
as `@attachments`, `#topic`, `old`, and `jokes` are excluded. Override the root
with `PICORG_ASSORTED_ROOT` or `--assorted-root` and the ledger with
`PICORG_ASSORTED_ASSOCIATIONS` or `--assorted-associations`.

The complete manual-identity promotion lifecycle, including scheduler job
boundaries, evidence requirements, rollback, and the shared-registry owner
approval boundary, is documented in
[`docs/IDENTITY_PROMOTION_WORKFLOW.md`](/opt/picorg/docs/IDENTITY_PROMOTION_WORKFLOW.md).

Confirmed image assignments also append marker candidates to
`identity_face_markers.json`. Build reusable, fingerprint-checked embedding
markers from the durable face cache with:

```bash
.venv/bin/python build_identity_face_markers.py \
  --embeddings .cache/picorg/20260818T115727Z.face-embeddings.json \
  --output .cache/picorg/identity-face-markers.json
```

Only assignments with `status=confirmed` are promoted into the embedding
marker output; pending/rejected decisions remain review evidence only.

Before relying on old review evidence, inspect its durability. This command is
read-only and flags confirmed markers whose source moved or lacks a hash:

```bash
.venv/bin/python audit_face_marker_health.py \
  --markers identity_face_markers.json \
  --output .cache/picorg/face-marker-health.json
```

Do not repair missing markers by basename alone; re-confirm them in the UI or
relink only with an independently verified SHA-256 match.

To audit whether face-only clusters remain pure after manual confirmation, use
the report-only checker below. It joins confirmed image/cluster-sample labels
to `face_cluster_id`, and can conservatively relink moved files by unique
basename. Ambiguous, missing, and conflicting labels are reported rather than
counted as correct:

```bash
.venv/bin/python face_cluster_purity_report.py \
  --audit .cache/picorg/audits/<timestamp>.face-clusters.json \
  --decisions review_decisions.json \
  --decisions review_image_decisions.json \
  --search-root /mnt/elements16/@mixedpics_sorted \
  --output .cache/picorg/audits/<timestamp>.cluster-purity.json
```

`valid: false` (and exit code 2) means no confirmed labels intersected the
audit. `comparable_to_default_strict_mode` is false for older reports that did
not record strict all-member clustering; those reports must not be used as
evidence for the current 0.90 review setting.

The review UI is served through Waitress by default (install
`requirements-review.txt` once). Keep port 8787 on a trusted LAN and do not
expose it directly to the internet. Clients on `192.168.2.0/24`, loopback,
and link-local networks are unrestricted by default, including assignments and
moves. While a face rebuild holds `/tmp/picorg-face-rebuild.lock`, the UI remains
browseable but automatically enters read-only mode. Identity assignments remain
available as report-only actions in either mode: they are written to
`.cache/picorg/pending-review-assignments.json`, the durable
`.cache/picorg/identity_evidence.sqlite3` assignment queue, and the append-only
review ledger, but do not move files, update face markers, or change normal decisions. All other
decision, identity-creation, undo, and move endpoints return HTTP 423 until the
rebuild finishes. Non-LAN clients must provide `PICORG_UI_TOKEN` (or `--token`); health
probes remain open. Set `PICORG_UI_TRUSTED_CIDRS` to replace the default
trusted networks. Set `PICORG_UI_AUTH=1` to require the token for LAN clients
too.
The cluster and identity views also read `identity_face_markers.json`, so
assignments applied later by `reconcile_confirmed.py --apply` remain visible and
hidden-confirmed filtering continues to work after a UI restart.
During a rebuild the page polls `/api/rebuild-status` and shows the current
stage, elapsed time, worker PID, and append-only repair-ledger counts. This is
bounded telemetry only; raw rebuild logs and source paths are not exposed.
`PICORG_UI_SERVER=flask` is a development-only fallback; never use it for a
LAN deployment. `PICORG_UI_ALLOW_UNAUTH_WRITES=1` is reserved for isolated
tests and local development.

The durable queue can be inspected at any time (the UI exposes the same data):

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from identity_evidence_store import list_assignment_queue
for item in list_assignment_queue(Path('.cache/picorg/identity_evidence.sqlite3'), ['pending', 'error', 'conflict']):
    print(item['assignment_id'], item['status'], item['identity'], item['path'])
PY
```

After the rebuild completes, review the queued work without changing anything:

```bash
.venv/bin/python reconcile_confirmed.py --pending-assignments .cache/picorg/pending-review-assignments.json --evidence-db .cache/picorg/identity_evidence.sqlite3
```

When the report is correct, apply the queued assignments (moves and marker
updates) explicitly:

```bash
.venv/bin/python reconcile_confirmed.py --apply --pending-assignments .cache/picorg/pending-review-assignments.json --evidence-db .cache/picorg/identity_evidence.sqlite3
```

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

Review clustering is broader than automatic identity assignment so the UI
shows candidate groups, not identity proof. `runweb.sh` now requires cosine
similarity of at least `FACE_CLUSTER_SIMILARITY=0.90` for faces to share a
cluster. The default `FACE_CLUSTER_STRICT_ALL_MEMBERS=1` additionally checks a
candidate against every existing member after representative shortlisting,
preventing bridge faces from creating mixed groups. The legacy
`FACE_CLUSTER_THRESHOLD` remains available for comparison runs, but is not a
percentage. These settings affect review grouping only and never authorize a
move or identity assignment. Set `FACE_CLUSTER_STRICT_ALL_MEMBERS=0` only for
an explicitly faster diagnostic run; `FACE_CLUSTER_MAX_REPRESENTATIVES`
controls the shortlist size.

When `USE_EXISTING_FACE_AUDIT=1` reuses a report, `runweb.sh` prints the
recorded threshold, coverage warnings, and the largest cluster. A report built
with a different threshold or containing very large groups should be rebuilt
before relying on its grouping; the UI remains image-level review only.
When clustering a known reference gallery, the InsightFace cache tool also
partitions by reference identity folder, so it cannot merge two registered
identities into one face group.

Direct extraction is single-worker by default. `runweb.sh` uses two bounded
dlib worker processes by default on the rotational media; the parent process
alone writes the embedding checkpoint:

```bash
./runweb.sh
```

Override with `PICORG_FACE_WORKERS=1` for a damaged or network-backed source,
or use the equivalent CLI option `--workers 2`. Values are capped at eight;
four workers provided only a marginal improvement in the local benchmark.
InsightFace remains single-process for now.

For the complete name+face reconciled workflow, use `runweb.sh` after installing
the optional dependency once:

```bash
INSTALL_FACE_DEPS=1 ./runweb.sh
```

For a single safe command that runs the dry-run pipeline, builds face groups,
and starts the Waitress review UI on all LAN interfaces:

```bash
./run_face_review_pipeline.sh
```

It defaults to `HOST=0.0.0.0`, `PORT=8787`, skips `photo_reorg`, and never
applies organizer moves. Override `PORT` or set `HOST=127.0.0.1` for local-only
access. If the chosen port is busy, launchers pick the next free port (see
`picorg_resolve_port.sh`). Port 8787 was reclaimed after Readarr retirement.
Install the UI server dependency first:

```bash
.venv/bin/pip install -r requirements-review.txt
```

Set `PICORG_UI_TOKEN` for remote access; clients send `X-Picorg-Token` (or a
Bearer token). LAN clients do not need the header unless `PICORG_UI_AUTH=1` is
set.

The normal pipeline ingests completed downloads and performs a complete face
reference/database rebuild before matching. Use the explicit reuse shortcut
only when you intentionally want to avoid extraction:

```bash
./run_face_review_pipeline.sh --reuse-face-db
```

To explicitly enable live high-confidence processing:

```bash
./run_face_review_pipeline.sh --apply-high-confidence
```

This first creates a fingerprinted dry-run audit and passes the proposed name
moves through a precision/non-empty high-confidence gate before applying only
those unchanged high-confidence name matches (`>=0.95`). The broader
production-readiness gate still requires recall and held-out face benchmarks.
It then
rebuilds the selected face backend's database from the already sorted tree plus the
Metadaily and Redditdaily download trees, matches remaining audit items against
that database, and finally builds review-only face groups and starts the LAN UI.
The default roots are `/mnt/elements16/@mixedpics_sorted`,
`/mnt/elements16a/Pron/metadaily/downloads`, and
`/mnt/elements16a/Pron/redditdaily/downloads`; override the colon-delimited
`REFERENCE_ROOTS` when needed. These are face-reference inputs only; protected
MD/RD source trees are never modified.

The compatibility default remains `FACE_DATABASE_BACKEND=photo_reorg` while the
PicOrg-owned backend is benchmarked. Set `FACE_DATABASE_BACKEND=picorg` to use
PicOrg's bounded dlib extraction and atomic SQLite writer; it preserves the
`face_encodings`/`person_metadata` schema so existing match readers continue to
work. Do not switch automatic moves to it until its held-out results match or
exceed the current backend.
Review the dry-run results before enabling this mode; it is the only mode that
moves files. Override `REFERENCE_ROOTS` and `FACE_DB` when your canonical tree
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
queue, not identity confirmation: every usable face in a multi-face image is
evaluated and retained as evidence, but the image is grouped only when exactly
one face clears the identity distance and margin gates; otherwise it remains
review-only.

Unreadable files can be permanently excluded before preflight touches the
filesystem. Add absolute paths to
`.cache/picorg/skip_paths.json` (or point `SKIP_PATHS` at another JSON file); the
entry is recorded as `skipped` and is excluded from face extraction and review.
and no face result is exported automatically. Within a cluster, select several
images and use “Assign selected to identity”; type a new identity and use “Save
typed identity as new” to record it in the separate review identity ledger.

### Face extraction speed

Extraction is resumable and now skips files already classified by preflight as
missing, corrupt, unsupported, or oversized. Keep `num-jitters=1` for normal
operation, reuse the embedding cache, and benchmark `FACE_BACKEND=insightface`
separately; the current CPU benchmark was about 2.2× faster than dlib, but it
requires independent accuracy and model-license approval.
If a file disappears between preflight and extraction, it is counted as
`missing` rather than a model error and skipped cleanly.
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
# apply only the reviewed fingerprinted audit selected above
./picorg_manual.sh apply --audit-input /path/to/reviewed-audit.json
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

For cleaner calibration, prefer individually confirmed image decisions; this
avoids treating a mixed face cluster as a single identity:

```bash
python3 build_face_pairs.py \
  --decisions review_decisions.json \
  --image-decisions review_image_decisions.json \
  --embeddings /path/to/face-embeddings.json \
  --output /tmp/picorg-image-labelled-pairs.json
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
  --model-license-status research-only \
  --output /tmp/picorg-face-calibration-uniface.json
```

If review decisions refer to files that were moved after the audit, add one or
more `--search-root` options. Only a unique basename is relinked; ambiguous or
missing files are excluded and the command exits nonzero when no genuine and
impostor pairs remain:

```bash
  --search-root /mnt/elements16/@mixedpics_sorted
```

This is benchmark-only until a sufficiently large held-out set confirms error
bounds and the individual model licenses are approved. UniFace itself is MIT,
but its bundled model choices have separate licenses; do not treat a good small
pair score as production evidence.

The optional experiment suite can run the same AdaFace test without changing
production state. Set `RUN_UNIFACE_BENCHMARK=1`; use
`UNIFACE_SEARCH_ROOT=/mnt/elements16/@mixedpics_sorted` when the pair manifest
contains paths that were moved after the audit:

```bash
RUN_UNIFACE_BENCHMARK=1 \
UNIFACE_SEARCH_ROOT=/mnt/elements16/@mixedpics_sorted \
./run_ai_matching_experiments.sh
```

### Face-database backend parity

The legacy photo_reorg database remains the default. Before opting into the
PicOrg-native compatible database, compare both embedding caches on the same
image-disjoint held-out pairs:

```bash
.venv/bin/python backend_parity_benchmark.py \
  --pairs /path/to/heldout-pairs.json \
  --legacy-embeddings /path/to/legacy-embeddings.json \
  --picorg-embeddings /path/to/picorg-embeddings.json \
  --output .cache/picorg/backend-parity.json
```

The report must meet the same minimum pair and confidence-interval gates used
by `production_readiness.py`. Only then should a deployment explicitly set
`FACE_DATABASE_BACKEND=picorg`; otherwise keep the default `photo_reorg`.

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
the audit, including an audit SHA-256, backend/model/detector, threshold,
license-status, cache root, counts, and measured accuracy.
These manifests are suitable for local OpenTelemetry/Langfuse ingestion while
keeping image paths and image contents out of remote services.

The live review snapshot is published atomically at
`.cache/picorg/current-run.json`.  It points to the primary audit, face audit,
and face-only reconciled audit and records hashes for each, so the UI no longer
has to select a run by directory mtime.  SQLite remains the operational
authority; the pointer and dated JSON files are immutable provenance/recovery
artifacts.  A valid pointer is used automatically by `runweb.sh` and the
standalone UI; a changed or incomplete artifact is rejected.

Old manifest-backed snapshots can be reviewed before removal with:

```bash
.venv/bin/python prune_run_artifacts.py --keep 30 \
  --ledger review_decision_ledger.jsonl \
  --ledger review_decisions.json
```

The command is a dry run by default.  Only add `--apply` after checking its
candidate list.  The current run, the newest retained runs, runs mentioned in
review ledgers, and legacy audits without manifests are never removed.

`uv.lock` pins the complete dependency graph and hashes for every optional
environment. `requirements-locked.txt` is the equivalent all-extras export
for hash-checked pip installs; verify that both files are synchronized with:

```bash
./verify_dependency_lock.sh
```

The script is read-only apart from its temporary comparison file. Use
`uv sync --frozen --extra review` (plus any required face extras) for a
project-managed environment, or `.venv/bin/pip install --require-hashes -r
requirements-locked.txt` when pip is required. Run `pip-audit` after deliberate
dependency changes.

FAISS exact retrieval is available only as an isolated benchmark (`--extra
retrieval`); it is not enabled in the production matcher until full-gallery
top-k parity and accuracy gates pass:

```bash
UV_CACHE_DIR=/tmp/picorg-uv-cache uv sync --frozen --extra retrieval
PYTHONPATH=. .venv/bin/python faiss_exact_benchmark.py \
  --embeddings .cache/picorg/face-embeddings-native.json \
  --limit 5000 --queries 256 --top-k 10
```

The latest full-cache benchmark showed exact top-10 parity and faster search,
but cache loading and face extraction still dominate end-to-end time. Keep the
current verifier and cache path until a persistent index is profiled against a
representative repeated workload.

An identity-level exact benchmark against the active 29,106-reference
database retained 256/256 top-10 rankings and measured ~4.3× faster search
than the current Python scan. This remains an opt-in benchmark; no index is
used for assignments yet.

In a spread-across-gallery probe, bounded retrieval needed about 2,048 vector
candidates to preserve all 64 sampled identity top-10 rankings; 256 and 512
candidates dropped to 39/64 and 59/64. Treat this as a tuning lead only and
keep the current exact verifier until a larger image-disjoint evaluation is
complete.

The image-disjoint reviewed set currently has 169 usable query images. With
`retrieval_k=2,048`, FAISS preserved all 169 identity top-10 lists and reduced
ranking time by about 52.6×. This remains a candidate-retrieval benchmark, not
an authorization to bypass the calibrated verifier or safety gate.

A larger 512-query spread sample from the confirmed image ledger also retained
512/512 identity top-10 lists at `retrieval_k=2,048`, with ~62.4× faster
ranking. This remains a pre-integration parity result, not a production
authorization.

The deferred atomic index/manifest design is documented in
[`docs/FAISS_INDEX_MANIFEST.md`](/opt/picorg/docs/FAISS_INDEX_MANIFEST.md).
The implementation scaffold in
[`faiss_candidate_retriever.py`](/opt/picorg/faiss_candidate_retriever.py)
validates gallery metadata and falls back to the complete current reference
set when FAISS is unavailable or incompatible; it is not wired into default
matching.

Check whether a specific run meets all automatic-move gates:

```bash
.venv/bin/python production_readiness.py \
  --audit /path/to/audit.json \
  --preflight /path/to/audit.preflight.json \
  --benchmark /path/to/face-calibration.json \
  --markers .cache/picorg/face-marker-health.json \
  --ui-token "$PICORG_UI_TOKEN"
```

The default benchmark gate requires at least 100 genuine and 100 impostor
pairs and 95% confidence-interval upper bounds no higher than 1% for both FMR
and FNMR. When `--markers` is supplied, all confirmed markers must also have a
durable SHA-256. Override those values only with documented evidence.

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

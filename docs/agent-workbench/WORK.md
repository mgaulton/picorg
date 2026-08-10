# Work: picorg

work_id: picorg
updated: 2026-07-31
agent_last: codex
spec_kit: <optional — link or path to spec/plan/tasks when multi-step>

## Goal

Build a deterministic organizer that can resolve mixed Reddit media into canonical identity folders, support dry-run matching, report accuracy, and provide a repeatable periodic workflow with a repo-local overlay registry.

## Spec (what — Spec Kit)

User-facing requirements only. No implementation detail.

- Problem / user story
- Acceptance criteria (testable)
- Out of scope

## Plan (how — Spec Kit)

Technical approach after spec is stable.

- Architecture / components touched
- Data flow or API contracts
- Risks and mitigations

## Tasks (do — Spec Kit)

Ordered, checkable items. One agent turn ≈ one task when possible.

- [ ] Task 1
- [ ] Task 2

## State

- Finished: `picorg_sorter.py` implements identity catalog loading, dry-run matching, manifest export, and optional apply mode.
- Finished: Reddit-context parsing now boosts explicit `r/` and `u/` markers plus title hints.
- Finished: apply mode preserves intact folders where possible and routes exact duplicate folders into `duplicates/`.
- Finished: `README.md` documents sources, usage, and current dry-run metrics.
- Finished: `RUNBOOK.md`, `OPERATING_POLICY.md`, and `picorg_manual.sh` provide the manual operator workflow and confidence rubric.
- Finished: `project_registry.json` keeps repo-local alias overrides and blocked generic tokens separate from ingested lists.
- Finished: dry-run across `/mnt/elements16/@mixedpics`, `/mnt/elements16a/Pron/jdownloaderscomplete`, and `/mnt/desktop/Pictures` completed with proxy accuracy 1.0.
- Finished: benchmarked the available reviewed image pairs (2 genuine, 1 impostor); selected threshold 0.4734 with zero observed FMR/FNMR, but sample size is insufficient for production calibration.
- Finished: expanded all available confirmed review samples to 66 pairs (31 genuine, 35 impostor); selected threshold 0.5638 with zero observed FMR/FNMR. Wilson 95% upper bounds remain 11.0% FNMR and 9.9% FMR, so this is not production-grade evidence.
- Health: pytest with plugin autoload disabled reports 33 passed, 2 failed because face extraction imports an unavailable optional dependency before cache-only paths can be reused; the durable embedding cache reports 47.7% extraction errors (mostly missing/invalid inputs).
- Fixed: face-recognition import is now deferred until an uncached image requires extraction; isolated pytest now passes 35/35.
- Fixed: resume wrappers now have Bash shebangs and forward arguments; all repository shell scripts pass `bash -n` and ShellCheck.
- Fixed: added `pytest.ini`, pinned `requirements-test.txt`, and executable `run_tests.sh` that isolates global plugins and prefers `.venv`; wrapper test run passes 35/35.
- Fixed: added `.github/workflows/quality.yml` to run isolated tests, Bash syntax checks, and ShellCheck on pushes and pull requests; YAML parses successfully.
- Added: `media_preflight.py` classifies unmatched paths non-destructively; `runweb.sh` runs it before face extraction and writes `*.preflight.json`; test suite now passes 37/37.
- Added: face-cluster reports include bounded preflight counts and benchmark reports accept `--preflight` coverage context; test suite now passes 38/38.
- Added: benchmark preflight metadata has regression coverage and README guidance; full suite now passes 39/39.
- Added: benchmark selected operating points now include Wilson 95% FMR/FNMR intervals, preventing zero-error small samples from appearing definitive.
- Added: `build_face_pairs.py` creates deterministic labeled pairs from confirmed decisions; regression coverage brings the suite to 40/40.
- Cleaned: removed a duplicate `hashlib` import; compile, diff, and test checks remain clean.
- Added: `run_face_review_pipeline.sh` runs the dry-run intake and launches the LAN-bound review UI without applying moves; audit selection now excludes derived reports; suite remains 40/40.
- Added: `run_face_review_pipeline.sh --apply-high-confidence` explicitly runs photo_reorg live, picorg's >=0.95 apply path, then face grouping and LAN review; default mode remains non-mutating.
- Corrected: high-confidence mode now applies picorg name matches first, rebuilds photo_reorg references from `REFERENCE_ROOT`, runs reference matching, then launches review grouping; default remains non-mutating.
- Corrected: reconciliation now treats face clusters as review membership and name clusters as supporting context, preventing name-only paths from merging distinct face groups; suite remains 40/40.
- Added: `dedupe_priority.py` hashes priority source trees first and reports/quarantines exact duplicates from mixedpics/Desktop without modifying protected downloads; suite passes 41/41.
- Added: isolated optional `insightface_backend.py` (SCRFD/ArcFace via ONNX Runtime) plus bounded requirements and capability tests; default dlib behavior is unchanged; suite passes 43/43.
- Ran: InsightFace `buffalo_l` CPU A/B benchmark on the current 66 pairs; all 12 images embedded and 66 pairs scored with zero observed errors; selected distance threshold 1.0115. This matches dlib's zero-error result but does not establish superiority with only two identities.
- Added: `insightface_pair_benchmark.py` reproduces the A/B evaluation with model/status metadata; regression suite now passes 44/44.
- Added: `pipeline_safety_gate.py` blocks live apply below configurable precision/recall gates (default 0.99); priority dedupe now persists a metadata+SHA-256 cache for incremental rescans; suite passes 46/46.
- Tightened: verified preflight now detects corrupt/oversized images, and high-confidence reference rebuilds fail closed when photo_reorg lacks `face_recognition_models`; suite passes 47/47.
- Added: `face_cluster_unmatched.py` and `runweb.sh` now support explicit `FACE_BACKEND=dlib|insightface` with model-specific caches; dlib remains the default; suite passes 47/47.
- Added: `pyproject.toml` defines reproducible uv/optional face environments; Promptfoo dataset/provider scaffolding exports confirmed pairs locally; review runs write privacy-preserving SHA-256 manifests; suite remains 47/47.
- Added: optional `PICORG_UI_TOKEN`/Bearer authentication for LAN review UI; health probes remain open; suite passes 48/48.
- Added: `production_readiness.py` combines accuracy, preflight, and LAN-auth gates into an explicit automatic-move pass/fail; suite passes 50/50.
- Added: Pillow decompression-bomb warnings are treated as hard preflight/decoder failures; readiness also requires explicit InsightFace model-license confirmation; suite passes 52/52.
- Tightened: production readiness now requires a held-out benchmark, minimum 100 genuine/100 impostor pairs, and ≤1% 95% CI upper bounds for FMR/FNMR; suite passes 53/53.
- Added: deterministic image-disjoint `split_face_pairs.py` prevents train/evaluation image leakage; suite passes 54/54.
- Relevant paths: `picorg_sorter.py`, `README.md`, `RUNBOOK.md`, `OPERATING_POLICY.md`, `picorg_manual.sh`, `project_registry.json`, `/tmp/picorg-dry-run.json`

## Next (ordered)

1. Expand validated identity/source coverage to raise recall above 0.99; keep apply limited to confidence >= 0.95.
2. Run `picorg_sorter.py manifest` if you want a persisted source map.
3. Expand the source registry if new follow/friends lists appear.

## Decisions

- 2026-07-08: Use a single canonical destination tree with family subfolders and source-aware alias resolution.
- 2026-07-08: Treat follow/friends/subreddit lists as additional identity sources, but keep conservative matching and quarantine unmatched caption-only files.
- 2026-07-08: Preserve intact folders where possible and route exact duplicate folders into `duplicates/`.
- 2026-07-08: Keep local alias exceptions and blocked generic terms in `project_registry.json`, not in ingested source files.
- 2026-07-09: Use `OPERATING_POLICY.md` as the authoritative confidence rubric and manual workflow guide.

## Blockers / failed paths

- Do not retry: …

## Pointers (do not paste large logs here)

- Handoff export: `tools/agent-workbench-export.sh <repo>`
- Structure: gitnexus MCP or `.gitnexus/`
- Skeleton: `.rtt/context.txt`
- Fleet memory: `AI_MEMORY_ROOT` + `ai_memory_cli.py memory status`

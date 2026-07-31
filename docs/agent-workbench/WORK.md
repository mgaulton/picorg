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

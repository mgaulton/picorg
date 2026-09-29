# AGENTS.md baseline

This file is the portable baseline for a project that does not yet have
project-specific agent instructions. The containing project directory is the
scope; preserve its existing architecture and follow its README and manifests.

## Goal

Maintain and improve the containing project without inventing duplicate
capabilities. Inspect the current implementation, direct dependencies, scripts,
tests, and deployment files before making changes. Apply the shared rules in
`/OneDrive/AI-Memory/AGENTS.md` when available.

## Setup commands

- Read `README.md`, project documentation, and existing configuration first.
- Use the repository's existing package manager, virtual environment, and
  lockfiles; do not add dependencies or services without a demonstrated gap.
- Keep production, container, and network changes scoped to an explicit task.

## Code style

- Follow the existing language, formatter, lint configuration, and module
  boundaries. Prefer small, testable extensions over rewrites.
- Preserve stable APIs, data formats, logging, error handling, and security
  defaults unless the task requires a compatible change.

## Testing instructions

- Run the narrowest existing tests, type checks, lint, build, or configuration
  validation that covers the change.
- If no test runner is present, perform a bounded syntax/configuration smoke
  check and record the limitation; do not claim untested behavior is verified.

## Key files

- `README.md` and the containing project's package/dependency manifests.
- Existing entrypoints, configuration, scripts, tests, and deployment files.

## Project-specific rules

- Preserve user work and unrelated changes; inspect before editing.
- For a new dependency or external project, record the capability gap, reuse
  alternatives considered, security/license/maintenance fit, and rollback.

## PR instructions

- Summarize the smallest reversible delta, verification performed, and any
  follow-up or deployment requirement. Never commit secrets.

<!-- AGENT_WORKBENCH_AGENTS:BEGIN -->
## Agent workbench (autonomous handoff)

- **State:** `docs/agent-workbench/WORK.md` + `docs/agent-workbench/handoff.json`
- **Export:** `.cursor/last-workbench-export.txt` (regenerated on session start / chat rotate)
- **Do not** ask the operator to paste handoff context; read the files above first.
<!-- AGENT_WORKBENCH_AGENTS:END -->

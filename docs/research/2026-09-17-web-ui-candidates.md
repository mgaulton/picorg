# PicOrg web UI candidate evaluation

## Decision

Keep the current Flask/Waitress server and browser API. The UI owns review
state, assignment queues, audit IDs, protected media access, and LAN policy;
those boundaries should not move into a new client framework. Apply native
browser rendering improvements now and run a feature-flagged virtualization
pilot before adding a frontend build toolchain.

## Current baseline

- `review_ui.py` serves the HTML/CSS/JavaScript and JSON/media endpoints.
- `runweb.sh` uses Waitress for the production LAN process.
- The browser currently pages clusters at 50, loads cluster members in bounded
  batches, uses lazy media loading, and stores decisions through the existing
  audit/SQLite/JSON paths.
- A Docker-CDP smoke test verified a 101-image cluster and modal navigation
  after the renderer-freeze fix on 2026-09-17.

## Candidates

### TanStack Virtual — Test next

Canonical sources: <https://tanstack.com/virtual/latest/docs/introduction>,
<https://github.com/TanStack/virtual>.

The headless virtualizer supports list, grid, horizontal, and measured layouts,
including framework-agnostic core packages. It directly addresses future
clusters with hundreds or thousands of members by keeping only visible tiles in
the DOM. The project is active, MIT-licensed, and publishes signed releases.

Integration: add a pinned local browser bundle behind `PICORG_UI_VIRTUAL_GRID`
and keep the current Flask API. Do not move assignment or path validation into
the client. Required fixture: a synthetic 2,000-member cluster with mixed
image/video/error states. Pass when the DOM remains below 250 tiles, selection
and modal navigation preserve all paths, and first interaction remains below
500 ms. Rollback is disabling the flag.

Complexity: Medium. Owner: PicOrg UI. Deployment: isolated pilot only.

### HTMX 2.x — Test only for status/settings

Canonical sources: <https://htmx.org/docs/>,
<https://github.com/bigskysoftware/htmx/releases>.

HTMX provides server-rendered partial updates, browser history integration, and
optional SSE/WebSocket extensions. It is active and Zero-Clause BSD licensed.
It could simplify scheduler/rebuild status fragments and settings forms, but
it does not solve the stateful image grid, modal navigation, selection model,
or virtualized rendering.

Integration: a self-hosted, pinned copy may be tested only on the Settings and
status panels. Keep the existing JSON APIs and polling as fallback. Do not use
CDN assets or replace the review grid. Complexity: Low/Medium. Deployment:
isolated pilot. Recommendation: Test narrowly, otherwise reject for the grid.

### Lit — Monitor / future

Canonical sources: <https://lit.dev/docs/v3/>,
<https://github.com/lit/lit>.

Lit supplies lightweight reactive Web Components with BSD-3 licensing and
incremental adoption. It is a reasonable way to split the current monolithic
inline script into `picorg-cluster-list`, `picorg-media-grid`, and
`picorg-review-modal` components. It still requires a browser build and does
not replace virtualization, path safety, or API design.

Recommendation: defer until the virtual-grid pilot demonstrates that the UI
needs component boundaries. Complexity: Medium/High.

### React 19 + Vite + TanStack Virtual — Defer

Canonical sources: <https://react.dev/versions>,
<https://github.com/react/react>.

React is healthy, MIT-licensed, and has a large ecosystem, but a migration
would introduce a Node build/deployment path, duplicate the existing Flask
rendering/state behavior, and create a large regression surface for assignment
and media safety. It is justified only if PicOrg becomes a multi-page product
with several independent teams or clients.

Complexity: High. Deployment: not installed. Recommendation: defer.

### Streamlit/Gradio — Reject

These are useful for prototypes, but they do not fit PicOrg's protected media
allow-list, audit ledger, queued assignments, URL-preserved review state, or
LAN mutation policy without replacing the existing API boundary. They would be
an operational regression, not an improvement.

## Implemented now

- Keep Flask/Waitress and all existing API/security boundaries.
- Use native `content-visibility`/containment for media tiles so off-screen
  tiles do not consume the full rendering cost.
- Keep lazy image decoding, bounded member batches, and modal async media
  assignment.
- Keep React/Lit/HTMX out of production until their isolated fixtures pass.

## Pilot acceptance and rollback

The pilot must be self-hosted, pinned, and run without production credentials.
Browser-CDP checks must cover: cluster open, all-member loading, select all/none,
identity assignment, undo, modal Previous/Next, unavailable media, URL refresh,
and read-only mode during rebuild. A failed pilot is rolled back by removing
the static bundle and disabling `PICORG_UI_VIRTUAL_GRID`; the Flask UI remains
the production path.

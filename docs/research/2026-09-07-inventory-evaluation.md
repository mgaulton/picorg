# AI Agent Resource Inventory evaluation for PicOrg

## Question

Which resources in the canonical Google-Drive inventory materially fit PicOrg
without replacing its deterministic, registry-first photo workflow or changing
the existing AI-Memory, routing, memory, or security authorities?

## Canonical inventory and provenance

The entire native Google Doc was read before evaluation:

- **Title:** AI Agent Resource Inventory and Architecture Report — Working Master
- **Document ID:** `1xOO2NEAvniwnv6p2pdshsWqChB5kkhFZTvZq1RDvKsA`
- **URL:** <https://docs.google.com/document/d/1xOO2NEAvniwnv6p2pdshsWqChB5kkhFZTvZq1RDvKsA/edit>
- **Revision read:** `ANLCKQm2AG7syllFDrJ8INrYjWjTaJt3r21CZTTyrDkjPx9MBS1UyJakqaQv0F0yiepheTWRtGLRLm3SmW57Nqm5_0FOQaziBtmLZAlkvJY`
- **Size read:** 987 paragraphs / 82,237 characters

Resource IDs and historical claims below are preserved as written. “Current
fact” is separately verified against this checkout or an authoritative upstream
source. No resource was installed, activated, or granted credentials.

## Project boundary and current implementation

PicOrg (`/opt/picorg`) is a deterministic organizer, not an autonomous agent
platform. Its pipeline is intake → priority hash dedupe → registry/name audit →
optional safety-gated name apply → read-only reference coalescing → face
embedding/database build → identity candidate matching and strict face-only
clustering → append-only review decisions/markers → Waitress LAN review UI.

Directly related systems are:

- `/opt/photo_reorg`: current default face-database builder/legacy SQLite
  consumer; PicOrg also has a native builder and parity/evaluation tools.
- `/opt/metadaily`: authoritative local identity/alias registry and profile
  evidence source (`/opt/shared/identity_aliases.json`; the former
  `/opt/metadaily/data/identity_aliases.json` location is legacy).
- `/opt/redditdaily`: protected download store and PostgreSQL identity/source
  metadata; its README states the license is commercial.
- `/opt/move_downloads_remote.sh`: controlled intake mover.
- `/opt/omniroute`, `/opt/ollama`, and `/opt/ai-memory`: existing fleet-local
  AI/routing/memory authorities, not PicOrg-owned dependencies.

Observed strengths: protected MD/RD roots, fingerprinted audits, SHA/marker
relinking, dry-run and safety gates, atomic database promotion/recovery,
checkpointed extraction, generic-token filtering, review ledger/undo paths,
Waitress production serving, LAN-default policy with optional remote token, and
substantial unit/integration tests. The selected PicOrg tests passed (production
readiness, safety gate, face clustering, review UI: all green; only the known
`pkg_resources` deprecation warning appeared).

Material gaps: benchmark labels remain too small/uneven for a universal face
threshold; unreadable media and removable-drive errors remain operational
inputs; the custom pipeline has no independent AI-regression harness or
centralized trace/cost view; the review UI is not a browser-agent dependency;
and adding an agent/orchestration layer would risk turning deterministic moves
into opaque, non-reproducible decisions.

## Existing PicOrg capabilities already covered by inventory

| Inventory IDs | Existing coverage | Remaining gap |
|---|---|---|
| 028, 035–037 | Task-scoped scripts, project registry, audit/manifest provenance, pinned requirements, safety gates | Formal machine-readable approved-tool/skill records and review dates are not a PicOrg runtime dependency. |
| 047–052 | Dry-run/apply separation, human review, audit ledger, atomic DB rebuild, recovery wrapper, bounded workers, LAN auth policy | A single release checklist should assert all controls; credential scope and event taxonomy should be documented consistently. |
| 053, 055, 110, 114 | Source paths and audit IDs are retained; external titles are treated as data; strict JSON contract in `agent_review.py`; repo AGENTS/workbench rules | Preserve evidence/authority labels when any future LLM review is added. |
| 058, 080 | RedditDaily/Metadaily are already the protected identity/source authorities | Registry freshness/alias conflicts need explicit audit output, not broader fuzzy matching. |
| 091, 092, 106 | AI-Memory, Ollama, and OmniRoute exist elsewhere in the fleet | PicOrg must not create a second memory/router authority or send media off-host by default. |
| 112 | UI/release checks are partly covered by tests and runbooks | Add an explicit browser smoke checklist only after the UI contract stabilizes. |

## Candidate evaluations

### Implement Now (no new third-party dependency)

#### IDs 028, 035, 036, 037 — task-scoped tools, approved registries, provenance

- **Category/source:** architecture/security patterns; inventory-derived, not a
  software package.
- **Historical claim:** load only required tools; keep version-pinned skills and
  MCPs with source, hash, license, permissions, owner, approval, and review date;
  quarantine discoveries before production.
- **Current fact:** PicOrg already has bounded scripts, `project_registry.json`,
  audit/run-manifest files, pinned optional requirements, and no production MCP
  dependency. This complements rather than replaces the existing design.
- **Gap/benefit:** formalizes provenance and prevents an LLM or future tool from
  bypassing protected roots and human approval.
- **Security/privacy:** least privilege, no media upload, no secret in prompts;
  treat downloaded skills and Reddit/web text as untrusted data.
- **License/cost/health:** pattern, no license or runtime cost; healthy if kept
  as local policy.
- **Compatibility/location:** `project_registry.json`, audit manifests,
  `OPERATING_POLICY.md`, `run_picorg.sh`, `runweb.sh`, and review ledger.
- **Complexity/status/deployment:** Low / **Adopt** / Project-local policy;
  already partially implemented.
- **Fixture/acceptance/rollback/owner:** fixture with one protected MD/RD path,
  one generic token, one candidate tool, and one missing file; acceptance is
  deterministic rejection plus provenance record; rollback is deleting the
  proposed policy-only record; owner is PicOrg maintainer.

#### IDs 047–055 — staging, human approval, logging, rollback, budgets, credentials, provenance, injection boundary

- **Category/source:** security and reliability architecture patterns.
- **Historical claim:** isolate tests, require human production approval, log
  tool/privilege/deployment/auth/version events, maintain tested rollback,
  bound cost/concurrency, broker scoped credentials, retain research provenance,
  and never treat retrieved text as instructions.
- **Current fact:** PicOrg already separates dry-run/apply, has a precision
  safety gate, atomic rebuild/recovery, audit/marker ledgers, bounded face
  workers, and LAN token policy. The UI can mutate media only through audited
  endpoints; remote clients are token-gated by default.
- **Gap/benefit:** consolidate these controls into one release acceptance record;
  add explicit resource/error counters and a documented last-known-good audit/DB
  pointer before any autonomous helper is considered.
- **Security/privacy:** especially important because face embeddings and sexual
  media are sensitive; do not emit image bytes, tokens, or full paths to cloud
  services.
- **License/cost/health:** patterns, no runtime dependency; healthy.
- **Location/complexity/status:** `OPERATING_POLICY.md`, runbooks, safety gate,
  recovery wrapper, `review_ui.py`; Low / **Adopt** / Project-local.
- **Fixture/acceptance/rollback/owner:** simulated missing source, corrupt image,
  interrupted apply, unauthorized remote request, and failed DB validation; all
  must fail closed and preserve previous DB/audit; rollback is existing atomic
  DB/audit restore; owner PicOrg maintainer.

#### ID 110 — AGENTS.MD context hygiene

- **Historical claim:** keep always-on instructions short/current and move durable
  knowledge into project docs/memory.
- **Current fact:** `/opt/picorg/AGENTS.md`, workbench files, README/RUNBOOK and
  existing research docs already implement this direction.
- **Recommendation:** **Adopt**, Low, project-local documentation only; no new
  agent package. Acceptance: no duplicate authority for identity or memory.

#### ID 112 — web release quality checklist

- **Historical claim:** verify rendered behavior, errors, titles/meta, accessibility,
  console errors, and performance before release.
- **Current fact:** the UI has focused tests but no full browser smoke/release
  checklist. This is directly relevant to the review UI, not to face scoring.
- **Recommendation:** **Adopt**, Low, project-local QA. Fixture: a synthetic
  audit with confirmed/hidden/unreadable/multiface items. Acceptance: all states,
  modal navigation, assignment/undo, LAN health, and refresh URL state pass;
  rollback is test-only/no production mutation.

#### ID 114 — structured prompt contract pattern

- **Historical claim:** role → task/success criteria → relevant context →
  constraints → output contract; never require private chain-of-thought.
- **Current fact:** `agent_review.py` already uses a report-only, strict JSON
  contract and explicitly disallows identity/move fields.
- **Recommendation:** **Adopt** as a documentation rule for future local AI
  quality review; Low; no dependency. Acceptance: malformed/identity-bearing
  model output is rejected and cannot alter decisions.

### Test Next (isolated, pinned, no production credentials)

#### 118 — Promptfoo

- **Category/canonical source:** agent/LLM evaluation and red-teaming;
  <https://github.com/promptfoo/promptfoo>.
- **Historical claim:** critical, repeatable prompt/agent/RAG regression and
  red-team gate.
- **Current verified fact:** official repository describes CLI/library evals,
  red-teaming, CI integration, local execution, and MIT licensing; current
  releases include `0.122.2` (2026-08-28). Current docs require Node `>=22.22.0`
  (Node 24 LTS recommended). It is now part of OpenAI, which is a governance
  and supply-chain consideration, not an approval.
- **Relevance/gap:** useful only for the optional `agent_review.py` quality
  observer and future structured AI assistance; it cannot validate face identity
  accuracy or replace deterministic thresholds.
- **Complements/replaces:** complements pytest, face-match benchmarks, and the
  strict JSON observer; replaces ad-hoc prompt regression scripts only if a
  measurable fixture proves value.
- **Benefits/drawbacks:** declarative CI regression and red-team cases; adds a
  Node runtime and another dependency/telemetry surface, and LLM-judge variance.
- **Security/privacy/license/cost:** MIT; run local with synthetic or redacted
  metadata, never upload images or production tokens; Node supply chain must be
  locked and audited; software is free, model/provider costs are not.
- **Health:** Healthy/active (9,563 commits, current releases and active issue/PR
  traffic in the official repository).
- **Compatibility/location:** `evals/promptfoo_face_provider.py` and
  `agent_review.py`; High integration complexity because the project is Python
  and has no Node lockfile.
- **Status/deployment:** **Test** / Not installed.
- **Fixture/acceptance/rollback/owner:** 50–100 synthetic uncertain-item JSON
  fixtures plus adversarial malformed outputs; pin `0.122.2` and Node 24 in an
  isolated test job; pass if schema rejection is 100%, no identity/move field is
  accepted, and repeated scoring is within a pre-set variance; remove the test
  job/config to roll back; owner PicOrg maintainer.

#### 119 — Langfuse

- **Category/canonical source:** LLM observability/evaluation;
  <https://github.com/langfuse/langfuse>.
- **Historical claim:** critical tracing/metrics/evaluation plane for model,
  retrieval, agent, and tool activity.
- **Current verified fact:** official project is active, MIT except `ee/`,
  self-hostable with Docker/Compose or production Helm, and uses Postgres plus
  ClickHouse; current docs state local Compose is for testing/low scale and lacks
  HA/scaling/backup. Enterprise-only features include some RBAC, retention,
  audit logs, and server-side masking.
- **Relevance/gap:** potentially useful for timing/cost/error traces of the
  optional Ollama observer, not for image bytes or core face decisions. PicOrg
  already has durable audit JSON and run manifests.
- **Benefits/drawbacks:** useful latency/cost/error dashboards; substantial
  Postgres/ClickHouse/retention/PII burden and another authority. Face media and
  embeddings must remain out of traces.
- **Security/privacy/license/cost:** self-hosted core MIT; EE license for some
  controls; configure redaction, retention, isolated LAN bind, auth, backups,
  and disable/understand telemetry before any trial. Infrastructure and storage
  cost are non-trivial.
- **Health:** Healthy/active; official repo documents frequent tagged releases.
- **Compatibility/location:** future `agent_review.py`/pipeline timing hooks;
  High complexity.
- **Status/deployment:** **Test** only after a data-minimization design; Not
  installed.
- **Fixture/acceptance/rollback/owner:** local synthetic traces for one 100-item
  review run; accept only if no path/image/embedding/secret leaves the host,
  event latency is bounded, and audit IDs correlate; delete Compose volume and
  hooks to roll back; owner PicOrg maintainer.

#### ID 121 — Browser Use (preferred browser candidate if a browser test is needed)

- **Category/canonical source:** browser automation;
  <https://github.com/browser-use/browser-use>.
- **Historical claim:** very high priority; persistent profiles, domain
  restrictions, parallel workflows, and sensitive-data handling.
- **Current verified fact:** official repo is MIT, active (10,258 commits,
  112.9k stars, 118 issues), supports local Ollama/other LLMs, and warns that
  Chrome memory/parallelism need operational management. Its docs describe
  reuse of real browser profiles, which is a sensitive trust boundary.
- **Relevance/gap:** isolated browser smoke tests for the review UI; no face
  accuracy benefit and no role in identity assignment.
- **Complements/replaces:** complements existing Flask/Waitress and pytest UI
  tests; could replace brittle manual browser smoke checks, not the UI server.
- **Benefits/drawbacks:** realistic click/modal/assignment testing; browser
  profiles, cookies, downloads, prompt injection, and model nondeterminism add
  risk and runtime.
- **Security/privacy/license/cost:** MIT library; use a disposable profile,
  localhost/LAN allowlist, synthetic audit only, no logged-in media sources or
  cloud provider. Service/cloud mode would add cost and data exposure.
- **Health:** Healthy/active, but browser-agent dependency surface changes
  quickly; pin a revision and review security policy.
- **Compatibility/location:** isolated `tests/browser/` or a staging UI; Medium
  complexity.
- **Status/deployment:** **Test** / Not installed.
- **Fixture/acceptance/rollback/owner:** synthetic 20-item audit; search identity
  → drill-down → load all → modal next/previous → assign/undo → restart recovery;
  zero external requests and no file moves; remove test environment to roll back;
  owner PicOrg maintainer.

#### ID 125 — DeepEval

- **Category/source:** LLM/agent evaluation;
  <https://github.com/confident-ai/deepeval>.
- **Historical claim:** high-priority programmatic regression framework,
  potentially complementary to Promptfoo.
- **Current verified fact:** official repository is Apache-2.0 and active; current
  releases include 4.x and current documentation includes multimodal/trace and
  simulator work.
- **Relevance/gap:** only evaluates the optional AI quality observer; it does not
  calibrate face embeddings. It substantially overlaps Promptfoo.
- **Recommendation:** **Reject for now** (or defer a narrow comparison) to avoid
  two evaluation authorities. Test only if Promptfoo cannot express a required
  Python metric without an extra service. No deployment.

### Future Enhancements / Monitor

#### IDs 024–026 — Supabase/Obsidian/vector+graph memory

The inventory’s historical claim is semantic vector plus human-readable graph
memory. PicOrg already has SQLite embeddings, JSON audits, identity markers, and
Metadaily’s registry. Introducing another vector/graph authority would create
stale identity/retention/deletion conflicts and is not justified by current
profiling. **Defer/Reject** unless a measured retrieval problem appears; keep
face evidence in PicOrg/MD/RD authorities.

#### IDs 038–041 — router, orchestrator-worker, evaluator loop, parallel specialists

The inventory’s architecture is valid for dynamic agent work, but PicOrg’s core
pipeline is intentionally deterministic. **Adopt only the evaluator principle**
(ID 040) for bounded optional AI quality review; **Reject as core runtime
orchestration** for IDs 038–041. A router cannot decide identity moves. Existing
shell stages, checkpoints, and tests are simpler and safer.

#### IDs 058 and 080 — RedditDaily and Metadaily

These are existing directly related local projects, not new candidates. Keep
Metadaily’s canonical registry and RedditDaily’s protected source/download
metadata read-only. Their inventory “indexed/readiness requires local
verification” wording is accurate; it is not evidence of production approval.

#### IDs 091, 092, 098, 099, 101, 102, 106, 107 — AI-Memory, Ollama,
Prompt-Optimizer, AISkills, PyOllama, AIOrchestrator, OmniRoute, OmniGlyph

These are existing local projects. Ollama/OmniRoute can support the already
report-only `agent_review.py` path; AI-Memory remains the authority; the other
orchestration/skill/UI projects would duplicate PicOrg or add an unneeded
control plane. **Monitor/Test only for bounded observer use; do not promote any
to identity authority or auto-move capability.** OmniRoute’s local gateway
requires management authentication and should remain separate from the
unrestricted LAN review UI.

#### IDs 110, 118, 119, 121, 125, 134 — architecture learning loop

AAE (ID 134) is a taxonomy/reference, not a dependency. Use its emphasis on
evaluation/observability to guide the roadmap, but do not install another
framework solely for catalog completeness.

#### IDs 120 and 124 — Daytona vs E2B sandboxes

The historical claim is an isolated runtime for AI-generated code. Official
Daytona is AGPL-3.0 and current releases are active; E2B is Apache-2.0 and its
official repository describes managed/self-hosted cloud sandboxes. PicOrg does
not execute agent-generated code or untrusted plugins in its pipeline, so both
are **Reject/Defer**. If future skill testing requires a sandbox, benchmark one
in a separate security project; do not add both. Daytona’s 2026 advisory for
public-preview visibility reinforces the need for pinned patched versions and
private-by-default configuration.

#### IDs 132 and 133 — Browser Harness and Open-Browser-Use

Both are browser-control alternatives to ID 121. The inventory claims local CDP,
MCP, extension/native-host and self-healing helpers; current Browser Harness is
MIT and active, while Open-Browser-Use needs a separate trust/maintenance check.
They are more privileged and overlapping than needed for a single local UI.
**Monitor; prefer one isolated Browser Use test before comparing alternatives.**

#### ID 126 — agent-security-scanner-mcp and ID 128 — external-content sanitizer

The inventory itself says their security claims require independent benchmarking.
PicOrg has no MCP server and already treats filenames/titles as untrusted data.
Prompt-only sanitization cannot replace permission isolation. **Reject as runtime
dependencies; monitor as supply-chain test candidates** only if a future MCP/skill
registry is introduced.

## Candidates that duplicate existing functionality

- IDs 038/039/102 duplicate deterministic PicOrg pipeline orchestration.
- IDs 024–026/123 duplicate AI-Memory, SQLite/audit/marker authorities.
- IDs 118/125 overlap; choose at most one evaluation harness (Promptfoo first).
- IDs 121/132/133 overlap; choose at most one browser test/control standard.
- IDs 120/124 overlap; choose at most one sandbox strategy only if code execution
  becomes a real requirement.
- IDs 035–037 complement existing registry/audit controls rather than adding a
  package.

## Catalog verdict conflicts and corrections

- Inventory “critical” for Promptfoo and Langfuse is reasonable for a general
  agent platform, but it is not a PicOrg adoption approval: PicOrg is not an LLM
  application whose face decisions need an LLM judge.
- Inventory “very high” for Browser Use/Letta/OpenHands-style systems conflicts
  with PicOrg’s deterministic boundary; they are test/monitor candidates only.
- Inventory’s “existing local project” entries (MD/RD/Ollama/OmniRoute) correctly
  state that indexing does not imply activation, permissions, or production
  readiness.
- Browser Harness/Open-Browser-Use claims must not be treated as safer than
  Browser Use: a signed-in browser or native host is a privileged boundary.
- Daytona/E2B claims are technically relevant only to untrusted code execution,
  which PicOrg does not currently perform.

## Security and trust-boundary changes required before any adoption

1. Keep face media, embeddings, identities, and protected MD/RD paths local; use
   synthetic/redacted fixtures for external tooling.
2. Record candidate source URL, exact commit/version, license, dependency hashes,
   permissions, data classes, owner, and review date in the local registry before
   any pilot.
3. Run pilots in a disposable worktree/profile with no production credentials,
   no source-root write permission, no remote network by default, and bounded
   CPU/RAM/time/concurrency.
4. Require schema validation and human approval for every AI-generated observation;
   prohibit identity, move, delete, or registry fields from the observer.
5. Retain audit IDs and last-known-good DB/audit pointers; test interrupted-run
   recovery and UI undo before production use.
6. If Langfuse or any telemetry is piloted, redact paths, filenames, image bytes,
   embeddings, tokens, and sensitive prompts; define retention/deletion first.

## Prioritized roadmap

1. **P0 — Keep the deterministic boundary:** maintain registry/name evidence as
   priority, face clustering as review evidence, and human approval for moves.
2. **P0 — Reliability evidence:** make the existing fixture suite cover corrupt,
   missing, removable, duplicate, interrupted, and protected-root cases and
   publish one machine-readable run summary.
3. **P1 — Accuracy:** purify confirmed labels, add hard negatives and per-identity
   calibration, and report precision/recall at identity and cluster levels. Do
   not widen thresholds based on generic clusters.
4. **P1 — Provenance/governance:** formalize IDs 035–037 and 047–055 as a small
   local policy/manifest rather than adding services.
5. **P1 — UI quality:** implement ID 112 browser smoke scenarios against a
   synthetic audit; only then run an isolated Browser Use pilot (ID 121).
6. **P2 — AI observer evaluation:** if `agent_review.py` is valuable, compare a
   pinned Promptfoo fixture (ID 118) against existing pytest; no identity/move
   authority, no remote credentials.
7. **P2 — Observability:** measure decode, detection, embedding, retrieval,
   clustering, and UI latency from existing manifests first; test Langfuse only
   if JSON/run manifests cannot answer operational questions.
8. **P3 — Sandbox/memory/orchestration:** only revisit IDs 024–026, 038–041,
   120/124, and 123 after a documented capability gap; never introduce multiple
   competing authorities.

## What remains unverified

- No full license/weight audit was performed for every face model used by
  `photo_reorg`, InsightFace/UniFace options, or downloaded model artifacts.
- Current production DB quality/cluster purity remains data-dependent; passing
  unit tests does not establish 80–90% real-world identity precision.
- The complete state of running rebuild screens, removable media health, and
  current MD/RD registry freshness was not changed or inferred from this report.
- No candidate was installed, benchmarked, or exercised; current upstream facts
  are repository/documentation observations, not PicOrg-specific performance.
- Open-Browser-Use, AI-Memory/OpenViking alternatives, and candidate MCP scanners
  require deeper source/security review before any pilot.

## Change report

- **Entries added:** none to the canonical inventory; no inventory write was
  requested or performed.
- **Entries enriched:** this project-local evaluation records PicOrg relevance for
  IDs 028, 035–055, 058, 080, 091–093, 098–102, 106–107, 110, 112, 114,
  118–126, 132–134 without changing their canonical text.
- **Duplicates merged:** none in the canonical inventory; this report identifies
  overlapping candidate families (Promptfoo/DeepEval, browser tools, sandboxes,
  memory/orchestration).
- **Conflicts found:** catalog priority is broader than PicOrg’s deterministic
  scope; screenshot/historical claims for candidates are not deployment facts.
- **Deferred/rejected:** autonomous orchestrators, new memory stores, browser
  control in production, sandboxes, MCP scanners/sanitizers, cloud face/reverse
  image services, and duplicate evaluation stacks.
- **Evidence used:** full Google Doc revision above; local PicOrg source/docs/tests;
  `/opt/photo_reorg`, `/opt/metadaily`, `/opt/redditdaily`, and `/opt/omniroute`
  documentation; official upstream repositories/releases cited below.
- **Residual risk:** sensitive image/embedding leakage, false face matches,
  corrupt/removable media, dependency/model supply chain, and accidental move
  authority remain human-reviewed risks.

## Authoritative upstream evidence

- Promptfoo repository, MIT, local evaluation/red-team/CI behavior and Node
  requirement: <https://github.com/promptfoo/promptfoo>
- Promptfoo current release list (`0.122.2`, 2026-08-28):
  <https://github.com/promptfoo/promptfoo/releases>
- Langfuse repository/license and self-host architecture:
  <https://github.com/langfuse/langfuse>
- Langfuse self-hosting, low-scale limitations and MIT/EE split:
  <https://github.com/langfuse/langfuse-docs/blob/main/content/self-hosting/index.mdx>
- Browser Use repository, MIT, local Ollama option, profile/security caveats:
  <https://github.com/browser-use/browser-use>
- Browser Harness repository and MIT/CDP/MCP trust boundary:
  <https://github.com/browser-use/browser-harness>
- Daytona repository/license and sandbox model:
  <https://github.com/daytonaio/daytona>
- Daytona public-preview security advisory (patched in 0.184.0):
  <https://github.com/daytonaio/daytona/security/advisories/GHSA-ww63-pv5x-vfc8>
- E2B repository, Apache-2.0 and self-hosting model:
  <https://github.com/e2b-dev/e2b>
- DeepEval repository/license and current activity:
  <https://github.com/confident-ai/deepeval>
- Letta Code repository/license and persistent-agent scope:
  <https://github.com/letta-ai/letta-code>

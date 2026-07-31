# picorg Manual Runbook

## Goal

Run the organizer conservatively and repeatably so canonical identity folders stay stable across intake roots when you trigger it manually.

## Intake roots

- `/mnt/elements16/@mixedpics`
- `/mnt/elements16a/Pron/jdownloaderscomplete`
- `/mnt/desktop/Pictures`

## Canonical destination

- `/mnt/elements16/@mixedpics_sorted`

## Default workflow

1. Refresh or verify the local identity lists.
2. Run a dry pass.
3. Review the unmatched tail and any low-confidence matches.
4. Apply only if the dry pass is stable.
5. Review the audit JSON after the run.

The authoritative scoring rules live in [`OPERATING_POLICY.md`](/opt/picorg/OPERATING_POLICY.md).

## Manual Production Run

Use this exact flow for periodic runs. Keep it manual and review the audit before any apply.

### 1. Inspect the catalog

```bash
./picorg_manual.sh inspect
```

### 2. Run a dry pass

```bash
./picorg_manual.sh dry-run
```

Optional OCR-assisted review on a known local Tesseract image:

```bash
./picorg_manual.sh dry-run --ocr-image yock1/embycreditocr:latest
```

### 3. Review the audit

- Check `ground_truth_precision`, `ground_truth_recall`, `match_coverage`, and `source_metrics`.
- Inspect `top_unmatched` and the largest gallery clusters.
- Review any medium-confidence items and any identity family that suddenly dominates the run.
- Confirm that precedence-sensitive aliases still resolve the way you expect.

### 4. Apply only when stable

```bash
./picorg_manual.sh apply
```

### 5. Re-read the audit

- Confirm the apply report is consistent with the dry run.
- Verify that any moved folders stayed intact where possible.
- Review `duplicates/` for exact-content collisions.

## Commands

Dry run:

```bash
python3 picorg_sorter.py dry-run --audit-out /tmp/picorg_dry.json
```

Apply:

```bash
python3 picorg_sorter.py dry-run --apply --audit-out /tmp/picorg_apply.json
```

Manifest export:

```bash
python3 picorg_sorter.py manifest --output /tmp/picorg_manifest.json
```

Catalog inspection:

```bash
python3 picorg_sorter.py inspect --limit 20
```

## Decision thresholds

- `0.95` to `1.00`: high confidence.
  - Safe for dry-run analysis and usually safe to apply when the source family is stable.
- `0.80` to `0.94`: medium confidence.
  - Review before apply unless the source family has been explicitly validated.
- Below `0.80`: low confidence or unmatched.
  - Keep for manual review or leave in place.
- If labeled precision is below `0.99`, or labeled recall is below `0.99`, stop and inspect the unmatched tail.
- If the run is dominated by a new source family, update the identity registry before applying again.
- If identical folders recur, expect them to land in `duplicates/`.

## Acceptance criteria

Treat a run as production-ready only when all of the following are true:

- `ground_truth_precision` and `ground_truth_recall` are each at least `0.99`.
- No new low-confidence cluster appears in the top matches.
- The unmatched tail is still dominated by known generic or camera-generated clusters.

## Profile and image verification

Use `/opt/redditdaily/docs/IDENTITY_PROFILE_VERIFICATION.md` before adding public
profile handles. Record confirmed evidence in
`/opt/picorg/identity_profile_verification.json`; candidate and probable
profiles are intentionally ignored by routing.

For permitted public reference images, use `profile_image_match.py` with a
local reference tree and review its JSON report. It uses normalized image
fingerprints only, never fetches URLs, and never authorizes apply mode. Exact or
near-identical reference hits are corroboration for a human review, not proof
of identity by themselves.

For unmatched files, `reverse_search_queue.py` can export a bounded queue of
paths, SHA-256 hashes, and title hints. Any external reverse-image search must
be manual and consent-aware; `FB IMG` and `RDT` entries are source-labeled,
not discarded. Use `--per-gallery` to keep large download clusters diverse,
and record independent corroboration before changing the verified profile registry.
- Any new aliases are verified before they are added to [`project_registry.json`](/opt/picorg/project_registry.json).
- Precedence-sensitive identities still resolve to the intended canonical target.

## Matching policy

- Use subreddit, author, title, filename, and parent folder when available.
- Treat exact alias matches as stronger than heuristics.
- Do not promote bare generic words into identities; generic tokens only count when they are embedded in a username or handle.
- Treat numbered filename variants like `(1)`, `(2)`, ... as one gallery set when reviewing or applying a folder.
- Treat `#####.ext` suffix series as one gallery set when the filenames clearly belong to the same download batch.
- Preserve source folders intact where possible.
- Fall back to file-level moves only for mixed or flat intake.
- Keep project-specific overrides in [`project_registry.json`](/opt/picorg/project_registry.json); do not edit ingested source lists for local fixes.
- Keep OCR opt-in. Enable `PICORG_OCR_IMAGE` only for low-confidence review runs where the extra latency is acceptable.
- Use OCR as a last-resort signal after subreddit, author, title, filename, and folder context.
- For ad hoc OCR runs, prefer `./picorg_manual.sh dry-run --ocr-image yock1/embycreditocr:latest` instead of exporting OCR env vars globally.

## Operating rubric

- High-confidence exact registry hits may be used to promote a folder or file in a dry-run analysis.
- Medium-confidence items require a second supporting signal before apply.
- Low-confidence items stay in review unless the source family has already been validated.
- Apply mode currently moves only matches at or above `0.95` confidence; weaker matches are reported as skipped.
- Generic-word collisions should be treated as false-positive risk, not as new identities.
- Gallery-level decisions should override isolated filename noise when the series is clearly one post.
- Source-family shifts should trigger registry expansion, not broader fuzziness.

## Review points

- `unmatched/` for manual follow-up.
- `duplicates/` for duplicate content checks.
- `_audit/` or the configured audit root for run history.

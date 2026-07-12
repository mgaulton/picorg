# picorg Operating Policy

## Purpose

Organize mixed Reddit and creator media into canonical identity folders with conservative, repeatable decisions.

## Operating rules

- Run manually only.
- Start with a dry run.
- Apply only after reviewing the unmatched tail and any low-confidence matches.
- Keep project-local aliases and block rules in `project_registry.json`.
- Do not update ingested source lists for local corrections.
- Preserve intact folders where possible.
- Fall back to file-level moves only for mixed or flat intake.
- Treat numbered filename variants like `(1)`, `(2)`, etc. as one gallery set.
- Treat `#####.ext` suffix runs as one gallery set when they are clearly repeated post downloads.
- Keep OCR opt-in and manual-only.
- Use OCR only as a last-resort signal.
- Prefer exact alias hits over fuzzy token overlap.
- Do not let bare generic words create new identities unless they are part of a username or handle.

## Source precedence

1. Exact registry alias.
2. Explicit Reddit markers such as `u/`, `r/`, `author`, `subreddit`, or `posted in`.
3. Title, filename, and parent-folder context.
4. OCR text when enabled.
5. Leave unmatched.

## Identity sources

- Reddit friend and follow lists.
- `pscrape` redditor lists.
- `redditdaily` identities and folder names.
- `metdaily` social account identities.
- IMDb actor lists.
- Project-local overrides in `project_registry.json`.

## Confidence rubric

- `0.95` to `1.00`: high confidence.
  - Apply automatically in dry-run analysis.
  - Usually safe to promote if the source family is stable.
- `0.80` to `0.94`: medium confidence.
  - Review before apply unless the source is already well understood.
  - Require at least two supporting signals when the name is ambiguous.
- `0.50` to `0.79`: low confidence.
  - Do not apply without manual confirmation.
  - Prefer to keep in review or unmatched.
- Below `0.50`: unmatched.
  - Leave in place.

## Strong signals

- Exact alias match in the registry.
- Exact `u/` or `r/` marker.
- Matching canonical name in a title that clearly came from Reddit.
- Gallery groups whose filenames and post title agree.
- OCR text that repeats a known alias or canonical name.

## Weak signals

- Generic word overlap.
- Partial substring matches.
- OCR text alone without a registry hit.
- A filename that only looks like a creator name because it shares common words.

## Review queue rules

- Anything below `0.80` goes to review unless the source family is already validated.
- Anything that depends on a generic word should be treated as ambiguous.
- If a shard produces mostly unmatched files, treat it as a source-quality problem, not a matcher failure.
- If one source family dominates a run, expand the registry before applying again.

## Dedupe rules

- Detect exact duplicates before organizing.
- Keep one canonical object and record the duplicates separately.
- Prefer checksum-based dedupe over filename dedupe.
- Do not move duplicate groups into identity folders unless the canonical object is already stable.

## Periodic manual workflow

1. Run a dry pass against the target root.
2. Inspect the top matches, unmatched tail, and any low-confidence items.
3. Add only well-evidenced aliases to `project_registry.json`.
4. Rerun the dry pass.
5. Apply only when the distribution is stable.
6. Record the run outcome in the audit JSON.

## Accuracy expectations

- A clean curated root can approach perfect proxy accuracy.
- A heterogeneous flat intake root will usually have a large unmatched tail.
- The goal is stable organization with low false positives, not forced full coverage.

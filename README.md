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
- Bare generic words are not treated as identities unless they are part of a username/handle-shaped string.
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
- `/opt/redditdaily/redditsubs.txt`
- `/opt/redditdaily/data/`
- Existing folder names under `/mnt/elements16a/Pron/redditdaily`
- Existing folder names under `/mnt/elements16a/Pron/pscrape`
- Follow/friend lists under `/opt/reddit/`, `/opt/redditgrab/`, `/opt/grabplaylist/`, and `/opt/photo_reorg/`

## Usage

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

Apply mode exists, but should be used only when the destination tree is writable and the audit output has been reviewed.

For manual operator runs, use [`RUNBOOK.md`](/opt/picorg/RUNBOOK.md), [`OPERATING_POLICY.md`](/opt/picorg/OPERATING_POLICY.md), and the wrapper script [`picorg_manual.sh`](/opt/picorg/picorg_manual.sh).

## Manual production flow

Use this when you want a periodic run without automation:

1. Inspect the catalog.
2. Run a dry pass.
3. Review `ground_truth_accuracy`, `top_unmatched`, and any medium-confidence or precedence-sensitive matches.
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

- Scanned: 3877
- Matched: 2563
- Unmatched: 1314
- High confidence: 2468
- Proxy ground-truth accuracy: 0.9979

This baseline is intentionally conservative: it prioritizes correct identity placement over forcing a guess on caption-only files, and it no longer lets bare generic words become identities.

## Reddit matching order

1. Explicit `u/` or `r/` markers in the file path or name.
2. Exact alias match from the identity registry.
3. Canonical name match in the title or filename.
4. Parent folder hint.
5. OCR text from the image when configured.
6. Fallback to `unmatched`.

Numbered filename variants like `(1)`, `(2)`, etc. are treated as the same gallery title for matching and review.

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

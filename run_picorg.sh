#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
    exec /usr/bin/env bash "$0" "$@"
fi
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Baseline policy: refresh intake and evidence, but do not move library files
# unless the caller explicitly requests the high-confidence apply path.
RUN_INGEST=1
APPLY_HIGH_CONFIDENCE=0
DEDUPE_APPLY=0
REUSE_FACE_AUDIT=0

usage() {
    cat <<'EOF'
Usage: run_picorg.sh [options]

Runs the complete conservative PicOrg workflow:
  1. Move completed downloads into the protected intake area.
  2. Hash-priority dedupe (redditdaily/metadaily wins; report by default).
  3. PicOrg dry-run and audit generation.
  4. Face matching/grouping with cached embeddings where available.
  5. Reconcile review data and start the LAN review UI on port 8787.

Options:
  --no-ingest              Skip move_downloads_remote.sh.
  --dry-run                Skip intake and all apply actions; produce reports/UI.
  --apply-high-confidence  Apply only the existing PicOrg safety-gated high-
                           confidence name matches, then rebuild face refs.
  --dedupe-apply           Quarantine exact duplicates from target roots.
  --reuse-faces            Reuse the newest complete face audit; never extract.
  -h, --help               Show this help.

The default is non-destructive for the photo library. Dedupe uses a persistent
hash cache under .cache/picorg and never changes priority roots.
EOF
}

while (($#)); do
    case "$1" in
        --no-ingest) RUN_INGEST=0 ;;
        --dry-run) RUN_INGEST=0 ;;
        --apply-high-confidence) APPLY_HIGH_CONFIDENCE=1 ;;
        --dedupe-apply) DEDUPE_APPLY=1 ;;
        --reuse-faces) REUSE_FACE_AUDIT=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

echo "[1/3] intake and priority dedupe"
if ((RUN_INGEST)); then
    echo "[pipeline] ingest: /opt/move_downloads_remote.sh"
    /opt/move_downloads_remote.sh
else
    echo "[pipeline] ingest: skipped (--no-ingest)"
fi

if ((DEDUPE_APPLY || APPLY_HIGH_CONFIDENCE)); then
    .venv/bin/python "$ROOT_DIR/dedupe_priority.py" \
        --priority-root /mnt/elements16a/Pron/redditdaily \
        --priority-root /mnt/elements16a/Pron/metadaily \
        --target-root /mnt/elements16/@mixedpics \
        --target-root /mnt/desktop \
        --apply
else
    .venv/bin/python "$ROOT_DIR/dedupe_priority.py" \
        --priority-root /mnt/elements16a/Pron/redditdaily \
        --priority-root /mnt/elements16a/Pron/metadaily \
        --target-root /mnt/elements16/@mixedpics \
        --target-root /mnt/desktop
fi

if ((REUSE_FACE_AUDIT)); then
    export USE_EXISTING_FACE_AUDIT=1
fi

echo "[2/3] face matching, grouping, and review reconciliation"
if ((APPLY_HIGH_CONFIDENCE)); then
    "$ROOT_DIR/run_face_review_pipeline.sh" --apply-high-confidence
else
    "$ROOT_DIR/run_face_review_pipeline.sh"
fi

echo "[3/3] workflow complete; review UI should be available at http://${HOST:-0.0.0.0}:${PORT:-8787}/"

#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Default mode stops at manual review; live apply requires an explicit option.
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8787}"
AUDIT_ROOT="${AUDIT_ROOT:-$ROOT_DIR/.cache/picorg/audits}"
INSTALL_FACE_DEPS="${INSTALL_FACE_DEPS:-1}"
REFERENCE_ROOT="${REFERENCE_ROOT:-/mnt/elements16/@mixedpics_sorted}"
FACE_DB="${FACE_DB:-/opt/photo_reorg/data/high_accuracy_faces.db}"
LIVE_HIGH_CONFIDENCE=0
RUN_INGEST=0

usage() {
    cat <<'EOF'
Usage: run_face_review_pipeline.sh [--ingest] [--apply-high-confidence]

Default: picorg dry-run, face grouping, and LAN review UI; no moves.
--ingest: run move_downloads_remote.sh before the dry-run/apply stages.
--apply-high-confidence: apply picorg's >=0.95 name matches first, rebuild
                         face references from REFERENCE_ROOT, then match/group/UI.
EOF
}

while (($#)); do
    case "$1" in
        --ingest) RUN_INGEST=1 ;;
        --apply-high-confidence) LIVE_HIGH_CONFIDENCE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if ((LIVE_HIGH_CONFIDENCE)); then
    echo "[1/5] applying picorg high-confidence name matches (>=0.95)"
    if ((RUN_INGEST)); then
        ./run_media_pipeline.sh --ingest --skip-photo
    else
        ./run_media_pipeline.sh --skip-photo
    fi
    AUDIT_GATE="$(find "$AUDIT_ROOT" -maxdepth 1 -type f -name '*.json' ! -name '*.preflight.json' ! -name '*.face-clusters.json' ! -name '*.reconciled.json' -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')"
    .venv/bin/python pipeline_safety_gate.py --audit "$AUDIT_GATE" \
        --min-precision "${MIN_GROUND_TRUTH_PRECISION:-0.99}" \
        --min-recall "${MIN_GROUND_TRUTH_RECALL:-0.99}"
    echo "[2/5] applying picorg high-confidence name matches"
    ./picorg_manual.sh apply
else
    echo "[1/3] running picorg dry-run pipeline (no apply, photo_reorg skipped)"
    if ((RUN_INGEST)); then
        ./run_media_pipeline.sh --ingest --skip-photo
    else
        ./run_media_pipeline.sh --skip-photo
    fi
fi

AUDIT="$(find "$AUDIT_ROOT" -maxdepth 1 -type f -name '*.json' \
    ! -name '*.preflight.json' ! -name '*.face-clusters.json' ! -name '*.reconciled.json' \
    -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')"
if [[ -z "$AUDIT" || ! -f "$AUDIT" ]]; then
    echo "error: no audit found under $AUDIT_ROOT" >&2
    exit 2
fi

if ((LIVE_HIGH_CONFIDENCE)); then
    echo "[3/6] rebuilding face references from organized root: $REFERENCE_ROOT"
    if [[ ! -d "$REFERENCE_ROOT" || ! -x /opt/photo_reorg/venv/bin/python ]]; then
        echo "error: reference root or photo_reorg venv is unavailable" >&2
        exit 2
    fi
    if ! /opt/photo_reorg/venv/bin/python -c 'import face_recognition_models' >/dev/null 2>&1; then
        echo "error: photo_reorg venv lacks face_recognition_models; install its face requirements before rebuilding" >&2
        exit 2
    fi
    (cd /opt/photo_reorg && venv/bin/python rebuild_face_database.py --source-dirs "$REFERENCE_ROOT" --verbose)
    echo "[4/6] matching remaining audit items against rebuilt face database"
    .venv/bin/python face_group_unmatched.py --audit "$AUDIT" --db "$FACE_DB" \
        --output "${AUDIT%.json}.face-matches.json"
    echo "[5/6] selected audit: $AUDIT"
else
    echo "[2/3] selected audit: $AUDIT"
fi
MANIFEST="${AUDIT%.json}.run-manifest.json"
.venv/bin/python pipeline_run_manifest.py --audit "$AUDIT" --output "$MANIFEST" \
    --backend "${FACE_BACKEND:-dlib}"
if ((LIVE_HIGH_CONFIDENCE)); then
    echo "[6/6] starting LAN review UI at http://${HOST}:${PORT}/"
else
    echo "[3/3] starting LAN review UI at http://${HOST}:${PORT}/"
fi
exec env AUDIT="$AUDIT" AUDIT_ROOT="$AUDIT_ROOT" HOST="$HOST" PORT="$PORT" INSTALL_FACE_DEPS="$INSTALL_FACE_DEPS" ./runweb.sh

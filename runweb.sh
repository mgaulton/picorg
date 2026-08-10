#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

AUDIT_ROOT="${AUDIT_ROOT:-$ROOT_DIR/.cache/picorg/audits}"
if [[ -z "${AUDIT:-}" ]]; then
    AUDIT="$(find "$AUDIT_ROOT" -maxdepth 1 -type f -name '*.json' \
        ! -name '*.preflight.json' ! -name '*.face-clusters.json' ! -name '*.reconciled.json' \
        -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')"
    AUDIT="${AUDIT:-/tmp/picorg_sorted_audit/20260731T170645Z.json}"
fi
FACE_AUDIT="${FACE_AUDIT:-${AUDIT%.json}.face-clusters.json}"
CACHE_ROOT="${CACHE_ROOT:-$ROOT_DIR/.cache/picorg}"
mkdir -p "$CACHE_ROOT"
FACE_CACHE="${FACE_CACHE:-$CACHE_ROOT/$(basename "${AUDIT%.json}").face-embeddings.json}"
FACE_BACKEND="${FACE_BACKEND:-dlib}"
LEGACY_FACE_CACHE="${AUDIT%.json}.face-embeddings.json"
RECONCILED_AUDIT="${RECONCILED_AUDIT:-${AUDIT%.json}.reconciled.json}"
PREFLIGHT="${PREFLIGHT:-${AUDIT%.json}.preflight.json}"
DECISIONS="${DECISIONS:-$ROOT_DIR/review_decisions.json}"
IMAGE_DECISIONS="${IMAGE_DECISIONS:-$ROOT_DIR/review_image_decisions.json}"
REVIEW_IDENTITIES="${REVIEW_IDENTITIES:-$ROOT_DIR/review_identities.json}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8787}"

LOCK_FILE="${LOCK_FILE:-/tmp/picorg-runweb.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "another runweb.sh instance is already active (lock: $LOCK_FILE)" >&2
    exit 1
fi
LOG_ROOT="${LOG_ROOT:-$ROOT_DIR/.cache/picorg/logs}"
mkdir -p "$LOG_ROOT"
LOG_FILE="${LOG_FILE:-$LOG_ROOT/runweb-$(date -u +%Y%m%dT%H%M%SZ).log}"
exec > >(tee -a "$LOG_FILE") 2>&1

if [[ ! -f "$AUDIT" ]]; then
    echo "error: audit not found: $AUDIT" >&2
    exit 2
fi

if [[ ! -s "$FACE_CACHE" && -s "$LEGACY_FACE_CACHE" ]]; then
    cp -p -- "$LEGACY_FACE_CACHE" "$FACE_CACHE"
    echo "migrated embedding cache to $FACE_CACHE"
fi

echo "[1/5] classifying unmatched media inputs"
.venv/bin/python media_preflight.py "$AUDIT" --output "$PREFLIGHT" --verify-images

echo "[2/5] validating face-matching dependency"
if ! .venv/bin/python -c 'import face_recognition' >/dev/null 2>&1; then
    if [[ "${INSTALL_FACE_DEPS:-0}" == "1" ]]; then
        .venv/bin/pip install -r requirements-face.txt
    else
        echo "error: face_recognition is not installed" >&2
        echo "run INSTALL_FACE_DEPS=1 $0 once, or install requirements-face.txt manually" >&2
        exit 2
    fi
fi

if [[ ! -s "$FACE_AUDIT" || "${FORCE_FACE_REBUILD:-0}" == "1" ]]; then
    echo "[3/5] building face clusters from $AUDIT (large collections may take time)"
    if [[ "${FORCE_FACE_REBUILD:-0}" == "1" ]]; then
        rm -f -- "$FACE_AUDIT" "$FACE_CACHE"
    fi
    .venv/bin/python face_cluster_unmatched.py \
        --audit "$AUDIT" \
        --output "$FACE_AUDIT" \
        --cache "$FACE_CACHE" \
        --backend "$FACE_BACKEND"
fi

echo "[4/5] reconciling name and face clusters"
.venv/bin/python reconcile_review_clusters.py \
    --name-audit "$AUDIT" \
    --face-audit "$FACE_AUDIT" \
    --output "$RECONCILED_AUDIT"

echo "[5/5] starting LAN review UI at http://${HOST}:${PORT}/"
UI_ARGS=( \
    --audit "$RECONCILED_AUDIT" \
    --decisions "$DECISIONS" \
    --image-decisions "$IMAGE_DECISIONS" \
    --review-identities "$REVIEW_IDENTITIES" \
    --host "$HOST" \
    --port "$PORT" \
)
exec .venv/bin/python review_ui.py "${UI_ARGS[@]}"

#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

AUDIT="${AUDIT:-/tmp/picorg_sorted_audit/20260731T170645Z.json}"
FACE_AUDIT="${FACE_AUDIT:-${AUDIT%.json}.face-clusters.json}"
FACE_CACHE="${FACE_CACHE:-${AUDIT%.json}.face-embeddings.json}"
RECONCILED_AUDIT="${RECONCILED_AUDIT:-${AUDIT%.json}.reconciled.json}"
DECISIONS="${DECISIONS:-$ROOT_DIR/review_decisions.json}"
IMAGE_DECISIONS="${IMAGE_DECISIONS:-$ROOT_DIR/review_image_decisions.json}"
REVIEW_IDENTITIES="${REVIEW_IDENTITIES:-$ROOT_DIR/review_identities.json}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8787}"

if [[ ! -f "$AUDIT" ]]; then
    echo "error: audit not found: $AUDIT" >&2
    exit 2
fi

echo "[1/4] validating face-matching dependency"
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
    echo "[2/4] building face clusters from $AUDIT (large collections may take time)"
    if [[ "${FORCE_FACE_REBUILD:-0}" == "1" ]]; then
        rm -f -- "$FACE_AUDIT" "$FACE_CACHE"
    fi
    .venv/bin/python face_cluster_unmatched.py \
        --audit "$AUDIT" \
        --output "$FACE_AUDIT" \
        --cache "$FACE_CACHE"
fi

echo "[3/4] reconciling name and face clusters"
.venv/bin/python reconcile_review_clusters.py \
    --name-audit "$AUDIT" \
    --face-audit "$FACE_AUDIT" \
    --output "$RECONCILED_AUDIT"

echo "[4/4] starting LAN review UI at http://${HOST}:${PORT}/"
exec .venv/bin/python review_ui.py \
    --audit "$RECONCILED_AUDIT" \
    --decisions "$DECISIONS" \
    --image-decisions "$IMAGE_DECISIONS" \
    --review-identities "$REVIEW_IDENTITIES" \
    --host "$HOST" \
    --port "$PORT"

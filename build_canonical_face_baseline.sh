#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PICORG_PYTHON:-$ROOT_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "error: Python environment not found: $PYTHON_BIN" >&2
  exit 2
fi

# Refresh the local evidence authority from the canonical shared registry
# before selecting identity folders.  The registry and marker inputs are
# read-only; only PicOrg's SQLite evidence store is updated.
IDENTITY_REGISTRY="${PICORG_IDENTITY_REGISTRY:-/opt/shared/identity_aliases.json}"
IDENTITY_STORE="${PICORG_IDENTITY_STORE:-$ROOT_DIR/.cache/picorg/identity_evidence.sqlite3}"
if [[ "${PICORG_BASELINE_SYNC_EVIDENCE:-1}" != "0" ]]; then
  "$PYTHON_BIN" identity_evidence_store.py \
    --db "$IDENTITY_STORE" \
    --md-registry "$IDENTITY_REGISTRY" \
    --markers "$ROOT_DIR/identity_face_markers.json" \
    --assignments "$ROOT_DIR/review_image_decisions.json"
fi

# The baseline is local and read-only with respect to MD/RD.  Extraction is
# enabled by default so this one command can establish a useful gallery; set
# PICORG_BASELINE_EXTRACT=0 for a cache-only/report pass.
args=(build_canonical_face_baseline.py)
if [[ "${PICORG_BASELINE_EXTRACT:-1}" != "0" ]]; then
  args+=(--extract-missing)
fi
if [[ -n "${PICORG_BASELINE_WORKERS:-}" ]]; then
  args+=(--workers "$PICORG_BASELINE_WORKERS")
fi

exec "$PYTHON_BIN" "${args[@]}" "$@"

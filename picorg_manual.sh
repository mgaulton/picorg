#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
AUDIT_ROOT="${AUDIT_ROOT:-$ROOT_DIR/.cache/picorg/audits}"
LIMIT="${LIMIT:-20}"
OCR_IMAGE="${OCR_IMAGE:-}"
OCR_COMMAND_JSON="${OCR_COMMAND_JSON:-}"

usage() {
  cat <<'EOF'
Usage:
  picorg_manual.sh dry-run [--ocr-image IMAGE]
  picorg_manual.sh apply [--ocr-image IMAGE]
  picorg_manual.sh manifest
  picorg_manual.sh inspect
EOF
}

cmd="${1:-}"
shift || true

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ocr-image)
      OCR_IMAGE="${2:-}"
      shift 2
      ;;
    --ocr-command-json)
      OCR_COMMAND_JSON="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$cmd" in
  dry-run)
    mkdir -p "$AUDIT_ROOT"
    if [ -n "$OCR_IMAGE" ]; then
      export PICORG_OCR_IMAGE="$OCR_IMAGE"
    fi
    if [ -n "$OCR_COMMAND_JSON" ]; then
      export PICORG_OCR_COMMAND_JSON="$OCR_COMMAND_JSON"
    fi
    exec "$PYTHON" "$ROOT_DIR/picorg_sorter.py" dry-run --limit "$LIMIT" --audit-root "$AUDIT_ROOT" --audit-out /tmp/picorg_periodic_dry.json
    ;;
  apply)
    mkdir -p "$AUDIT_ROOT"
    if [ -n "$OCR_IMAGE" ]; then
      export PICORG_OCR_IMAGE="$OCR_IMAGE"
    fi
    if [ -n "$OCR_COMMAND_JSON" ]; then
      export PICORG_OCR_COMMAND_JSON="$OCR_COMMAND_JSON"
    fi
    exec "$PYTHON" "$ROOT_DIR/picorg_sorter.py" dry-run --apply --limit "$LIMIT" --audit-root "$AUDIT_ROOT" --audit-out /tmp/picorg_periodic_apply.json
    ;;
  manifest)
    exec "$PYTHON" "$ROOT_DIR/picorg_sorter.py" manifest --output /tmp/picorg_manifest.json
    ;;
  inspect)
    exec "$PYTHON" "$ROOT_DIR/picorg_sorter.py" inspect --limit "$LIMIT"
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac

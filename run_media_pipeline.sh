#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHOTO_ROOT="/opt/photo_reorg"
RUN_INGEST=0
RUN_PICORG_APPLY=0
RUN_PHOTO=1

usage() {
  cat <<'EOF'
Usage: run_media_pipeline.sh [--ingest] [--apply] [--skip-photo]

Stages:
  --ingest       Run /opt/move_downloads_remote.sh (moves incoming files).
  --apply        Apply picorg moves after its dry-run.
  --skip-photo   Skip the photo_reorg dry-run stage.

Without --ingest or --apply, all stages are non-mutating dry runs.
EOF
}

while (($#)); do
  case "$1" in
    --ingest) RUN_INGEST=1 ;;
    --apply) RUN_PICORG_APPLY=1 ;;
    --skip-photo) RUN_PHOTO=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ((RUN_INGEST)); then
  echo "[pipeline] ingest: /opt/move_downloads_remote.sh"
  /opt/move_downloads_remote.sh
else
  echo "[pipeline] ingest: skipped (use --ingest)"
fi

echo "[pipeline] picorg: dry-run"
"$ROOT_DIR/picorg_manual.sh" dry-run

if ((RUN_PICORG_APPLY)); then
  echo "[pipeline] picorg: apply"
  "$ROOT_DIR/picorg_manual.sh" apply
else
  echo "[pipeline] picorg: apply skipped (use --apply)"
fi

if ((RUN_PHOTO)); then
  echo "[pipeline] photo_reorg: dry-run"
  if [[ -x "$PHOTO_ROOT/venv/bin/python" ]]; then
    (cd "$PHOTO_ROOT" && venv/bin/python run.py --dry-run)
  else
    echo "photo_reorg venv is not ready: $PHOTO_ROOT/venv/bin/python" >&2
    exit 1
  fi
else
  echo "[pipeline] photo_reorg: skipped"
fi

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
  --apply        Run the fingerprinted precision-gated name organization.
  --skip-photo   Skip the photo_reorg dry-run stage.

Without --apply, all stages are non-mutating dry runs.  The apply path is
delegated to run_name_org.sh and never bypasses its safety gate.
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

if ((RUN_PICORG_APPLY)); then
  # The legacy script used to call picorg_manual.sh apply directly, bypassing
  # the fingerprint and precision safety gate in run_name_org.sh.  Delegate
  # the complete name stage to the gated entry point instead.
  echo "[pipeline] picorg: gated name organization"
  if ((RUN_INGEST)); then
    "$ROOT_DIR/run_name_org.sh" --ingest
  else
    "$ROOT_DIR/run_name_org.sh"
  fi
else
  if ((RUN_INGEST)); then
    echo "[pipeline] ingest: /opt/move_downloads_remote.sh"
    /opt/move_downloads_remote.sh
  else
    echo "[pipeline] ingest: skipped (use --ingest)"
  fi
  echo "[pipeline] picorg: dry-run"
  "$ROOT_DIR/picorg_manual.sh" dry-run
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

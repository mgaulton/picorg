#!/usr/bin/env bash
set -Eeuo pipefail

# Read-only release smoke check.  It deliberately does not ingest, move,
# rebuild, start services, or contact external providers.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

FULL=0
case "${1:-}" in
  "") ;;
  --full) FULL=1 ;;
  -h|--help)
    printf 'Usage: %s [--full]\n' "${BASH_SOURCE[0]}"
    printf '  default: focused safety/UI/observer tests; --full: complete pytest suite\n'
    exit 0
    ;;
  *) echo "error: unknown option: $1" >&2; exit 2 ;;
esac

SCRIPTS=(
  run_picorg.sh run_face_review_pipeline.sh runweb.sh run_name_org.sh
  rebuild_face_data.sh rebuild_face_data_recover.sh picorg_screen.sh
  restart_picorg_review.sh run_existing_face_db.sh
)
for script in "${SCRIPTS[@]}"; do
  if [[ -f "$script" ]]; then
    bash -n "$script"
  fi
done
echo "[release-check] bash syntax: passed"

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck --severity=error "${SCRIPTS[@]}"
  echo "[release-check] shellcheck (errors): passed"
else
  echo "[release-check] shellcheck: skipped (not installed)"
fi

if ((FULL)); then
  ./run_tests.sh -q
else
  ./run_tests.sh -q \
    tests/test_production_readiness.py \
    tests/test_pipeline_safety_gate.py \
    tests/test_face_cluster_unmatched.py \
    tests/test_review_ui.py \
    tests/test_agent_review.py
fi
echo "[release-check] pytest: passed"
echo "[release-check] no files moved; no services started"

#!/usr/bin/env bash
set -euo pipefail

AUDIT="${AUDIT:-/tmp/picorg_periodic_apply.json}"
BATCH_SIZE="${BATCH_SIZE:-500}"
BATCH_DIR="${BATCH_DIR:-/tmp}"
CHECKPOINT="${CHECKPOINT:-/tmp/picorg_face_grouping.checkpoint.json}"
PYTHON="${PYTHON:-/opt/photo_reorg/venv/bin/python}"
SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/face_group_unmatched.py"
RECONCILER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reconcile_face_group_batches.py"

# Keep the loop bound aligned with face_group_unmatched.py: video-only
# unmatched entries are valid audit rows but are not face-group candidates.
total=$(jq '[.results[] | select(.rule == "unmatched") | .path
  | select(type == "string" and ((ascii_downcase | endswith(".jpg"))
    or (ascii_downcase | endswith(".jpeg"))
    or (ascii_downcase | endswith(".png"))
    or (ascii_downcase | endswith(".webp"))
    or (ascii_downcase | endswith(".gif"))
    or (ascii_downcase | endswith(".bmp"))
    or (ascii_downcase | endswith(".tif"))
    or (ascii_downcase | endswith(".tiff"))))] | length' "$AUDIT")
offset=0
if [[ -f "$CHECKPOINT" ]]; then
  offset=$(jq -r '.next_offset // 0' "$CHECKPOINT")
fi

while ((offset < total)); do
  output="$BATCH_DIR/picorg_face_grouping_batch-$(printf '%05d' "$offset").json"
  log="${output%.json}.log"
  status="${output%.json}.status"
  if [[ -f "$status" ]] && [[ "$(cat "$status")" == "0" ]] && [[ -f "$output" ]]; then
    offset=$((offset + BATCH_SIZE))
    continue
  fi
  if "$PYTHON" "$SCRIPT" --audit "$AUDIT" --offset "$offset" --max-files "$BATCH_SIZE" \
    --limit-per-cluster 0 --checkpoint "$CHECKPOINT" --output "$output" >"$log" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  echo "$rc" >"$status"
  ((rc == 0)) || exit "$rc"
  offset=$((offset + BATCH_SIZE))
done

"$PYTHON" "$RECONCILER" --audit "$AUDIT" --batch-dir "$BATCH_DIR" \
  --output "$BATCH_DIR/picorg_face_grouping_reconciled.json"
